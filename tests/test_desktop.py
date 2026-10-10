import json
import time

import httpx
import pytest
from fastmcp import Client

from vidu_purchase.connection import ConnectionManager
from vidu_purchase.credentials import CredentialVault
from vidu_purchase.desktop import create_desktop_server
from vidu_purchase.models import DomainError
from vidu_purchase.video_models import AccountSnapshot
from vidu_purchase.video_provider import WebsiteVideoProvider


class MemoryKeyring:
    """Test fixture only. The distributed entry point selects a native OS keyring."""

    def __init__(self):
        self.values = {}

    def get_password(self, service, owner):
        return self.values.get((service, owner))

    def set_password(self, service, owner, value):
        self.values[service, owner] = value

    def delete_password(self, service, owner):
        del self.values[service, owner]


@pytest.fixture
def vault(tmp_path):
    return CredentialVault(tmp_path, backend=MemoryKeyring())


def test_disconnect_rotates_account_and_invalidates_old_principal(vault):
    first = vault.owner()
    vault.save(first, "test-only-secret", "global")
    assert vault.credential(first)[1] == "test-only-secret"
    with pytest.raises(DomainError, match="账号连接已变化"):
        vault.save(first, "replacement", "cn")
    vault.disconnect()
    second = vault.owner()
    assert second != first
    vault.save(second, "different-account", "cn")
    assert vault.credential(first) is None
    assert vault.credential(second)[0].region == "cn"
    assert "test-only-secret" not in vault.path.read_bytes().decode(errors="ignore")


def test_locked_store_does_not_fall_back_to_plaintext(vault):
    def failure(*_args):
        raise RuntimeError("contains-sensitive-provider-error")

    vault.backend.get_password = failure
    with pytest.raises(DomainError) as raised:
        vault.credential(vault.owner())
    assert "sensitive-provider" not in str(raised.value)


async def validated_account(token, region):
    assert token == "test-only-secret"
    return AccountSnapshot(status="connected", region=region, credits=21, message="test fixture")


def test_loopback_connection_requires_same_origin_and_is_one_use(vault):
    manager = ConnectionManager(vault, validator=validated_account)
    try:
        result = manager.begin()
        assert result["connection_url"] == manager.begin()["connection_url"]
        url = result["connection_url"]
        with httpx.Client(trust_env=False) as client:
            form = client.get(url)
            assert form.status_code == 200
            assert form.headers["cache-control"] == "no-store"
            assert 'type="password"' in form.text
            assert client.get(url, headers={"Host": "attacker.example"}).status_code == 404
            payload = {"token": "test-only-secret", "region": "global"}
            assert client.post(url, data=payload).status_code == 403
            assert (
                client.post(
                    url, data=payload, headers={"Origin": "https://attacker.example"}
                ).status_code
                == 403
            )
            assert vault.credential(vault.owner()) is None
            response = client.post(url, data=payload, headers={"Origin": manager.origin})
            assert response.status_code == 200
            assert "test-only-secret" not in response.text
            assert vault.credential(vault.owner())[1] == "test-only-secret"
            assert (
                client.post(url, data=payload, headers={"Origin": manager.origin}).status_code
                == 403
            )
            assert manager.begin()["status"] == "already_bound"
    finally:
        manager.close()


def test_connection_timeout_and_validation_failure_never_save(vault):
    async def rejected(_token, _region):
        raise DomainError("login_required", "授权无效")

    now = [time.monotonic()]
    manager = ConnectionManager(vault, validator=rejected, clock=lambda: now[0])
    try:
        url = manager.begin()["connection_url"]
        with httpx.Client(trust_env=False) as client:
            assert (
                client.post(
                    url,
                    data={"token": "invalid", "region": "global"},
                    headers={"Origin": manager.origin},
                ).status_code
                == 400
            )
            assert vault.credential(vault.owner()) is None
            now[0] += 901
            assert client.get(url).status_code == 404
            assert (
                client.post(
                    url,
                    data={"token": "invalid", "region": "global"},
                    headers={"Origin": manager.origin},
                ).status_code
                == 403
            )
            assert manager.begin()["connection_url"] != url
    finally:
        manager.close()


def test_disconnect_while_form_open_prevents_binding_to_another_account(vault):
    manager = ConnectionManager(vault, validator=validated_account)
    try:
        url = manager.begin()["connection_url"]
        vault.disconnect()
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                url,
                data={"token": "test-only-secret", "region": "global"},
                headers={"Origin": manager.origin},
            )
            assert response.status_code == 400
        assert vault.credential(vault.owner()) is None
    finally:
        manager.close()


async def test_live_desktop_has_no_demo_or_billing_tools_or_secret_arguments(tmp_path, vault):
    async with Client(create_desktop_server(tmp_path, vault=vault)) as client:
        tools = await client.list_tools()
        assert len(tools) == 9
        assert not any("purchase" in t.name or "plan" in t.name for t in tools)
        for tool in tools:
            properties = tool.input_schema.get("properties", {})
            assert not {"token", "owner", "paid", "api_key", "password"} & set(properties)
        capabilities = (await client.call_tool("vidu_capabilities", {})).data
        assert capabilities["mode"] == "live"
        assert capabilities["commerce_available"] is False
        status = (await client.call_tool("vidu_account_status", {})).data
        assert status["status"] == "not_connected"
        assert status["credits"] is None


async def test_desktop_reads_vault_dynamically_and_uses_region(vault):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"credits": 20})

    video = WebsiteVideoProvider(
        {}, credentials=vault.credential, transport=httpx.MockTransport(handler)
    )
    owner = vault.owner()
    assert (await video.account(owner)).status == "not_connected"
    assert calls == []
    vault.save(owner, "test-only-secret", "cn")
    assert (await video.account(owner)).credits == 20
    assert str(calls[0].url) == "https://service.vidu.cn/credit/v1/credits/me"
    assert calls[0].headers["Authorization"] == "Token test-only-secret"
    vault.disconnect()
    assert (await video.account(owner)).status == "not_connected"
    assert len(calls) == 1


async def test_browser_authorization_reaches_running_plugin_and_disconnects_both(
    tmp_path, vault, monkeypatch
):
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"credits": 20})

    original = WebsiteVideoProvider.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(WebsiteVideoProvider, "__init__", init)
    browser_vault = CredentialVault(tmp_path, backend=vault.backend)
    async with Client(create_desktop_server(tmp_path, vault=vault)) as client:
        assert (await client.call_tool("vidu_account_status", {})).data["status"] == "not_connected"
        browser_vault.save_browser(
            browser_vault.owner(), "test-browser-grant", "vidu_TEST1234", "a" * 64
        )
        assert (await client.call_tool("vidu_account_status", {})).data["credits"] == 20
        assert requests[0].headers["Authorization"] == "Token test-browser-grant"
        previous_owner = browser_vault.owner()
        await client.call_tool("vidu_disconnect_account", {})
        assert browser_vault.owner() != previous_owner
        assert browser_vault.credential(previous_owner) is None
        assert browser_vault.credential(browser_vault.owner()) is None
        assert (await client.call_tool("vidu_account_status", {})).data["status"] == "not_connected"
        assert len(requests) == 1


async def test_desktop_requests_always_require_generation_confirmation(
    tmp_path, vault, monkeypatch
):
    # This unit test verifies the confirmation invariant, not real Vidu availability.
    async def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"credits": 20})
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"has_pass": False})
        return httpx.Response(
            200, json={"cost_credits": 10, "current_credits": 20, "can_submit": True}
        )

    original = WebsiteVideoProvider.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(WebsiteVideoProvider, "__init__", init)
    vault.save(vault.owner(), "test-only-secret", "global")
    async with Client(create_desktop_server(tmp_path, vault=vault)) as client:
        workflow = (
            await client.call_tool(
                "vidu_start_video_request",
                {"config": {"prompt": "unit test"}, "idempotency_key": "request-once-only"},
            )
        ).data
        assert workflow["next_action"] == "ask_generation_confirmation"
        assert workflow["auto_generate"] is False
        assert "test-only-secret" not in json.dumps(workflow)
        workflow = (
            await client.call_tool("vidu_get_workflow", {"workflow_id": workflow["workflow_id"]})
        ).data
        assert workflow["task"] is None


async def test_connection_validator_rejects_unauthenticated_account(monkeypatch):
    async def no_login(*_args):
        return AccountSnapshot(status="login_required", message="not connected")

    monkeypatch.setattr(WebsiteVideoProvider, "account", no_login)
    with pytest.raises(DomainError, match="Token 无效"):
        await ConnectionManager.validate("invalid", "global")
