"""Regression tests for login redirects across real adapter/CDP reconnections.

Only the browser transport is substituted. These do not establish live login or
payment acceptance; the production page-selection and navigation code runs here.
"""

import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from test_browser_commerce import VisibleFixture
from test_hosted import running

from vidu_purchase.browser import SUBSCRIPTION_URL, BrowserBinding, ViduBrowserProvider
from vidu_purchase.hosted import create_hosted_app
from vidu_purchase.models import DomainError, PurchaseState
from vidu_purchase.service import PurchaseService
from vidu_purchase.store import Store
from vidu_purchase.video_provider import WebsiteVideoProvider

HOME_URL = "https://www.vidu.com/zh/home/recommend"


@pytest.fixture
def attached_browser(monkeypatch, tmp_path):
    page = VisibleFixture()

    async def navigate(url, **_kwargs):
        page.url = url

    page.goto = AsyncMock(side_effect=navigate)
    page.set_default_timeout = Mock()
    page.reload = AsyncMock()
    context = SimpleNamespace(pages=[page], new_page=AsyncMock())
    page.context = context
    browser = SimpleNamespace(contexts=[context], close=AsyncMock())
    connect = AsyncMock(return_value=browser)

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))

    monkeypatch.setattr("vidu_purchase.browser.async_playwright", playwright)
    provider = ViduBrowserProvider({"alice": BrowserBinding("http://127.0.0.1:9222")})
    service = PurchaseService(Store(tmp_path / "navigation.sqlite3"), provider)
    yield page, context, provider, service, connect
    context.new_page.assert_not_awaited()
    page.reload.assert_not_awaited()
    browser.close.assert_not_awaited()


async def test_status_reuses_redirected_login_page_across_reconnections(attached_browser):
    page, _, provider, _, connect = attached_browser
    page.url, page.login = HOME_URL, True
    for _ in range(3):
        assert (await provider.session_status("alice"))["status"] == "login_required"
    assert connect.await_count == 3
    page.goto.assert_not_awaited()
    assert page.url == HOME_URL


async def test_repeated_catalog_requests_leave_login_in_progress_alone(attached_browser):
    page, _, provider, _, _ = attached_browser
    page.url, page.login = HOME_URL, True
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            await provider.list_plans("alice")
        assert error.value.code == "login_required"
    page.goto.assert_not_awaited()


async def test_login_completion_is_detected_without_opening_member_page(attached_browser):
    page, _, provider, _, _ = attached_browser
    page.url, page.login = HOME_URL, True
    assert (await provider.session_status("alice"))["status"] == "login_required"
    page.login = False
    assert (await provider.session_status("alice"))["status"] == "connected"
    page.goto.assert_not_awaited()
    assert len(await provider.list_plans("alice")) == 3
    assert len(await provider.list_plans("alice")) == 3
    page.goto.assert_awaited_once_with(SUBSCRIPTION_URL, wait_until="domcontentloaded")


async def test_login_redirect_during_catalog_navigation_is_not_retried(attached_browser):
    page, _, provider, _, _ = attached_browser
    page.url = HOME_URL

    async def expired_login(*_args, **_kwargs):
        page.url, page.login = HOME_URL, True

    page.goto.side_effect = expired_login
    for _ in range(2):
        with pytest.raises(DomainError) as error:
            await provider.list_plans("alice")
        assert error.value.code == "login_required"
    assert (await provider.session_status("alice"))["status"] == "login_required"
    page.goto.assert_awaited_once()


async def test_navigation_revalidates_account_before_reading_quotes(attached_browser):
    page, _, provider, _, _ = attached_browser
    page.url = HOME_URL

    async def changed_account(url, **_kwargs):
        page.url, page.handle = url, "vidu_BOB12345"

    page.goto.side_effect = changed_account
    with pytest.raises(DomainError) as error:
        await provider.list_plans("alice")
    assert error.value.code == "browser_account_changed"


@pytest.mark.parametrize("state", ["no_page", "external_login", "conflicting_accounts"])
async def test_unavailable_or_ambiguous_page_never_creates_another(attached_browser, state):
    page, context, provider, _, _ = attached_browser
    if state == "no_page":
        context.pages = []
    elif state == "external_login":
        # A redirect outside the official origin is not inspected or navigated.
        page.url = "https://identity.example.test/sign-in"
    else:
        other = VisibleFixture()
        other.handle = "vidu_BOB12345"
        context.pages.append(other)
    expected = "ambiguous_page" if state == "conflicting_accounts" else "browser_page_missing"
    for _ in range(2):
        assert (await provider.session_status("alice"))["status"] == expected
    page.goto.assert_not_awaited()


async def test_old_duplicate_login_tabs_are_reused_without_cleanup_or_navigation(attached_browser):
    page, context, provider, _, _ = attached_browser
    page.url, page.login = HOME_URL, True
    other = VisibleFixture()
    other.url, other.login = HOME_URL, True
    context.pages.append(other)
    for _ in range(3):
        assert (await provider.session_status("alice"))["status"] == "login_required"
    assert len(context.pages) == 2
    page.goto.assert_not_awaited()


async def test_completed_login_is_found_among_old_unrecognized_tabs(attached_browser):
    page, context, provider, _, _ = attached_browser
    page.url = HOME_URL
    old = VisibleFixture()
    old.handle = None
    context.pages.insert(0, old)
    assert (await provider.session_status("alice"))["status"] == "connected"
    assert (await provider.list_plans("alice"))[0].account_label == page.handle
    page.goto.assert_awaited_once_with(SUBSCRIPTION_URL, wait_until="domcontentloaded")
    assert len(context.pages) == 2


async def test_duplicate_pages_for_same_account_do_not_reopen_login(attached_browser):
    page, context, provider, _, _ = attached_browser
    other = VisibleFixture()
    context.pages.append(other)
    for _ in range(2):
        assert (await provider.session_status("alice"))["status"] == "connected"
    assert len(await provider.list_plans("alice")) == 3
    page.goto.assert_not_awaited()


async def test_unrecognized_identity_is_not_treated_as_a_reason_to_reopen_login(attached_browser):
    page, _, provider, _, _ = attached_browser
    page.url, page.handle = HOME_URL, None
    for _ in range(2):
        assert (await provider.session_status("alice"))["status"] == "browser_identity_unverified"
    page.goto.assert_not_awaited()


async def test_receipt_polling_leaves_original_login_and_payment_tabs_alone(attached_browser):
    page, context, provider, service, _ = attached_browser
    quote = (await service.list_plans("alice"))["quotes"][0]
    pending = await service.prepare_purchase("alice", quote["quote_id"], "navigation-regression")
    assert pending["state"] == PurchaseState.AWAITING_PAYMENT
    assert len(context.pages) == 2
    payment_page = context.pages[1]
    page.url, page.login = HOME_URL, True
    for _ in range(2):
        result = await service.check_purchase("alice", pending["purchase_id"])
        assert result["state"] == PurchaseState.LOGIN_REQUIRED
        assert result["provider_reference"] == pending["provider_reference"]
    assert (await provider.session_status("alice"))["status"] == "login_required"
    page.goto.assert_not_awaited()
    assert context.pages[1] is payment_page and len(context.pages) == 2


async def test_wrong_quote_account_blocks_before_member_navigation(attached_browser):
    page, _, _, service, _ = attached_browser
    quote = (await service.list_plans("alice"))["quotes"][0]
    page.url, page.handle = HOME_URL, "vidu_BOB12345"
    result = await service.prepare_purchase(
        "alice", quote["quote_id"], "switched-before-navigation"
    )
    assert result["state"] == PurchaseState.BLOCKED
    assert "browser_account_changed" in result["message"]
    page.goto.assert_not_awaited()


@pytest.fixture
def local_check(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/selfhost_local.py"
    spec = importlib.util.spec_from_file_location("local_check_regression", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    return module.check


async def test_local_check_skips_catalog_while_waiting_for_login(
    attached_browser, protocol_system, tmp_path, local_check
):
    page, _, provider, service, connect = attached_browser
    page.url, page.login = HOME_URL, True
    provider.list_plans = AsyncMock(
        side_effect=AssertionError("Must not read catalog during login")
    )
    app = create_hosted_app(
        store=service.store,
        verifier=protocol_system[2],
        video=WebsiteVideoProvider({}),
        billing=provider,
    )
    async with running(app) as origin:
        await local_check(int(origin.rsplit(":", 1)[1]), "alice")
    report = json.loads((tmp_path / ".local/selfhost-verification.json").read_text("utf-8"))
    assert report["plans_http_status"] is None
    assert report["plans_skipped_reason"] == "browser_not_connected"
    assert report["commerce_session_status"] == "login_required"
    assert report["mcp_commerce_session_status"] == "login_required"
    # REST/MCP each read the website session and the combined connection state.
    assert connect.await_count == 4
    provider.list_plans.assert_not_awaited()
    page.goto.assert_not_awaited()


async def test_failed_catalog_is_not_reported_as_successful_live_check(
    attached_browser, protocol_system, tmp_path, local_check
):
    _, _, provider, service, _ = attached_browser
    provider.list_plans = AsyncMock(
        side_effect=DomainError("catalog_controls_unavailable", "Test-only inaccessible controls")
    )
    app = create_hosted_app(
        store=service.store,
        verifier=protocol_system[2],
        video=WebsiteVideoProvider({}),
        billing=provider,
    )
    async with running(app) as origin:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await local_check(int(origin.rsplit(":", 1)[1]), "alice")
        assert error.value.response.status_code == 409
    report = json.loads((tmp_path / ".local/selfhost-verification.json").read_text("utf-8"))
    assert report["plans_error"]["code"] == "catalog_controls_unavailable"
    assert report["plans_http_status"] == 409
    assert report["authenticated_commerce_account_verified"] is True
    assert report["payment_executed"] is False
