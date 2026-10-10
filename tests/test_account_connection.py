"""Grant binding tests with isolated browser/API transports, not live acceptance evidence."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from test_console import ORIGIN, TOKEN, ConsoleKeyringFixture, browser, login
from test_hosted import running

from vidu_purchase.browser import BrowserBinding, ViduBrowserProvider
from vidu_purchase.credentials import AccountVaults, CredentialVault
from vidu_purchase.hosted import create_hosted_app
from vidu_purchase.store import Store
from vidu_purchase.video_models import AccountSnapshot
from vidu_purchase.video_provider import WebsiteVideoProvider

SECRET = "fixture-only-credential-never-an-api-token"


def test_per_subject_vaults_isolate_credentials_and_reconnect_after_restart(tmp_path):
    backend = ConsoleKeyringFixture()
    subjects = {"alice", "bob", "../../opaque-subject"}
    vaults = AccountVaults(tmp_path, subjects, backend=backend)
    alice = vaults.for_owner("alice")
    alice.save_browser(alice.owner(), SECRET, "vidu_ALICE1234", "a" * 64)
    bob = vaults.for_owner("bob")
    assert vaults.credential("bob") is None
    assert alice.path != bob.path
    assert vaults.credential("unprovisioned") is None
    assert vaults.for_owner("../../opaque-subject").path.parent.parent == tmp_path
    assert SECRET.encode() not in alice.path.read_bytes()
    restarted = AccountVaults(tmp_path, subjects, backend=backend)
    assert restarted.credential("alice")[1] == SECRET
    assert restarted.browser_link("alice")["account_label"] == "vidu_ALICE1234"
    revision = restarted.revision("alice")
    alice.disconnect()
    assert restarted.revision("alice") != revision
    assert restarted.credential("alice") is None and restarted.credential("bob") is None


class AccountPage:
    def __init__(self):
        self.url = "https://www.vidu.com/zh/home"
        self.handle = "vidu_ALICE1234"
        self.login = False
        self.panel = False
        self.other_modal = False
        self.secret = SECRET
        self.actions = []
        self.set_default_timeout = Mock()
        self.bring_to_front = AsyncMock()
        self.goto = AsyncMock()
        self.reload = AsyncMock()

    def get_by_text(self, name, **_):
        if name == "使用邮箱注册/登录":
            return AccountLocator(self, "login")
        assert name == "Vidu Token"
        return AccountLocator(self, "title")

    def get_by_role(self, role, name=None, **_):
        if role == "button":
            assert name in {"登录", "Close login dialog"}
            return AccountLocator(self, "login")
        assert role in {"menu", "dialog"}
        return AccountLocator(self, role)

    def locator(self, selector):
        if selector == 'header img[alt], div.cursor-pointer > img[alt]':
            kind = "identity"
        elif selector == '[role="dialog"][aria-modal="true"]':
            kind = "other_modal"
        else:
            assert selector == (
                f'header img[alt="{self.handle}"], '
                f'div.cursor-pointer > img[alt="{self.handle}"]'
            )
            kind = "avatar"
        return AccountLocator(self, kind)


class AccountLocator:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind

    def filter(self, **_):
        assert self.kind in {"dialog", "menu"}
        return self

    def locator(self, selector):
        assert self.kind == "dialog" and selector == "div.truncate"
        return AccountLocator(self.page, "credential")

    def get_by_text(self, text, **_):
        assert self.kind == "menu" and text == "Vidu Token"
        return AccountLocator(self.page, "token_menu")

    def get_by_role(self, role, name, **_):
        assert self.kind == "dialog" and role == "button" and name == "Close"
        return AccountLocator(self.page, "close")

    async def is_visible(self):
        if self.kind == "menu":
            return False
        return self.page.login if self.kind == "login" else self.page.panel

    async def count(self):
        return int(self.page.other_modal) if self.kind == "other_modal" else 1

    async def evaluate_all(self, _code):
        assert self.kind == "identity"
        return [self.page.handle]

    async def wait_for(self, state="visible", **_):
        assert self.page.panel is (state == "visible")

    async def inner_text(self):
        assert self.kind == "credential" and self.page.panel
        self.page.actions.append("read_visible_token")
        return self.page.secret

    async def click(self):
        assert self.kind in {"avatar", "token_menu", "close"}
        self.page.actions.append(self.kind)
        if self.kind in {"token_menu", "close"}:
            self.page.panel = self.kind == "token_menu"


@pytest.fixture
def system(tmp_path, monkeypatch):
    page = AccountPage()
    vault = CredentialVault(tmp_path / "account", backend=ConsoleKeyringFixture())
    context = SimpleNamespace(pages=[page], new_page=AsyncMock())

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(
            connect_over_cdp=AsyncMock(return_value=SimpleNamespace(contexts=[context]))
        ))

    monkeypatch.setattr("vidu_purchase.browser.async_playwright", playwright)
    billing = ViduBrowserProvider({"local-user": BrowserBinding("http://127.0.0.1:9222")})
    video = WebsiteVideoProvider(
        {}, credentials=lambda _: vault.credential(vault.owner()),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"credits": 14})),
    )
    verifier = StaticTokenVerifier(tokens={
        TOKEN: {"client_id": "test-client", "sub": "local-user",
                "scopes": ["vidu:video", "vidu:purchase"]},
        "limited": {"client_id": "test-client", "sub": "local-user", "scopes": ["vidu:video"]},
    })
    app = create_hosted_app(
        store=Store(tmp_path / "service.sqlite3"), verifier=verifier,
        video=video, billing=billing, local_console=True, console_vault=vault,
    )

    async def validated(token, region):
        assert token == SECRET and region == "global"
        return AccountSnapshot(status="connected", credits=14, message="isolated fixture")

    app.state.console.validator = validated
    return SimpleNamespace(app=app, page=page, vault=vault, billing=billing, context=context)


async def authorize(client, headers):
    return await client.post(
        "/console/account/browser", json={"authorize": True}, headers=headers
    )


async def test_authorization_binds_real_reader_result_without_returning_credential(system):
    async with browser(system.app) as client:
        _, headers = await login(client)
        result = await authorize(client, headers)
        assert result.status_code == 200
        assert result.json()["account_link_verified"] is True
        assert result.json()["auto_generate"] is False
        assert SECRET not in result.text
        assert system.vault.credential(system.vault.owner())[1] == SECRET
        assert SECRET.encode() not in system.vault.path.read_bytes()
        assert system.page.actions[-1] == "close"
        system.page.actions.clear()
        state = (await client.get("/console/api/v1/account/connection", headers=headers)).json()
        assert state["state"] == "connected"
        assert state["account_link_verified"] is True
        assert system.page.actions == []
        system.page.goto.assert_not_awaited()
        system.page.reload.assert_not_awaited()
        system.context.new_page.assert_not_awaited()


@pytest.mark.parametrize("body", [{}, {"authorize": False}, {"authorize": 1},
                                 {"authorize": True, "token": SECRET}])
async def test_no_import_without_explicit_ui_consent(system, body):
    async with browser(system.app) as client:
        _, headers = await login(client)
        response = await client.post("/console/account/browser", json=body, headers=headers)
        assert response.status_code == 422
        assert system.page.actions == []
        assert system.vault.credential(system.vault.owner()) is None


async def test_import_requires_console_session_and_csrf(system):
    async with browser(system.app) as client:
        assert (await authorize(client, {})).status_code in {401, 403}
        _, headers = await login(client)
        for changed in ({**headers, "X-CSRF-Token": "wrong"},
                        {**headers, "Origin": "https://attacker.example"}):
            assert (await authorize(client, changed)).status_code == 403
        assert system.page.actions == []


@pytest.mark.parametrize("change", ["account", "token", "disconnect", "validation"])
async def test_changes_during_validation_never_save_grant(system, change):
    original = system.app.state.console.validator

    async def changed(token, region):
        if change == "validation":
            raise RuntimeError("upstream private credential " + SECRET)
        if change == "account":
            system.page.handle = "vidu_BOB12345"
        if change == "token":
            system.page.secret = "fixture-rotated-token-credential"
        if change == "disconnect":
            system.vault.disconnect()
        return await original(token, region)

    system.app.state.console.validator = changed
    async with browser(system.app) as client:
        _, headers = await login(client)
        result = await authorize(client, headers)
        assert result.status_code in {409, 503}
        assert SECRET not in result.text
        assert system.vault.credential(system.vault.owner()) is None
        assert system.page.panel is False


async def test_matching_manual_grant_is_linked_without_invalidating_pending_work(system):
    original_owner = system.vault.owner()
    system.vault.save(original_owner, SECRET, "global")
    assert system.vault.browser_link(original_owner) is None
    system.page.panel = True  # A manual connection can leave the official Token panel open.
    async with browser(system.app) as client:
        _, headers = await login(client)
        assert (await authorize(client, headers)).status_code == 200
        assert system.vault.owner() == original_owner
        assert system.page.panel is False
        reopened = CredentialVault(system.vault.path.parent, backend=system.vault.backend)
        assert reopened.browser_link(original_owner)["account_label"] == system.page.handle


async def test_conflicting_existing_account_is_preserved(system):
    owner = system.vault.owner()
    system.vault.save(owner, "different-fixture-account-token", "global")
    async with browser(system.app) as client:
        _, headers = await login(client)
        result = await authorize(client, headers)
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "account_link_conflict"
        assert system.vault.credential(owner)[1] == "different-fixture-account-token"
        assert system.vault.browser_link(owner) is None


async def test_linked_browser_cannot_switch_to_another_account(system):
    async with browser(system.app) as client:
        _, headers = await login(client)
        assert (await authorize(client, headers)).status_code == 200
        system.page.actions.clear()
        system.page.handle = "vidu_BOB12345"
        state = await system.billing.session_status("local-user")
        assert state["status"] == "browser_account_changed"
        assert state["video_account_link_verified"] is False
        assert (await authorize(client, headers)).status_code == 409
        assert system.page.actions == []


@pytest.mark.parametrize("state", ["login", "other_modal", "masked"])
async def test_unavailable_official_grant_does_not_fall_back_to_hidden_data(system, state):
    if state == "masked":
        system.page.secret = "*" * 53
    else:
        setattr(system.page, state, True)
    async with browser(system.app) as client:
        _, headers = await login(client)
        result = await authorize(client, headers)
        assert result.status_code == 409
        assert system.vault.credential(system.vault.owner()) is None


async def test_agent_handoff_opens_connection_ui_but_cannot_authorize_by_itself(system):
    async with running(system.app) as url, Client(url + "/mcp", auth=TOKEN) as mcp:
        handoff = (await mcp.call_tool("vidu_connect_account", {})).data
        assert handoff["account_link_verified"] is False
        action = handoff["connection_action"]
        assert action["url"].startswith(ORIGIN + "/#connect=")
        assert action["url"].endswith("&view=account")
        assert SECRET not in json.dumps(handoff)
        tools = {tool.name: tool for tool in await mcp.list_tools()}
        assert not tools["vidu_connect_account"].input_schema.get("properties")
        assert not any("authorize" in name for name in tools)
        assert system.page.actions == []
        assert system.vault.credential(system.vault.owner()) is None
    async with browser(system.app) as client:
        forbidden = await client.post("/v1/account/connection", headers={
            "Authorization": "Bearer limited",
        })
        assert forbidden.status_code == 403


async def test_browser_grant_repr_does_not_include_secret(system):
    async with system.app.state.agent_api.connections.reader.read("local-user") as grant:
        assert SECRET not in repr(grant)
        await grant.verify()
