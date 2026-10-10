import asyncio

import pytest

from vidu_purchase.demo import DemoProvider
from vidu_purchase.models import Checkout, DomainError, PurchaseState
from vidu_purchase.service import CONTINUATION_PROMPT, SUCCESS_MESSAGE, PurchaseService
from vidu_purchase.store import Store


@pytest.fixture
def setup(tmp_path):
    provider = DemoProvider()
    service = PurchaseService(Store(tmp_path / "test.db"), provider, clock=lambda: 1000.0)
    return service, provider


async def quote_for(service, owner="alice"):
    return (await service.list_plans(owner))["quotes"][0]["quote_id"]


async def make_purchase(service, owner="alice", key="request-001"):
    return await service.prepare_purchase(owner, await quote_for(service, owner), key)


async def test_full_flow_asks_to_continue_only_after_payment_and_entitlement(setup):
    service, provider = setup
    result = await make_purchase(service)
    purchase_id = result["purchase_id"]
    purchase = service.store.purchase("alice", purchase_id)
    assert result["state"] == PurchaseState.AWAITING_PAYMENT
    assert result["continuation_prompt"] is None
    assert "成功" not in result["message"]
    assert (await service.check_purchase("alice", purchase_id))["state"] != PurchaseState.READY
    provider.confirm_for_test(purchase, entitlement_active=False)
    pending = await service.check_purchase("alice", purchase_id)
    assert pending["state"] == PurchaseState.PAID_PENDING_ENTITLEMENT
    assert pending["continuation_prompt"] is None
    provider.confirm_for_test(purchase)
    ready = await service.check_purchase("alice", purchase_id)
    assert ready["state"] == PurchaseState.READY
    assert ready["message"].endswith(SUCCESS_MESSAGE)
    assert ready["auto_generate"] is False
    assert ready["continuation_prompt"] == CONTINUATION_PROMPT
    assert ready["requires_generation_confirmation"] is True
    assert ready["requires_new_video_instruction"] is True
    assert "演示" in ready["message"]
    assert "owner" not in ready and "idempotency_key" not in ready
    # The follow-up survives a process restart without reopening the payment or starting a job.
    restarted = PurchaseService(Store(service.store.path), provider)
    assert await restarted.check_purchase("alice", purchase_id) == ready
    assert provider.checkout_count == 1


async def test_duplicate_request_after_restart_does_not_reopen_checkout(setup):
    service, provider = setup
    quote_id = await quote_for(service)
    first = await service.prepare_purchase("alice", quote_id, "same-request")
    restarted = PurchaseService(Store(service.store.path), provider)
    second = await restarted.prepare_purchase("alice", quote_id, "same-request")
    assert first == second
    assert provider.checkout_count == 1


async def test_parallel_calls_create_only_one_purchase(setup):
    service, provider = setup
    quote_id = await quote_for(service)
    results = await asyncio.gather(
        *[service.prepare_purchase("alice", quote_id, "same-request") for _ in range(10)]
    )
    assert len({result["purchase_id"] for result in results}) == 1
    assert provider.checkout_count == 1


async def test_idempotency_key_cannot_be_reused_for_different_quote(setup):
    service, _ = setup
    await make_purchase(service)
    with pytest.raises(DomainError, match="另一份报价"):
        await service.prepare_purchase("alice", await quote_for(service), "request-001")


async def test_users_cannot_access_each_others_quotes_or_purchases(setup):
    service, _ = setup
    quote_id = await quote_for(service)
    with pytest.raises(DomainError, match="当前用户的报价"):
        await service.prepare_purchase("bob", quote_id, "same-request")
    purchase = await service.prepare_purchase("alice", quote_id, "same-request")
    with pytest.raises(DomainError, match="当前用户的购买"):
        await service.check_purchase("bob", purchase["purchase_id"])


async def test_same_key_for_different_users_does_not_collide(setup):
    service, provider = setup
    a = await make_purchase(service, "alice", "same-request")
    b = await make_purchase(service, "bob", "same-request")
    assert a["purchase_id"] != b["purchase_id"]
    assert provider.checkout_count == 2


async def test_expired_quote_never_opens_checkout(setup):
    service, provider = setup
    quote_id = await quote_for(service)
    service.clock = lambda: 1180.0
    with pytest.raises(DomainError, match="已过期"):
        await service.prepare_purchase("alice", quote_id, "new-request")
    assert provider.checkout_count == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner", "someone-else"),
        ("purchase_id", "other-purchase"),
        ("plan_id", "wrong-plan"),
        ("amount_minor", 99),
        ("currency", "CNY"),
    ],
)
async def test_mismatched_receipt_cannot_fulfill(setup, field, value):
    service, provider = setup
    result = await make_purchase(service)
    pid = result["purchase_id"]
    provider.confirm_for_test(service.store.purchase("alice", pid))
    provider.receipts[pid] = provider.receipts[pid].model_copy(update={field: value})
    with pytest.raises(DomainError, match="不一致"):
        await service.check_purchase("alice", pid)
    assert service.store.purchase("alice", pid).state != PurchaseState.READY


async def test_same_receipt_cannot_fulfill_two_purchases(setup):
    service, provider = setup
    first = await make_purchase(service, key="first-request")
    second = await make_purchase(service, key="second-request")
    for result in [first, second]:
        pid = result["purchase_id"]
        provider.confirm_for_test(service.store.purchase("alice", pid))
        provider.receipts[pid] = provider.receipts[pid].model_copy(
            update={"order_reference": "same-official-order"},
        )
    await service.check_purchase("alice", first["purchase_id"])
    with pytest.raises(DomainError, match="不能确认两次"):
        await service.check_purchase("alice", second["purchase_id"])


async def test_network_ambiguity_is_not_retried_automatically(setup):
    service, provider = setup
    calls = []

    async def uncertain(purchase):
        calls.append(purchase.purchase_id)
        raise TimeoutError("Provider may have opened checkout")

    provider.prepare_checkout = uncertain
    quote_id = await quote_for(service)
    first = await service.prepare_purchase("alice", quote_id, "same-request")
    second = await service.prepare_purchase("alice", quote_id, "same-request")
    assert first["state"] == PurchaseState.VERIFICATION_REQUIRED
    assert first == second
    assert len(calls) == 1


async def test_checkout_creation_cannot_claim_payment_success(setup):
    service, provider = setup

    async def invalid(purchase):
        return Checkout(state=PurchaseState.READY, message="paid")

    provider.prepare_checkout = invalid
    result = await make_purchase(service)
    assert result["state"] == PurchaseState.BLOCKED
    assert "invalid_checkout_state" in result["message"]


async def test_unpaid_receipt_does_not_activate_entitlements(setup):
    service, provider = setup
    result = await make_purchase(service)
    pid = result["purchase_id"]
    provider.confirm_for_test(service.store.purchase("alice", pid))
    provider.receipts[pid] = provider.receipts[pid].model_copy(
        update={"payment_succeeded": False},
    )
    assert (await service.check_purchase("alice", pid))["state"] != PurchaseState.READY


async def test_stale_checkout_response_cannot_downgrade_paid_state(setup):
    service, provider = setup
    result = await make_purchase(service)
    pid = result["purchase_id"]
    old = service.store.purchase("alice", pid)
    provider.confirm_for_test(old, entitlement_active=False)
    await service.check_purchase("alice", pid)
    assert service.store.update(old).state == PurchaseState.PAID_PENDING_ENTITLEMENT
    provider.confirm_for_test(old)
    await service.check_purchase("alice", pid)
    assert service.store.update(old).state == PurchaseState.READY


async def test_live_checkout_progress_cannot_masquerade_as_receipt(setup):
    service, provider = setup
    result = await make_purchase(service)

    async def invalid(purchase):
        return Checkout(state=PurchaseState.READY, message="paid")

    provider.check_receipt = invalid
    with pytest.raises(DomainError, match="仅有结账页面"):
        await service.check_purchase("alice", result["purchase_id"])


async def test_status_refresh_does_not_open_another_checkout(setup):
    service, provider = setup
    result = await make_purchase(service)

    async def needs_login(purchase):
        return Checkout(state=PurchaseState.LOGIN_REQUIRED, message="请重新登录")

    provider.check_receipt = needs_login
    refreshed = await service.check_purchase("alice", result["purchase_id"])
    assert refreshed["state"] == PurchaseState.LOGIN_REQUIRED
    assert provider.checkout_count == 1
