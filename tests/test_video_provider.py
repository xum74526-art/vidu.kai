import json

import httpx
import pytest
from pydantic import ValidationError

from vidu_purchase.models import DomainError
from vidu_purchase.video_models import VideoConfig, VideoEstimate
from vidu_purchase.video_provider import AccountBinding, WebsiteVideoProvider, read_accounts


def adapter(monkeypatch, handler, *, enabled=False):
    monkeypatch.setenv("VIDU_ACCOUNT_ALICE_TOKEN", "fake-alice-secret")
    return WebsiteVideoProvider(
        {"alice": AccountBinding(token_env="VIDU_ACCOUNT_ALICE_TOKEN")},
        generation_enabled=enabled,
        transport=httpx.MockTransport(handler),
    )


async def test_disconnected_account_is_not_reported_as_nonexistent():
    provider = WebsiteVideoProvider({})
    result = await provider.account("alice")
    assert result.status == "not_connected"
    assert result.connection_url == "https://www.vidu.com"


async def test_official_token_protocol_and_strict_account_data(monkeypatch):
    def handler(request):
        assert request.url == "https://service.vidu.com/credit/v1/credits/me"
        assert request.headers["authorization"] == "Token fake-alice-secret"
        return httpx.Response(200, json={"credits": 99, "private_account_field": "hidden"})

    provider = adapter(monkeypatch, handler)
    result = await provider.account("alice")
    assert result.credits == 99
    assert "hidden" not in result.model_dump_json()
    assert "secret" not in result.model_dump_json()
    assert (await provider.account("bob")).status == "not_connected"


@pytest.mark.parametrize("payload", [{}, {"credits": None}, {"credits": "100"}, {"credits": True}])
async def test_unknown_balance_is_not_zero(monkeypatch, payload):
    provider = adapter(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(DomainError, match="未知额度"):
        await provider.account("alice")


async def test_expired_token_returns_reconnect_without_exposing_error_body(monkeypatch):
    provider = adapter(monkeypatch, lambda request: httpx.Response(401, text="secret-body"))
    result = await provider.account("alice")
    assert result.status == "login_required"
    assert "secret-body" not in result.model_dump_json()


async def test_cost_request_uses_exact_video_settings_and_does_not_submit(monkeypatch):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.method == "GET"
        if request.url.path.endswith("claw-pass/status"):
            return httpx.Response(200, json={"has_pass": False})
        assert request.url.path == "/vidu/v1/tasks/credits"
        assert request.url.params["settings.duration"] == "10"
        assert request.url.params["settings.aspect_ratio"] == "9:16"
        assert request.url.params["settings.schedule_mode"] == "normal"
        return httpx.Response(
            200, json={"cost_credits": 30, "current_credits": 10, "can_submit": False}
        )

    result = await adapter(monkeypatch, handler).estimate(
        "alice", VideoConfig(prompt="不能发送到只查询费用的接口", duration=10, aspect_ratio="9:16")
    )
    assert result.reason == "insufficient_credits"
    assert len(paths) == 2


async def test_claw_pass_failure_is_not_assumed_to_be_credit_shortage(monkeypatch):
    def handler(request):
        if request.url.path.endswith("claw-pass/status"):
            return httpx.Response(200, json={"has_pass": True})
        assert request.url.params["settings.schedule_mode"] == "claw_pass"
        return httpx.Response(
            200, json={"cost_credits": 20, "current_credits": 0, "can_submit": False}
        )

    result = await adapter(monkeypatch, handler).estimate("alice", VideoConfig(prompt="产品"))
    assert result.reason == "quota_or_entitlement_blocked"


@pytest.mark.parametrize(
    "payload", [{}, {"cost_credits": 10, "current_credits": 20, "can_submit": "true"}]
)
async def test_incomplete_estimate_does_not_enable_generation(monkeypatch, payload):
    provider = adapter(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(DomainError, match="费用"):
        await provider.estimate("alice", VideoConfig(prompt="产品", schedule_mode="normal"))


def estimate():
    return VideoEstimate(
        cost_credits=20,
        current_credits=100,
        can_submit=True,
        schedule_mode="normal",
        reason="available",
    )


async def test_real_submission_is_disabled_by_default(monkeypatch):
    calls = []
    provider = adapter(monkeypatch, lambda request: calls.append(request))
    with pytest.raises(DomainError, match="尚未启用"):
        await provider.submit("alice", VideoConfig(prompt="产品"), estimate())
    assert calls == []


async def test_submit_protocol_and_success_result_urls(monkeypatch):
    def handler(request):
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["input"]["prompts"] == [{"type": "text", "content": "产品展示"}]
            assert body["settings"]["duration"] == 8
            assert body["settings"]["schedule_mode"] == "normal"
            assert body["type"] == "text2video"
            return httpx.Response(200, json={"id": "task-123"})
        assert request.url.path == "/vidu/v1/tasks/task-123"
        return httpx.Response(
            200,
            json={
                "id": "task-123",
                "state": "success",
                "creations": [
                    {"nomark_uri": "https://media.example/video.mp4"},
                    {"uri": "javascript:alert(1)"},
                    {"uri": "https://user:password@media.example/private"},
                ],
            },
        )

    provider = adapter(monkeypatch, handler, enabled=True)
    submitted = await provider.submit("alice", VideoConfig(prompt="产品展示"), estimate())
    task = await provider.task("alice", submitted.task_id)
    assert task.state == "success"
    assert task.media_urls == ["https://media.example/video.mp4"]


@pytest.mark.parametrize("failure", ["timeout", "redirect", "server_error"])
async def test_post_is_never_retried_or_redirected(monkeypatch, failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private details")
        if failure == "redirect":
            return httpx.Response(302, headers={"location": "https://other.example/steal"})
        return httpx.Response(500, text="secret-body")

    provider = adapter(monkeypatch, handler, enabled=True)
    with pytest.raises(DomainError) as error:
        await provider.submit("alice", VideoConfig(prompt="产品"), estimate())
    assert len(calls) == 1
    assert "secret" not in str(error.value)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "config",
    [
        {"model_version": "3.0", "duration": 10},
        {"model_version": "3.1", "resolution": "720p"},
        {"model_version": "3.1", "transition": "pro"},
        {"duration": True},
        {"prompt": "   "},
    ],
)
def test_invalid_model_configuration_is_rejected(config):
    with pytest.raises(ValidationError):
        VideoConfig(**{"prompt": "产品", **config})


def test_account_binding_only_allows_scoped_env_references_and_no_shared_owner(tmp_path):
    with pytest.raises(ValidationError):
        AccountBinding(token_env="AWS_SECRET_ACCESS_KEY")
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps(
            {
                "alice": {"token_env": "VIDU_ACCOUNT_A_TOKEN"},
                "bob": {"token_env": "VIDU_ACCOUNT_A_TOKEN"},
            }
        )
    )
    with pytest.raises(ValueError, match="must not share"):
        read_accounts(path)
