"""Isolated DOM fixtures for failure handling; not live payment acceptance tests."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from playwright.async_api import TimeoutError as BrowserTimeout
from test_hosted import rest, running

from vidu_purchase.browser import (
    ORDER_HEADERS,
    SUBSCRIPTION_URL,
    BrowserBinding,
    ViduBrowserProvider,
)
from vidu_purchase.hosted import create_hosted_app
from vidu_purchase.models import Checkout, DomainError, PurchaseState
from vidu_purchase.service import PurchaseService
from vidu_purchase.store import Store
from vidu_purchase.video_provider import WebsiteVideoProvider

CARDS = [
    "标准版 $7/月 下个月续费金额：$10 立即订阅 每月800积分",
    "专业版 $26/月 下个月续费金额：$43 立即订阅 每月3500积分",
    "旗舰版 $138/月 下个月续费金额：$230 立即订阅 每月20,000积分",
]


class VisibleFixture:
    """The adapter gets only locator APIs and observed visible fields, no credentials."""

    def __init__(self):
        self.url = SUBSCRIPTION_URL
        self.handle = "vidu_ALICE1234"
        self.login = False
        self.login_choice = False
        self.cards = list(CARDS)
        self.review = False
        self.orders_open = False
        self.order_rows = [list(ORDER_HEADERS), ["暂无数据"]]
        self.review_text = "订阅 原价 $10 实付金额 $7 立即订阅"
        self.checkout_text = "订阅 Vidu Standard\nUS$7.00\n然后从下个月开始每月 US$10.00"
        self.created = 0
        self.selection = 0
        self.context = SimpleNamespace(pages=[self])

    def get_by_role(self, role, name=None, exact=None):
        return Locator(self, role, name)

    def get_by_text(self, text, exact=None):
        return Locator(self, "text", text)

    def locator(self, selector):
        return Locator(self, "selector", selector)

    @asynccontextmanager
    async def expect_popup(self, timeout=None):
        popup = SimpleNamespace(
            url="https://checkout.stripe.com/f/pay/cs_live_isolatedfixture",
            wait_for_load_state=AsyncMock(),
            get_by_role=lambda *a, **kw: SimpleNamespace(wait_for=AsyncMock()),
            locator=lambda *a: SimpleNamespace(
                inner_text=AsyncMock(return_value=self.checkout_text)
            ),
        )
        future = asyncio.get_running_loop().create_future()
        future.set_result(popup)
        yield SimpleNamespace(value=future)
        self.context.pages.append(popup)


class Locator:
    def __init__(self, page, role, name, in_dialog=False, index=0):
        self.page, self.role, self.name = page, role, name
        self.in_dialog, self.index = in_dialog, index

    def filter(self, **_kwargs):
        return self

    def get_by_role(self, role, name=None, exact=None):
        return Locator(self.page, role, name, in_dialog=True)

    def locator(self, selector):
        return Locator(self.page, "selector", selector, in_dialog=True)

    def nth(self, index):
        return Locator(self.page, self.role, self.name, index=index)

    @property
    def first(self):
        return self

    async def wait_for(self, **_kwargs):
        return None

    async def is_visible(self):
        return {
            "Close login dialog": self.page.login,
            "登录": self.page.login,
            "使用邮箱注册/登录": self.page.login_choice,
            "订阅": self.page.review,
            "购买记录": self.page.orders_open,
            "包月套餐": True,
        }.get(self.name, False)

    async def is_enabled(self):
        return True

    async def evaluate_all(self, _code):
        if self.name == "header img[alt], div.cursor-pointer > img[alt]":
            return [self.page.handle]
        if self.name == "table tr":
            return self.page.order_rows
        assert self.name == "立即订阅"
        return self.page.cards

    async def inner_text(self):
        assert self.role == "dialog"
        return self.page.review_text

    async def click(self):
        assert self.name in {"套餐", "包月套餐", "立即订阅", "我的订单", "Close"}
        if self.name == "立即订阅":
            if self.in_dialog:
                self.page.created += 1
                self.page.review = False
            else:
                self.page.selection = self.index
                self.page.review = True
        elif self.name == "我的订单":
            self.page.orders_open = True
        elif self.name == "Close":
            self.page.orders_open = False

    async def press(self, key):
        assert self.role == "dialog" and key == "Escape"
        self.page.orders_open = False


@pytest.fixture
def browser_system(tmp_path):
    dom = VisibleFixture()
    provider = ViduBrowserProvider({"alice": BrowserBinding("http://127.0.0.1:9222")})

    @asynccontextmanager
    async def page(owner):
        if owner not in provider.bindings:
            raise DomainError("browser_not_bound", "No isolated test session")
        yield dom, provider.bindings[owner]

    provider.page = page
    service = PurchaseService(Store(tmp_path / "browser.sqlite3"), provider)
    return dom, provider, service


async def purchase(service, index=0):
    quote = (await service.list_plans("alice"))["quotes"][index]
    return await service.prepare_purchase("alice", quote["quote_id"], "browser-request-1")


async def test_real_adapter_reads_quotes_but_never_pays_or_claims_fulfillment(browser_system):
    dom, provider, service = browser_system
    result = await purchase(service)
    assert result["state"] == PurchaseState.AWAITING_PAYMENT
    assert result["plan"]["account_label"] == dom.handle
    assert "account_ref" not in result["plan"]
    assert result["payment_action"]["confirmation_by"] == "user"
    assert result["payment_action"]["embedded"] is False
    assert dom.created == 1
    original = service.store.purchase("alice", result["purchase_id"])
    restarted = PurchaseService(Store(service.store.path), provider)
    assert (
        await restarted.prepare_purchase("alice", original.quote.quote_id, original.idempotency_key)
    ) == result
    assert dom.created == 1
    assert (await service.check_purchase("alice", result["purchase_id"]))["state"] == (
        PurchaseState.AWAITING_PAYMENT
    )
    # Closing/returning from checkout is not evidence of payment.
    dom.context.pages = [dom]
    checked = await service.check_purchase("alice", result["purchase_id"])
    assert checked["state"] == PurchaseState.VERIFICATION_REQUIRED
    assert checked["continuation_prompt"] is None and checked["auto_generate"] is False


async def test_premium_checkout_uses_observed_official_product_name(browser_system):
    dom, _, service = browser_system
    dom.review_text = "订阅 实付金额 $26 立即订阅"
    dom.checkout_text = "订阅 Vidu Premium\nUS$26.00\n然后从下个月开始每月 US$43.00"
    assert (await purchase(service, 1))["state"] == PurchaseState.AWAITING_PAYMENT
    assert dom.selection == 1 and dom.created == 1


async def test_unverified_product_is_comparable_but_cannot_create_checkout(browser_system):
    dom, _, service = browser_system
    quotes = (await service.list_plans("alice"))["quotes"]
    assert [q["checkout_available"] for q in quotes] == [True, True, False]
    result = await service.prepare_purchase("alice", quotes[2]["quote_id"], "ultimate-attempt")
    assert result["state"] == PurchaseState.BLOCKED and dom.created == 0


async def test_account_switch_invalidates_existing_quote_before_order_creation(browser_system):
    dom, _, service = browser_system
    quote = (await service.list_plans("alice"))["quotes"][0]
    dom.handle = "vidu_BOB12345"
    result = await service.prepare_purchase("alice", quote["quote_id"], "switched-account")
    assert result["state"] == PurchaseState.BLOCKED
    assert "browser_account_changed" in result["message"] and dom.created == 0


async def test_changed_browser_mapping_also_invalidates_quote(browser_system):
    dom, provider, service = browser_system
    quote = (await service.list_plans("alice"))["quotes"][0]
    provider.bindings["alice"] = BrowserBinding("http://127.0.0.1:9333")
    result = await service.prepare_purchase("alice", quote["quote_id"], "changed-endpoint")
    assert result["state"] == PurchaseState.BLOCKED and dom.created == 0


async def test_checkout_price_change_stops_before_creating_order(browser_system):
    dom, _, service = browser_system
    dom.review_text = "订阅 实付金额 $10 立即订阅"
    result = await purchase(service)
    assert result["state"] == PurchaseState.PRICE_CHANGED and dom.created == 0


async def test_unverified_stripe_summary_remembers_session_but_hides_payment_action(browser_system):
    dom, _, service = browser_system
    dom.checkout_text = "订阅 Vidu Premium\nUS$26.00"
    result = await purchase(service)
    assert result["state"] == PurchaseState.VERIFICATION_REQUIRED
    assert result["provider_reference"] == "cs_live_isolatedfixture"
    assert result["payment_action"] is None and dom.created == 1


async def test_different_key_does_not_create_second_open_checkout(browser_system):
    dom, _, service = browser_system
    await purchase(service)
    quote = (await service.list_plans("alice"))["quotes"][0]
    result = await service.prepare_purchase("alice", quote["quote_id"], "different-key")
    assert result["state"] == PurchaseState.BLOCKED and dom.created == 1


async def test_missing_renewal_is_not_accepted_as_monthly_price(browser_system):
    dom, provider, _ = browser_system
    dom.cards = ["标准版 $5.92/月 立即订阅 每月800积分"]
    with pytest.raises(DomainError, match="月付续费"):
        await provider.list_plans("alice")


async def test_monthly_selection_works_when_button_name_includes_discount_badge(browser_system):
    dom, provider, _ = browser_system
    original = dom.get_by_role

    def wide_layout(role, name=None, exact=None):
        if role == "button" and name == "包月套餐" and exact:
            # Observed desktop name: "包月套餐 40% 折扣"; exact role-name lookup fails.
            return SimpleNamespace(
                is_visible=AsyncMock(return_value=False),
                click=AsyncMock(side_effect=BrowserTimeout("Exact name does not match")),
            )
        return original(role, name=name, exact=exact)

    dom.get_by_role = wide_layout
    assert [plan.credits for plan in await provider.list_plans("alice")] == [800, 3500, 20000]


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        ("overlay intercepts pointer events", "弹窗遮挡"),
        ("strict mode violation", "多个同名入口"),
    ],
)
async def test_catalog_failure_reports_actionable_reason_without_raw_browser_data(
    browser_system, monkeypatch, diagnostic, expected
):
    _, provider, _ = browser_system
    monkeypatch.setattr(
        Locator, "click", AsyncMock(side_effect=BrowserTimeout(diagnostic + " private-test-value"))
    )
    with pytest.raises(DomainError) as error:
        await provider.list_plans("alice")
    assert error.value.code == "catalog_controls_unavailable"
    assert expected in str(error.value) and "切换包月套餐" in str(error.value)
    assert "private-test-value" not in str(error.value)


async def test_browser_session_is_not_a_video_token_and_never_exposes_endpoint(browser_system):
    dom, provider, _ = browser_system
    result = await provider.session_status("alice")
    assert result["status"] == "connected"
    assert result["video_account_link_verified"] is False
    assert "9222" not in str(result)
    dom.login = True
    assert (await provider.session_status("alice"))["status"] == "login_required"
    assert (await provider.session_status("bob"))["status"] == "not_configured"


async def test_order_table_never_fabricates_payment_evidence(browser_system, monkeypatch):
    dom, provider, _ = browser_system
    monkeypatch.setattr("vidu_purchase.browser.asyncio.sleep", AsyncMock())
    result = await provider.order_observation("alice")
    assert result["orders"] == [] and result["payment_verified"] is False
    assert not dom.orders_open
    dom.order_rows = [
        list(ORDER_HEADERS),
        ["fixture", "date", "date", "date", "test-001", "card", "$7"],
    ]
    result = await provider.order_observation("alice")
    assert len(result["orders"]) == 1 and result["entitlement_verified"] is False
    dom.order_rows = [["unexpected columns"], ["暂无数据"]]
    with pytest.raises(DomainError, match="订单列"):
        await provider.order_observation("alice")
    dom.order_rows = [list(ORDER_HEADERS)]
    with pytest.raises(DomainError, match="仍在加载"):
        await provider.order_observation("alice")
    assert not dom.orders_open


async def test_orders_wait_for_empty_placeholder_to_resolve(browser_system, monkeypatch):
    dom, provider, _ = browser_system
    loading_rows = iter(
        [[list(ORDER_HEADERS)], [list(ORDER_HEADERS), [""]], [list(ORDER_HEADERS), ["暂无数据"]]]
    )
    original = Locator.evaluate_all

    async def delayed_rows(locator, code):
        if locator.name == "table tr":
            return next(loading_rows)
        return await original(locator, code)

    monkeypatch.setattr(Locator, "evaluate_all", delayed_rows)
    monkeypatch.setattr("vidu_purchase.browser.asyncio.sleep", AsyncMock())
    result = await provider.order_observation("alice")
    assert result["orders"] == [] and result["payment_verified"] is False
    assert not dom.orders_open


async def test_returning_to_plans_closes_only_read_only_order_view(browser_system, monkeypatch):
    dom, provider, _ = browser_system
    original_click = Locator.click

    async def close_outside_viewport(locator):
        if locator.name == "Close":
            raise BrowserTimeout("element is outside of the viewport")
        await original_click(locator)

    monkeypatch.setattr(Locator, "click", close_outside_viewport)
    dom.orders_open = True
    assert len(await provider.list_plans("alice")) == 3
    assert not dom.orders_open
    dom.review = True
    with pytest.raises(DomainError) as error:
        await provider.list_plans("alice")
    assert error.value.code == "checkout_already_open" and dom.review


def test_loopback_aliases_cannot_share_same_browser():
    with pytest.raises(ValueError, match="distinct"):
        ViduBrowserProvider(
            {
                "alice": BrowserBinding("http://localhost:9222/"),
                "bob": BrowserBinding("http://127.0.0.1:9222"),
            }
        )


async def test_browser_http_and_mcp_share_authenticated_session_without_claiming_ready(
    browser_system, protocol_system
):
    _, provider, service = browser_system
    verifier = protocol_system[2]
    app = create_hosted_app(
        store=service.store, verifier=verifier, video=WebsiteVideoProvider({}), billing=provider
    )
    async with rest(app) as client:
        caps = (await client.get("/v1/capabilities")).json()
        assert caps["billing_adapter_configured"] is True
        assert caps["browser_commerce_available"] is True
        assert caps["real_payment_verification_available"] is False
        status = (await client.get("/v1/commerce/session")).json()
        assert status["status"] == "connected"
        assert (await client.get("/v1/account")).json()["status"] == "not_connected"
        assert (await client.get("/v1/commerce/orders")).json()["orders"] == []
    async with rest(app, "bob") as other:
        assert (await other.get("/v1/commerce/session")).json()["status"] == "not_configured"
        assert (await other.get("/v1/commerce/orders")).status_code == 409
    async with running(app) as url, Client(url + "/mcp", auth="purchase-only") as mcp:
        assert (await mcp.call_tool("vidu_commerce_session", {})).data == status
        assert (await mcp.call_tool("vidu_website_orders", {})).data["payment_verified"] is False


async def test_recovered_checkout_reference_is_persistent_and_cannot_be_replaced(browser_system):
    _, provider, service = browser_system
    result = await purchase(service)
    key = result["purchase_id"]
    provider.check_receipt = AsyncMock(
        return_value=Checkout(state=PurchaseState.LOGIN_REQUIRED, message="Login again")
    )
    assert (await service.check_purchase("alice", key))[
        "provider_reference"
    ] == "cs_live_isolatedfixture"
    provider.check_receipt = AsyncMock(
        return_value=Checkout(
            state=PurchaseState.AWAITING_PAYMENT,
            message="Different session",
            provider_reference="cs_live_other",
        )
    )
    with pytest.raises(DomainError, match="不能更换"):
        await service.check_purchase("alice", key)
