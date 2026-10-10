"""Isolated boundary/transport tests. No fake provider is included in the product."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from test_console import ConsoleKeyringFixture
from test_hosted import running

from vidu_purchase.browser import BrowserBinding, ViduBrowserProvider
from vidu_purchase.browser_panel import APP_URI, PanelInput, PanelSession, RemoteBrowser
from vidu_purchase.credentials import AccountVaults
from vidu_purchase.hosted import create_hosted_app
from vidu_purchase.models import DomainError, Plan, Purchase, PurchaseState, Quote
from vidu_purchase.store import Store
from vidu_purchase.video_models import AccountSnapshot
from vidu_purchase.video_provider import WebsiteVideoProvider

ORIGIN = "http://127.0.0.1:8787"


class RemoteFixture:
    def __init__(self, provider, session, store):
        self.stage = "fixture"
        self.lock = provider.locks.setdefault(session.owner, asyncio.Lock())
        self.start = AsyncMock()
        self.frame = AsyncMock(return_value={"state": "live", "epoch": 2})
        self.input = AsyncMock()
        self.close = AsyncMock()


@pytest.fixture
def panel_app(tmp_path):
    verifier = StaticTokenVerifier(tokens={
        name: {"client_id": "fixture", "sub": name, "scopes": scopes}
        for name, scopes in {
            "alice": ["vidu:video", "vidu:purchase"],
            "bob": ["vidu:video", "vidu:purchase"], "limited": ["vidu:video"],
        }.items()
    })
    provider = ViduBrowserProvider({
        "alice": BrowserBinding("http://127.0.0.1:9222"),
        "bob": BrowserBinding("http://127.0.0.1:9223"),
        "limited": BrowserBinding("http://127.0.0.1:9224"),
    })
    store = Store(tmp_path / "panels.sqlite3")
    app = create_hosted_app(
        store=store, verifier=verifier, video=WebsiteVideoProvider({}), billing=provider,
        panel_origins=["https://agent.example"],
    )
    app.state.panels.remote_factory = RemoteFixture
    return app, provider, store


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)


async def issue(api, owner="alice", body=None):
    return await api.post(
        "/v1/browser-panels", json=body or {"purpose": "account"},
        headers={"Authorization": f"Bearer {owner}"},
    )


async def connect(api, owner="alice"):
    result = await issue(api, owner)
    assert result.status_code == 200
    ticket = result.json()["panel_url"].split("#ticket=")[1]
    headers = {"Origin": ORIGIN, "X-Yingban-Panel": "1"}
    exchange = await api.post("/panel/session", json={"ticket": ticket}, headers=headers)
    assert exchange.status_code == 200
    headers["Authorization"] = "Bearer " + exchange.json()["session_token"]
    return ticket, headers


async def test_panel_auth_replay_and_no_ambient_cookie_access(panel_app):
    app, _, _ = panel_app
    async with client(app) as api:
        assert (await issue(api, "wrong")).status_code == 401
        assert (await issue(api, "limited")).status_code == 403
        ticket, headers = await connect(api)
        assert "yingban_console" not in api.cookies
        replay = await api.post("/panel/session", json={"ticket": ticket}, headers=headers)
        assert replay.status_code == 401
        denied = await api.get("/panel/frame", headers={"X-Yingban-Panel": "1"})
        assert denied.status_code == 401
        assert (await api.get("/panel/frame", headers=headers)).json()["state"] == "live"
        cross = await api.post("/panel/input", headers={**headers, "Origin":"https://evil.test"},
                               json={"kind":"click", "epoch":2})
        assert cross.status_code == 403
        assert (await api.get("/v1/account", headers=headers)).status_code == 401


async def test_distinct_users_and_explicit_release(panel_app):
    app, _, _ = panel_app
    async with client(app) as api:
        _, alice = await connect(api)
        _, bob = await connect(api, "bob")
        await api.get("/panel/frame", headers=alice)
        await api.get("/panel/frame", headers=bob)
        sessions = app.state.panels.sessions
        a, b = (sessions[headers["Authorization"][7:]] for headers in (alice, bob))
        assert a.owner == "alice" and b.owner == "bob" and a.remote is not b.remote
        second = (await issue(api)).json()["panel_url"].split("#ticket=")[1]
        duplicate = await api.post("/panel/session", json={"ticket":second}, headers=alice)
        assert duplicate.status_code == 409
        assert (await api.delete("/panel/session", headers=alice)).status_code == 200
        a.remote.close.assert_awaited_once()
        assert (await api.get("/panel/frame", headers=alice)).status_code == 401
        assert (await api.get("/panel/frame", headers=bob)).status_code == 200


async def test_session_expiry_binding_change_and_secret_errors(panel_app):
    app, provider, _ = panel_app
    async with client(app) as api:
        _, headers = await connect(api)
        await api.get("/panel/frame", headers=headers)
        saved = app.state.panels.sessions[headers["Authorization"][7:]]
        saved.remote.frame.side_effect = RuntimeError("private-input-secret-and-cdp-url")
        failure = await api.get("/panel/frame", headers=headers)
        assert failure.status_code == 503 and "private-input" not in failure.text
        provider.bindings["alice"] = BrowserBinding("http://127.0.0.1:9225")
        assert (await api.get("/panel/frame", headers=headers)).status_code == 401
        saved.expires = 0
        await app.state.panels.prune()
        saved.remote.close.assert_awaited_once()


@pytest.mark.parametrize("body", [
    {"purpose":"payment"}, {"purpose":"account", "purchase_id":"a"*32},
    {"purpose":"account", "url":"http://127.0.0.1:9222"},
    {"purpose":"account", "owner":"bob"}, {"purpose":"account", "token":"secret"},
])
async def test_launch_rejects_arbitrary_targets_and_identity(panel_app, body):
    async with client(panel_app[0]) as api:
        assert (await issue(api, body=body)).status_code == 422


def save_purchase(store, state=PurchaseState.AWAITING_PAYMENT):
    plan = Plan(plan_id="fixture", name="Test fixture", amount_minor=100,
                credits=10, source="vidu_website", source_text="Unit fixture only")
    quote = Quote(quote_id="q", owner="alice", plan=plan, created_at=1, expires_at=2)
    purchase = Purchase(
        purchase_id="a"*32, owner="alice", quote=quote, idempotency_key="fixture-purchase",
        state=state, created_at=1, updated_at=1, message="Fixture",
        handoff_url="https://checkout.stripe.com/c/pay/cs_live_FixtureOnly",
        provider_reference="cs_live_FixtureOnly",
    )
    store.insert_purchase(purchase)
    return purchase


async def test_payment_panel_only_opens_verified_checkout_of_same_owner(panel_app):
    app, _, store = panel_app
    purchase = save_purchase(store)
    body = {"purpose":"payment", "purchase_id":purchase.purchase_id}
    async with client(app) as api:
        assert (await issue(api, "bob", body)).status_code == 404
        result = await issue(api, body=body)
        assert result.status_code == 200
        assert "cs_live" not in result.text
        store.update(purchase.model_copy(update={"state":PurchaseState.VERIFICATION_REQUIRED}))
        assert (await issue(api, body=body)).status_code == 409


async def test_csp_explicit_embedding_and_no_private_assets(panel_app):
    async with client(panel_app[0]) as api:
        page = await api.get("/panel")
        assert page.status_code == 200
        csp = page.headers["content-security-policy"]
        assert "frame-ancestors 'self' https://agent.example" in csp and "*" not in csp
        assert page.headers["cache-control"] == "no-store"
        assert "session_token" not in page.text
        assert (await api.get("/assets/mcp-panel.html")).status_code == 404


async def test_mcp_app_keeps_panel_grant_out_of_model_content(panel_app):
    app, _, _ = panel_app
    async with running(app) as url, Client(url + "/mcp", auth="alice") as mcp:
        tools = {tool.name:tool for tool in await mcp.list_tools()}
        tool = tools["vidu_open_official_panel"]
        assert tool.meta["ui"]["resourceUri"] == APP_URI
        assert set(tool.input_schema["properties"]) == {"purpose", "purchase_id"}
        result = await mcp.call_tool("vidu_open_official_panel", {"purpose":"account"})
        assert "panel_url" not in result.data
        assert "ticket=" not in str(result.content)
        assert "ticket=" in result.meta["yingbanPanel"]["panel_url"]
        resource = await mcp.read_resource(APP_URI)
        assert resource[0].mime_type == "text/html;profile=mcp-app"
        assert "official-panel" in resource[0].text
        assert "ticket=" not in resource[0].text.split("<script")[0]


async def test_default_csp_is_valid_on_real_http_transport(tmp_path):
    app = create_hosted_app(
        store=Store(tmp_path / "csp.sqlite3"),
        verifier=StaticTokenVerifier(tokens={}), video=WebsiteVideoProvider({}),
    )
    async with running(app) as url, httpx.AsyncClient(base_url=url) as api:
        result = await api.get("/panel")
        assert result.status_code == 200
        assert result.headers["content-security-policy"].endswith("frame-ancestors 'self'")


def remote_fixture(purpose="account"):
    provider = ViduBrowserProvider({"alice":BrowserBinding("http://127.0.0.1:9222")})
    saved = PanelSession("alice", purpose, 9999999999, provider.bindings["alice"], None)
    remote = RemoteBrowser(provider, saved, Mock())
    remote.page = SimpleNamespace(url="https://www.vidu.com/zh/home")
    remote.cdp = SimpleNamespace(send=AsyncMock(return_value={"data":"fixture-pixels"}))
    remote.sync_pages = AsyncMock()
    remote.token_visible = AsyncMock(return_value=False)
    return remote


async def test_stale_frames_shortcuts_and_lock_do_not_dispatch_input():
    remote = remote_fixture()
    for body in [PanelInput(kind="click", epoch=99), PanelInput(kind="key", epoch=1, key="F12")]:
        with pytest.raises(DomainError):
            await remote.input(body)
    async with remote.lock:
        with pytest.raises(DomainError, match="正在处理"):
            await remote.input(PanelInput(kind="click", epoch=1))
        assert (await remote.frame())["state"] == "busy"
    remote.cdp.send.assert_not_awaited()


async def test_sensitive_view_is_never_captured_or_sent_to_model():
    remote = remote_fixture()
    remote.token_visible.return_value = True
    result = await remote.frame()
    assert result["state"] == "credential_hidden" and "image" not in result
    remote.cdp.send.assert_not_awaited()
    secret = PanelInput(kind="text", epoch=1, text="private-password")
    assert "private-password" not in repr(secret)
    with pytest.raises(DomainError):
        await remote.input(secret)


@pytest.mark.parametrize("url", [
    "file:///C:/private", "http://127.0.0.1:9222", "https://www.vidu.com.evil.test",
    "https://www.vidu.com@evil.test", "https://www.vidu.com:8443",
    "https://checkout.stripe.com/c/pay/cs_live_OtherOwner",
])
async def test_official_panel_rejects_other_origins_and_other_payments(url):
    remote = remote_fixture("payment")
    remote.checkout_reference = "cs_live_FixtureOnly"
    assert not remote.permitted(url)


async def test_inputs_dispatch_to_page_only_and_navigation_blocks_capture():
    remote = remote_fixture()
    await remote.input(PanelInput(kind="click", epoch=1, x=100, y=200))
    assert [c.args[1]["type"] for c in remote.cdp.send.call_args_list] == [
        "mousePressed", "mouseReleased",
    ]
    remote.cdp.send.reset_mock()
    remote.page.url = "https://evil.test"
    with pytest.raises(DomainError):
        await remote.frame()
    remote.cdp.send.assert_not_awaited()


def test_input_validation_never_contains_json_in_public_errors():
    body = PanelInput(kind="text", epoch=1, text="fixture-secret")
    assert "fixture-secret" not in repr(body)
    assert json.loads(body.model_dump_json())["text"] == "fixture-secret"


async def test_timeout_releases_remote_and_next_frame_reconnects(panel_app):
    app, _, _ = panel_app
    async with client(app) as api:
        _, headers = await connect(api)
        await api.get("/panel/frame", headers=headers)
        saved = app.state.panels.sessions[headers["Authorization"][7:]]
        previous = saved.remote
        previous.frame.side_effect = TimeoutError("private-upstream-data")
        response = await api.get("/panel/frame", headers=headers)
        assert response.json()["error"]["code"] == "panel_timeout"
        assert "private-upstream" not in response.text
        previous.close.assert_awaited_once()
        assert not saved.lock.locked() and saved.remote is None
        assert (await api.get("/panel/frame", headers=headers)).status_code == 200
        assert saved.remote is not previous


async def test_order_transition_disables_already_open_payment_panel():
    remote = remote_fixture("payment")
    remote.checkout_reference = "cs_live_FixtureOnly"
    remote.store.purchase.return_value = SimpleNamespace(state=PurchaseState.READY)
    with pytest.raises(DomainError, match="订单状态已变化"):
        await remote.frame()
    with pytest.raises(DomainError, match="订单状态已变化"):
        await remote.input(PanelInput(kind="click", epoch=1))
    remote.cdp.send.assert_not_awaited()


async def test_invalid_input_cannot_start_a_browser_connection(panel_app):
    app, _, _ = panel_app
    async with client(app) as api:
        _, headers = await connect(api)
        saved = app.state.panels.sessions[headers["Authorization"][7:]]
        response = await api.post("/panel/input", headers=headers, json={
            "kind":"text", "epoch":1, "text":"private-input", "url":"https://evil.test",
        })
        assert response.status_code == 422 and "private-input" not in response.text
        assert saved.remote is None


async def test_panel_viewport_stays_bounded_to_the_user_surface(panel_app):
    app, _, _ = panel_app
    async with client(app) as api:
        result = await issue(api)
        ticket = result.json()["panel_url"].split("#ticket=")[1]
        headers = {"X-Yingban-Panel":"1", "Origin":ORIGIN}
        oversized = await api.post("/panel/session", headers=headers, json={
            "ticket":ticket, "viewport_width":10000,
        })
        assert oversized.status_code == 422
        result = await api.post("/panel/session", headers=headers, json={
            "ticket":ticket, "viewport_width":360,
        })
        saved = app.state.panels.sessions[result.json()["session_token"]]
        assert saved.viewport_width == 360
        assert "ticket" not in result.json()


async def test_authorization_and_revocation_remain_bound_to_current_user(
    panel_app, tmp_path, monkeypatch,
):
    app, _, _ = panel_app
    connections = app.state.panels.connections
    vaults = AccountVaults(tmp_path / "subjects", {"alice", "bob"},
                          backend=ConsoleKeyringFixture())
    connections.vaults = vaults

    @asynccontextmanager
    async def grant(owner):
        assert owner == "alice"
        yield SimpleNamespace(token="fixture-private-grant", account_label="vidu_ALICE1234",
                              account_ref="a" * 64, verify=AsyncMock())

    connections.reader = SimpleNamespace(read=grant)
    monkeypatch.setattr("vidu_purchase.console.LocalConsole.validate_credential", AsyncMock(
        return_value=AccountSnapshot(status="connected", credits=14, message="Isolated fixture"),
    ))
    async with client(app) as api:
        _, alice = await connect(api)
        _, bob = await connect(api, "bob")
        denied = await api.post("/panel/authorize", headers=alice,
                                json={"authorize":True, "owner":"bob"})
        assert denied.status_code == 422
        result = await api.post("/panel/authorize", headers=alice, json={"authorize":True})
        assert result.json()["account_link_verified"] is True
        assert result.json()["auto_generate"] is False
        assert "fixture-private-grant" not in result.text
        assert vaults.credential("alice")[1] == "fixture-private-grant"
        assert vaults.credential("bob") is None
        vaults.for_owner("alice").disconnect()
        assert (await api.get("/panel/frame", headers=alice)).status_code == 401
        assert (await api.get("/panel/frame", headers=bob)).status_code == 200
