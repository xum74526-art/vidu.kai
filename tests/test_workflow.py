import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from vidu_purchase.demo import DemoProvider
from vidu_purchase.demo_video import DemoVideoProvider
from vidu_purchase.models import DomainError
from vidu_purchase.server import create_server
from vidu_purchase.service import PurchaseService
from vidu_purchase.store import Store
from vidu_purchase.video_models import AccountSnapshot, VideoConfig, VideoEstimate, WorkflowState
from vidu_purchase.workflow import WorkflowService


@pytest.fixture
def system(tmp_path):
    billing, video = DemoProvider(), DemoVideoProvider()
    now = [1000.0]
    purchases = PurchaseService(Store(tmp_path / "workflow.db"), billing, clock=lambda: now[0])
    workflow = WorkflowService(purchases, video, clock=lambda: now[0])
    return workflow, video, billing, now


async def start(workflow, owner="alice", key="video-request-001"):
    return await workflow.start(owner, VideoConfig(prompt="产品缓慢旋转", duration=10), key)


async def test_status_polling_does_not_extend_the_original_generation_confirmation(system):
    workflow, video, _, now = system
    video.connect_for_test("alice", credits=100)
    first = await start(workflow)
    confirmation = first["confirmation_id"]
    expires = first["estimate_expires_at"]
    for step in (30, 90, 179):
        now[0] = 1000 + step
        current = await workflow.get("alice", first["workflow_id"])
        assert current["confirmation_id"] == confirmation
        assert current["estimate_expires_at"] == expires
    now[0] = 1181
    expired = await workflow.confirm("alice", first["workflow_id"], confirmation)
    assert expired["confirmation_id"] != confirmation
    assert expired["state"] == "awaiting_generation_confirmation"
    assert video.submission_count == 0


async def test_account_switch_while_rechecking_cost_cannot_submit_to_new_account(system):
    workflow, video, _, _ = system
    binding = ["account-a"]
    workflow.binding_revision = lambda _owner: binding[0]
    video.connect_for_test("alice", credits=100)
    first = await start(workflow)
    previous_estimate = video.estimate

    async def switch_while_estimating(owner, config):
        result = await previous_estimate(owner, config)
        binding[0] = "account-b"
        return result

    video.estimate = switch_while_estimating
    with pytest.raises(DomainError, match="账号连接已更换"):
        await workflow.confirm("alice", first["workflow_id"], first["confirmation_id"])
    assert video.submission_count == 0


async def buy(workflow, wid):
    plans = await workflow.recommend("alice", wid)
    return await workflow.buy("alice", wid, plans["recommended_quote_id"], plans["revision"])


async def test_account_purchase_confirm_generate_end_to_end_through_mcp(system):
    workflow, video, billing, _ = system
    async with Client(
        create_server(workflow.purchases, local_owner="alice", workflow=workflow)
    ) as client:

        async def call(name, **args):
            return (await client.call_tool(name, args)).data

        first = await call(
            "vidu_start_video_request",
            config={"prompt": "产品缓慢旋转", "duration": 10},
            idempotency_key="mcp-first-video",
        )
        wid = first["workflow_id"]
        assert first["state"] == "account_required"
        assert first["next_action"] == "login_or_register"
        assert first["config"]["duration"] == 10
        assert "没有账号" not in first["message"]
        video.connect_for_test("alice")
        assert (await call("vidu_get_workflow", workflow_id=wid))["state"] == "needs_purchase"
        plans = await call("vidu_recommend_plans", workflow_id=wid)
        args = {
            "workflow_id": wid,
            "quote_id": plans["recommended_quote_id"],
            "revision": plans["revision"],
        }
        pending = await call("vidu_purchase_for_video", **args)
        assert pending["state"] == "awaiting_payment"
        repeated = await call("vidu_purchase_for_video", **args)
        assert repeated["purchase_id"] == pending["purchase_id"]
        assert billing.checkout_count == 1
        for _ in range(2):
            assert (await call("vidu_get_workflow", workflow_id=wid))["continuation_prompt"] is None
        purchase = workflow.purchases.store.purchase("alice", pending["purchase_id"])
        billing.confirm_for_test(purchase, entitlement_active=False)
        awaiting = await call("vidu_get_workflow", workflow_id=wid)
        assert awaiting["state"] == "awaiting_entitlement"
        assert awaiting["confirmation_id"] is None
        with pytest.raises(ToolError, match="confirmation_required"):
            await call("vidu_confirm_generation", workflow_id=wid, confirmation_id="pretend-paid")
        billing.confirm_for_test(purchase)
        video.connect_for_test("alice", credits=100)
        ready = await call("vidu_get_workflow", workflow_id=wid)
        assert ready["state"] == "awaiting_generation_confirmation"
        assert ready["continuation_prompt"] == "是否继续生成刚才的视频？"
        assert "购买成功" in ready["message"]
        assert video.submission_count == 0
        job = await call(
            "vidu_confirm_generation", workflow_id=wid, confirmation_id=ready["confirmation_id"]
        )
        assert job["state"] == "generating"
        assert video.submission_count == 1
        assert job["config"] == first["config"]
        video.complete_for_test(job["task"]["task_id"])
        completed = await call("vidu_get_workflow", workflow_id=wid)
        assert completed["state"] == "completed"
        assert completed["task"]["media_urls"] == []  # No fake playable video.
        assert "演示" in completed["message"]
        assert video.submission_count == 1


async def test_sufficient_quota_skips_purchase_and_reads_never_submit(system):
    workflow, video, billing, _ = system
    video.connect_for_test("alice", credits=100)
    ready = await start(workflow)
    assert ready["state"] == "awaiting_generation_confirmation"
    assert ready["next_action"] == "submit_authorized_generation"
    with pytest.raises(DomainError, match="不需要"):
        await workflow.recommend("alice", ready["workflow_id"])
    assert billing.checkout_count == video.submission_count == 0


async def test_restart_retains_prompt_confirmation_and_prevents_duplicate_submit(system):
    workflow, video, _, _ = system
    video.connect_for_test("alice", credits=100)
    ready = await start(workflow)
    restarted = WorkflowService(workflow.purchases, video, clock=workflow.clock)
    resumed = await restarted.get("alice", ready["workflow_id"])
    assert resumed["config"] == ready["config"]
    assert resumed["confirmation_id"] == ready["confirmation_id"]
    job = await restarted.confirm("alice", ready["workflow_id"], ready["confirmation_id"])
    repeated = await workflow.confirm("alice", ready["workflow_id"], ready["confirmation_id"])
    assert repeated["task"] == job["task"]
    assert video.submission_count == 1


async def test_different_workers_cannot_submit_the_same_video_twice(system):
    workflow, video, _, _ = system
    video.connect_for_test("alice", credits=100)
    ready = await start(workflow)
    second = WorkflowService(workflow.purchases, video, clock=workflow.clock)
    results = await asyncio.gather(
        workflow.confirm("alice", ready["workflow_id"], ready["confirmation_id"]),
        second.confirm("alice", ready["workflow_id"], ready["confirmation_id"]),
        return_exceptions=True,
    )
    assert video.submission_count == 1
    for result in results:
        assert isinstance(result, dict) or isinstance(result, DomainError)


async def test_parameter_edit_invalidates_previous_confirmation(system):
    workflow, video, _, _ = system
    video.connect_for_test("alice", credits=100)
    original = await start(workflow)
    revised = await workflow.update(
        "alice",
        original["workflow_id"],
        VideoConfig(prompt="改成蓝色背景", duration=8),
        original["revision"],
    )
    assert revised["confirmation_id"] != original["confirmation_id"]
    with pytest.raises(DomainError, match="明确"):
        await workflow.confirm("alice", original["workflow_id"], original["confirmation_id"])
    with pytest.raises(DomainError, match="版本"):
        await workflow.update(
            "alice", original["workflow_id"], VideoConfig(prompt="旧配置"), original["revision"]
        )
    assert video.submission_count == 0


@pytest.mark.parametrize("change", ["cost", "expiry", "balance", "login"])
async def test_changed_conditions_do_not_spend_under_old_confirmation(system, change):
    workflow, video, _, now = system
    video.connect_for_test("alice", credits=100)
    ready = await start(workflow)
    if change == "cost":
        video.unit_cost += 1
    elif change == "expiry":
        now[0] += 181
    elif change == "balance":
        video.credits["alice"] = 0
    else:
        del video.credits["alice"]
    latest = await workflow.confirm("alice", ready["workflow_id"], ready["confirmation_id"])
    assert latest["confirmation_id"] != ready["confirmation_id"]
    assert video.submission_count == 0


async def test_user_can_decline_after_payment_without_losing_purchase(system):
    workflow, video, billing, _ = system
    video.connect_for_test("alice")
    request = await start(workflow)
    pending = await buy(workflow, request["workflow_id"])
    purchase = workflow.purchases.store.purchase("alice", pending["purchase_id"])
    billing.confirm_for_test(purchase)
    video.connect_for_test("alice", credits=100)
    ready = await workflow.get("alice", request["workflow_id"])
    ended = await workflow.cancel("alice", request["workflow_id"], ready["revision"])
    assert ended["state"] == "cancelled"
    assert workflow.purchases.store.purchase("alice", pending["purchase_id"]).state == "ready"
    with pytest.raises(DomainError, match="明确"):
        await workflow.confirm("alice", request["workflow_id"], ready["confirmation_id"])
    assert video.submission_count == 0


async def test_uncertain_submit_and_crashed_submit_are_never_retried(system):
    workflow, video, _, _ = system
    video.connect_for_test("alice", credits=100)
    ready = await start(workflow)
    calls = []

    async def uncertain(*args):
        calls.append(args)
        raise TimeoutError("Contains private upstream response")

    video.submit = uncertain
    result = await workflow.confirm("alice", ready["workflow_id"], ready["confirmation_id"])
    assert result["state"] == "submission_unknown"
    assert "private" not in str(result)
    restarted = WorkflowService(workflow.purchases, video)
    await restarted.confirm("alice", ready["workflow_id"], ready["confirmation_id"])
    await restarted.get("alice", ready["workflow_id"])
    assert len(calls) == 1
    crashed = await start(workflow, key="crashed-request-002")
    workflow.store.save(
        workflow.load("alice", crashed["workflow_id"]),
        state=WorkflowState.SUBMITTING,
        confirmation_id=None,
    )
    await restarted.confirm("alice", crashed["workflow_id"], crashed["confirmation_id"])
    assert len(calls) == 1


async def test_interrupted_purchase_response_recovers_same_checkout(system):
    workflow, video, billing, _ = system
    video.connect_for_test("alice")
    request = await start(workflow)
    plans = await workflow.recommend("alice", request["workflow_id"])
    saved = workflow.save(
        workflow.load("alice", request["workflow_id"]),
        purchase_quote_id=plans["recommended_quote_id"],
        purchase_attempt=1,
        state=WorkflowState.PREPARING_PURCHASE,
    )
    order = await workflow.purchases.prepare_purchase(
        "alice", saved.purchase_quote_id, workflow.purchase_key(saved)
    )
    recovered = await workflow.get("alice", request["workflow_id"])
    assert recovered["purchase_id"] == order["purchase_id"]
    assert billing.checkout_count == 1


async def test_user_isolation_and_request_identity(system):
    workflow, _, _, _ = system
    request = await start(workflow)
    with pytest.raises(DomainError, match="当前用户"):
        await workflow.get("bob", request["workflow_id"])
    with pytest.raises(DomainError, match="其他配置"):
        await workflow.start("alice", VideoConfig(prompt="不同的视频"), "video-request-001")
    assert (await start(workflow))["workflow_id"] == request["workflow_id"]
    bob = await start(workflow, "bob")
    assert bob["workflow_id"] != request["workflow_id"]


async def test_unknown_quota_failure_does_not_become_a_purchase_recommendation(system):
    workflow, video, _, _ = system
    video.connect_for_test("alice")

    async def quota_blocked(*args):
        return VideoEstimate(
            cost_credits=0,
            current_credits=0,
            can_submit=False,
            schedule_mode="claw_pass",
            reason="quota_or_entitlement_blocked",
        )

    video.estimate = quota_blocked
    request = await start(workflow)
    assert request["state"] == "blocked"
    assert request["next_action"] == "none"


async def test_commerce_disabled_does_not_expose_purchase_tools_or_upsell(system):
    workflow, video, _, _ = system
    restricted = WorkflowService(workflow.purchases, video, commerce_enabled=False)
    video.connect_for_test("alice")
    async with Client(
        create_server(
            workflow.purchases, workflow=restricted, local_owner="alice", commerce_enabled=False
        )
    ) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert "vidu_recommend_plans" not in names
        assert "vidu_purchase_for_video" not in names
        assert "vidu_prepare_purchase" not in names
        assert "vidu_confirm_generation" in names
        request = (
            await client.call_tool(
                "vidu_start_video_request",
                {"config": {"prompt": "产品展示"}, "idempotency_key": "restricted-video"},
            )
        ).data
        assert request["state"] == "blocked"
        assert request["next_action"] != "compare_plans"


async def test_mainland_account_does_not_enter_global_website_checkout(system):
    workflow, video, billing, _ = system

    async def mainland(owner):
        return AccountSnapshot(status="connected", region="cn", credits=0, message="国内站账号")

    video.account = mainland
    request = await start(workflow)
    assert request["state"] == "blocked"
    with pytest.raises(DomainError, match="不需要"):
        await workflow.recommend("alice", request["workflow_id"])
    assert billing.checkout_count == 0
