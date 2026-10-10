import asyncio

import httpx
import pytest

from vidu_purchase.models import DomainError
from vidu_purchase.service import PurchaseService


async def purchase(system):
    service = system[0].state.agent_api.purchases
    quote = (await service.list_plans("alice"))["quotes"][0]["quote_id"]
    result = await service.prepare_purchase("alice", quote, "fixed-request-001")
    return service, quote, result


async def test_checkout_timeout_recovers_by_request_without_another_post(protocol_system):
    fixture = protocol_system[1]
    fixture.checkout_timeout = True
    service, quote, first = await purchase(protocol_system)
    assert first["state"] == "verification_required"
    restarted = PurchaseService(service.store, service.provider)
    repeated = await restarted.prepare_purchase("alice", quote, "fixed-request-001")
    assert repeated == first
    fixture.payment = 2
    ready = await restarted.check_purchase("alice", first["purchase_id"])
    assert ready["state"] == "ready"
    assert fixture.post_count("/v1/checkouts") == 1
    assert "private-token" not in str(ready)


async def test_concurrent_http_provider_checkout_is_sent_once(protocol_system):
    service = protocol_system[0].state.agent_api.purchases
    quote = (await service.list_plans("alice"))["quotes"][0]["quote_id"]
    results = await asyncio.gather(
        *[service.prepare_purchase("alice", quote, "concurrent-request") for _ in range(8)]
    )
    assert len({item["purchase_id"] for item in results}) == 1
    assert protocol_system[1].post_count("/v1/checkouts") == 1


@pytest.mark.parametrize(
    "change",
    [
        {"checkout_url": "https://evil.example/pay"},
        {"checkout_url": "https://checkout.stripe.com/c/pay/cs_test_Fixture"},
        {"checkout_reference": "different-reference"},
        {"renewal_amount_minor": 9999},
        {"recurring": False},
        {"amount_minor": 988},
        {"currency": "CNY"},
        {"account_ref": "other-person"},
    ],
)
async def test_unverified_checkout_is_not_presented_for_payment(protocol_system, change):
    protocol_system[1].checkout_changes = change
    _, _, result = await purchase(protocol_system)
    assert result["state"] == "verification_required" and result["handoff_url"] is None
    assert protocol_system[1].post_count("/v1/checkouts") == 1


@pytest.mark.parametrize(
    "change",
    [
        {"account_ref": "other-person"},
        {"request_id": "another-request"},
        {"plan_id": "other-plan"},
        {"currency": "CNY"},
        {"amount_minor": 988},
        {"payment_succeeded": "true"},
        {"entitlement_active": 1},
    ],
)
async def test_untrusted_receipt_fields_never_fulfill(protocol_system, change):
    service, _, pending = await purchase(protocol_system)
    protocol_system[1].payment = 2
    protocol_system[1].order_changes = change
    with pytest.raises(DomainError):
        await service.check_purchase("alice", pending["purchase_id"])
    assert service.store.purchase("alice", pending["purchase_id"]).state != "ready"


@pytest.mark.parametrize(
    "field", ["currency", "billing_cycle", "recurring", "renewal_amount_minor"]
)
async def test_catalog_cannot_guess_missing_price_or_renewal_terms(protocol_system, field):
    del protocol_system[1].plan[field]
    with pytest.raises(DomainError, match="套餐字段"):
        await protocol_system[-1].list_plans("alice")


async def test_changed_account_binding_cannot_use_a_previous_quote(protocol_system):
    service = protocol_system[0].state.agent_api.purchases
    quote = (await service.list_plans("alice"))["quotes"][0]["quote_id"]
    protocol_system[-1].settings.account_refs["alice"] = "new-account"
    result = await service.prepare_purchase("alice", quote, "changed-account-request")
    assert result["state"] == "blocked"
    assert protocol_system[1].post_count("/v1/checkouts") == 0


async def test_gateway_does_not_follow_redirects_or_disclose_response_secrets(protocol_system):
    gateway = protocol_system[-1]
    calls = []

    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://evil.example/secret"})

    gateway.transport = httpx.MockTransport(redirect)
    with pytest.raises(DomainError, match="未确认"):
        await gateway.list_plans("alice")
    assert len(calls) == 1
