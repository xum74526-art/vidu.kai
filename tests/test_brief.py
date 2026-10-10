"""Configuration conversation tests; upstream responses are isolated protocol fixtures."""

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from test_hosted import rest, running

from vidu_purchase.brief import BriefService
from vidu_purchase.brief_models import VIDEO_FIELDS, BriefAnswers, BriefInput, VideoPreferences
from vidu_purchase.models import DomainError
from vidu_purchase.video_models import VideoConfig


def service(system):
    return system[0].state.agent_api.briefs


async def begin(system, **kwargs):
    return await service(system).start(
        "alice", BriefInput(prompt="海边日落短片", **kwargs), "brief-001"
    )


async def test_missing_values_are_one_question_without_upstream_calls(protocol_system):
    brief = await begin(protocol_system)
    assert brief["state"] == "needs_clarification"
    assert [item["field"] for item in brief["questions"]] == list(VIDEO_FIELDS)
    assert all(label in brief["question"] for label in ("模型", "时长", "比例", "分辨率"))
    assert brief["config"] is None and brief["workflow_id"] is None
    assert brief["recommended_fields"] == {}
    assert protocol_system[1].calls == []


async def test_delegation_preserves_explicit_choice_and_requires_generation_confirmation(
    protocol_system,
):
    fixture = protocol_system[1]
    fixture.credits = 80
    brief = await begin(protocol_system, preferences=VideoPreferences(aspect_ratio="4:3"))
    answer = BriefAnswers(revision=brief["revision"], use_recommended_for=list(VIDEO_FIELDS))
    ready = await service(protocol_system).answer("alice", brief["brief_id"], answer)
    assert ready["config"]["aspect_ratio"] == "4:3"
    assert ready["config"]["duration"] == 8
    assert ready["config"]["model_version"] == "3.2"
    assert ready["config"]["resolution"] == "1080p"
    assert "aspect_ratio" not in ready["recommended_fields"]
    assert ready["workflow"]["state"] == "awaiting_generation_confirmation"
    assert ready["workflow"]["config"] == ready["config"]
    assert fixture.post_count("/vidu/v1/tasks") == 0
    assert fixture.post_count("/v1/checkouts") == 0
    repeated = await service(protocol_system).answer("alice", brief["brief_id"], answer)
    assert repeated["workflow_id"] == ready["workflow_id"]
    assert repeated["revision"] == ready["revision"]


@pytest.mark.parametrize(
    ("use", "ratio"),
    [
        ("general", "16:9"),
        ("vertical_short", "9:16"),
        ("landscape", "16:9"),
        ("square", "1:1"),
    ],
)
async def test_recommendation_uses_stated_purpose(protocol_system, use, ratio):
    ready = await begin(protocol_system, use_recommended_for=list(VIDEO_FIELDS), intended_use=use)
    assert ready["config"]["aspect_ratio"] == ratio
    assert ready["workflow"]["requires_continuation_confirmation"] is True


@pytest.mark.parametrize(
    ("preferences", "expected"),
    [
        ({"model_version": "3.0"}, {"model_version": "3.0", "duration": 5, "resolution": "1080p"}),
        ({"resolution": "720p"}, {"model_version": "3.2_a", "duration": 8, "resolution": "720p"}),
        ({"duration": 16}, {"model_version": "3.2", "duration": 16, "resolution": "1080p"}),
    ],
)
async def test_recommendations_respect_supported_combinations(
    protocol_system, preferences, expected
):
    brief = await begin(
        protocol_system,
        preferences=VideoPreferences(**preferences),
        use_recommended_for=list(VIDEO_FIELDS),
    )
    assert all(brief["config"][key] == value for key, value in expected.items())


async def test_conflicts_preserve_choices_and_do_not_create_workflow(protocol_system):
    brief = await begin(
        protocol_system,
        preferences=VideoPreferences(model_version="3.0", duration=8),
        use_recommended_for=list(VIDEO_FIELDS),
    )
    assert brief["state"] == "needs_clarification"
    assert brief["conflicts"][0]["code"] == "incompatible_video_options"
    assert brief["resolved_parameters"]["duration"] == 8
    assert brief["config"] is None
    assert protocol_system[1].calls == []
    ready = await service(protocol_system).answer(
        "alice",
        brief["brief_id"],
        BriefAnswers(
            revision=brief["revision"],
            preferences=VideoPreferences(duration=5),
        ),
    )
    assert ready["config"]["model_version"] == "3.0"
    assert ready["config"]["duration"] == 5


async def test_specific_delegation_does_not_choose_other_fields(protocol_system):
    brief = await begin(protocol_system, use_recommended_for=["model_version"])
    assert set(brief["resolved_parameters"]) == {"model_version"}
    assert {item["field"] for item in brief["questions"]} == {
        "duration",
        "resolution",
        "aspect_ratio",
    }
    reset = await service(protocol_system).answer(
        "alice",
        brief["brief_id"],
        BriefAnswers(
            revision=brief["revision"],
            preferences=VideoPreferences(model_version=None),
        ),
    )
    assert len(reset["questions"]) == 4
    assert reset["use_recommended_for"] == []
    assert protocol_system[1].calls == []


async def test_restart_ownership_idempotency_and_stale_answer(protocol_system):
    brief = await begin(protocol_system, preferences=VideoPreferences(aspect_ratio="9:16"))
    restored_service = BriefService(service(protocol_system).workflow)
    restored = await restored_service.get("alice", brief["brief_id"])
    assert restored == brief
    with pytest.raises(DomainError, match="未找到"):
        await restored_service.get("bob", brief["brief_id"])
    with pytest.raises(DomainError) as conflict:
        await restored_service.start("alice", BriefInput(prompt="另一个需求"), "brief-001")
    assert conflict.value.code == "idempotency_conflict"
    current = await restored_service.answer(
        "alice",
        brief["brief_id"],
        BriefAnswers(
            revision=brief["revision"],
            preferences=VideoPreferences(duration=10),
        ),
    )
    assert current["preferences"]["aspect_ratio"] == "9:16"
    with pytest.raises(DomainError) as stale:
        await restored_service.answer(
            "alice",
            brief["brief_id"],
            BriefAnswers(
                revision=brief["revision"],
                preferences=VideoPreferences(duration=6),
            ),
        )
    assert stale.value.code == "brief_changed"
    assert (await restored_service.get("alice", brief["brief_id"]))["preferences"]["duration"] == 10


async def test_lost_handoff_response_recovers_one_workflow(protocol_system, monkeypatch):
    briefs = service(protocol_system)
    original_save = briefs.save

    def fail_link(brief, **changes):
        if "workflow_id" in changes:
            raise RuntimeError("process interrupted after workflow persisted")
        return original_save(brief, **changes)

    monkeypatch.setattr(briefs, "save", fail_link)
    request = BriefInput(prompt="重启后保留描述", use_recommended_for=list(VIDEO_FIELDS))
    with pytest.raises(RuntimeError):
        await briefs.start("alice", request, "lost-response")
    resumed = BriefService(briefs.workflow)
    ready = await resumed.start("alice", request, "lost-response")
    assert ready["workflow"]["config"]["prompt"] == request.prompt
    with briefs.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM video_workflows").fetchone()[0] == 1
    assert protocol_system[1].post_count("/vidu/v1/tasks") == 0


async def test_rest_and_mcp_share_brief_and_enforce_identity(protocol_system):
    app, fixture, *_ = protocol_system
    async with running(app) as url:
        async with Client(url + "/mcp", auth="alice") as mcp:
            brief = (
                await mcp.call_tool(
                    "vidu_prepare_video_request",
                    {
                        "request": {
                            "prompt": "给短视频平台做一段海景",
                            "intended_use": "vertical_short",
                        },
                        "idempotency_key": "mcp-brief-001",
                    },
                )
            ).data
        async with rest(app) as http:
            current = (await http.get(f"/v1/video-briefs/{brief['brief_id']}")).json()
            assert current["brief_id"] == brief["brief_id"]
            response = await http.patch(
                f"/v1/video-briefs/{brief['brief_id']}",
                json={
                    "revision": current["revision"],
                    "use_recommended_for": list(VIDEO_FIELDS),
                },
            )
            assert response.status_code == 200
            ready = response.json()
            assert ready["config"]["aspect_ratio"] == "9:16"
        async with Client(url + "/mcp", auth="bob") as other:
            with pytest.raises(ToolError, match="brief_not_found"):
                await other.call_tool("vidu_get_video_brief", {"brief_id": brief["brief_id"]})
        async with rest(app, "purchase-only") as limited:
            assert (await limited.get(f"/v1/video-briefs/{brief['brief_id']}")).status_code == 403
        assert fixture.post_count("/vidu/v1/tasks") == 0


async def test_brief_to_payment_stops_at_fresh_continuation_confirmation(protocol_system):
    app, fixture, *_ = protocol_system
    async with rest(app) as client:
        response = await client.post(
            "/v1/video-briefs",
            json={
                "prompt": "竖屏海景",
                "intended_use": "vertical_short",
            },
            headers={"Idempotency-Key": "conversation-payment"},
        )
        assert response.status_code == 201
        brief = response.json()
        ready = (
            await client.patch(
                f"/v1/video-briefs/{brief['brief_id']}",
                json={
                    "revision": brief["revision"],
                    "use_recommended_for": list(VIDEO_FIELDS),
                },
            )
        ).json()
        path = f"/v1/videos/{ready['workflow_id']}"
        assert ready["workflow"]["state"] == "needs_purchase"
        offers = (await client.post(path + "/recommend")).json()
        pending = (
            await client.post(
                path + "/purchase",
                json={
                    "quote_id": offers["recommended_quote_id"],
                    "revision": offers["revision"],
                },
            )
        ).json()
        assert pending["payment_action"]["type"] == "open_official_checkout"
        assert pending["payment_action"]["embedded"] is False
        assert pending["purchase"]["plan"]["amount_minor"] == fixture.plan["amount_minor"]
        assert "account_ref" not in pending["purchase"]["plan"]
        fixture.payment = 1
        assert (await client.get(path)).json()["state"] == "awaiting_entitlement"
        fixture.payment, fixture.credits = 2, 80
        paid = (await client.get(path)).json()
        assert paid["continuation_prompt"] == "是否继续生成刚才的视频？"
        assert paid["payment_action"] is None
        assert paid["config"] == ready["config"]
        assert fixture.post_count("/vidu/v1/tasks") == 0
        assert fixture.post_count("/v1/checkouts") == 1


async def test_brief_input_rejects_forged_payment_and_invalid_types(protocol_system):
    async with rest(protocol_system[0]) as client:
        for extra in (
            {"owner": "bob"},
            {"payment_succeeded": True},
            {"preferences": {"duration": True}},
        ):
            response = await client.post(
                "/v1/video-briefs",
                json={"prompt": "海景", **extra},
                headers={"Idempotency-Key": "bad-input-test"},
            )
            assert response.status_code == 422
        assert protocol_system[1].calls == []


async def test_direct_hosted_calls_cannot_bypass_missing_parameters(protocol_system):
    app = protocol_system[0]
    async with rest(app) as client:
        response = await client.post(
            "/v1/videos",
            json={"config": {"prompt": "海景"}},
            headers={"Idempotency-Key": "missing-parameters"},
        )
        assert response.status_code == 422
    async with running(app) as url, Client(url + "/mcp", auth="alice") as mcp:
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        schema = tools["vidu_start_video_request"].input_schema
        assert set(VIDEO_FIELDS) <= set(schema["properties"]["config"]["required"])
        with pytest.raises(ToolError):
            await mcp.call_tool(
                "vidu_start_video_request",
                {
                    "config": {"prompt": "海景"},
                    "idempotency_key": "missing-parameters",
                },
            )
    assert protocol_system[1].calls == []


async def test_restored_brief_returns_updated_workflow_configuration(protocol_system):
    brief = await begin(protocol_system, use_recommended_for=list(VIDEO_FIELDS))
    config = VideoConfig.model_validate({**brief["config"], "duration": 12, "aspect_ratio": "9:16"})
    flow = brief["workflow"]
    await service(protocol_system).workflow.update(
        "alice",
        flow["workflow_id"],
        config,
        flow["revision"],
    )
    current = await service(protocol_system).get("alice", brief["brief_id"])
    assert current["config"] == config.model_dump()
    assert current["resolved_parameters"]["duration"] == 12
    assert "duration" not in current["recommended_fields"]
