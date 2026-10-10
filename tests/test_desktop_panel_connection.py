"""Installed entry-point regressions; all fixtures are isolated from real accounts."""

import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from test_console import ConsoleKeyringFixture

from vidu_purchase import desktop
from vidu_purchase.browser import BrowserBinding, ViduBrowserProvider
from vidu_purchase.credentials import CredentialVault
from vidu_purchase.desktop_connection import OfficialConnectionManager, register_official_panel
from vidu_purchase.hosted import create_hosted_app
from vidu_purchase.models import DomainError
from vidu_purchase.store import Store
from vidu_purchase.video_provider import WebsiteVideoProvider

ORIGIN = "http://127.0.0.1:8787"
IDENTITY = "a" * 24 + ":8787"
TOKEN = "test-service-secret-never-in-tool-result"


@pytest.fixture
def bridge(tmp_path):
    vault = CredentialVault(tmp_path / "account", backend=ConsoleKeyringFixture())
    vault.backend.set_password("yingban-selfhost/loopback", IDENTITY, TOKEN)
    register_official_panel(vault.path.parent, ORIGIN, IDENTITY)
    verifier = StaticTokenVerifier(tokens={TOKEN: {
        "client_id": "test", "sub": "local-user", "scopes": ["vidu:video", "vidu:purchase"],
    }})
    provider = ViduBrowserProvider({"local-user": BrowserBinding("http://127.0.0.1:9222")})
    app = create_hosted_app(
        store=Store(tmp_path / "workflows.sqlite3"), verifier=verifier,
        video=WebsiteVideoProvider({}), billing=provider, local_console=True, console_vault=vault,
    )
    transport = httpx.ASGITransport(app=app)
    manager = OfficialConnectionManager(vault, transport=transport)
    return app, vault, manager, transport


async def test_plugin_connect_opens_official_panel_without_a_token_form(bridge, monkeypatch):
    app, vault, manager, transport = bridge
    monkeypatch.setattr(desktop, "OfficialConnectionManager", lambda _vault, **_kw: manager)
    async with Client(desktop.create_desktop_server(vault.path.parent, vault=vault)) as client:
        result = (await client.call_tool("vidu_connect_account", {})).data
        assert result["connection_mode"] == "official_browser_panel"
        assert result["manual_token_required"] is False
        assert result["auto_generate"] is False
        assert TOKEN not in json.dumps(result)
        assert result["connection_url"].startswith(ORIGIN + "/panel#ticket=")
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as browser:
            page = await browser.get("/panel")
            assert "我已登录，授权并连接" in page.text
            assert 'name="token"' not in page.text
        assert len(app.state.panels.tickets) == 1
        assert vault.credential(vault.owner()) is None


async def test_stale_shared_account_is_rejected_and_disconnect_revokes_panel(bridge):
    app, vault, manager, transport = bridge
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as browser:
        headers = {"Authorization": "Bearer " + TOKEN, "X-Yingban-Console": "1"}
        denied = await browser.post("/console/desktop-panel", headers=headers,
                                    json={"binding_revision": "0" * 32})
        assert denied.status_code == 409
        assert denied.json()["error"]["code"] == "account_binding_changed"
        result = await manager.begin()
        vault.disconnect()
        stale = await browser.post(
            "/panel/session",
            headers={"Origin": ORIGIN, "X-Yingban-Panel": "1"},
            json={"ticket": result["connection_url"].split("#ticket=")[1]},
        )
        assert stale.status_code == 401
        assert not app.state.panels.sessions


async def test_desktop_panel_requires_service_auth_and_rejects_cross_origin(bridge):
    _, vault, _, transport = bridge
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as browser:
        for headers, status in [
            ({"X-Yingban-Console": "1"}, 401),
            ({"X-Yingban-Console": "1", "Authorization": "Bearer wrong"}, 401),
            ({"X-Yingban-Console": "1", "Authorization": "Bearer " + TOKEN,
              "Origin": "https://evil.example"}, 403),
        ]:
            response = await browser.post("/console/desktop-panel", headers=headers,
                                          json={"binding_revision": vault.owner()})
            assert response.status_code == status
            assert TOKEN not in response.text


async def test_connection_never_falls_back_to_legacy_form_or_remote_host(tmp_path):
    vault = CredentialVault(tmp_path, backend=ConsoleKeyringFixture())
    manager = OfficialConnectionManager(vault)
    with pytest.raises(DomainError) as absent:
        await manager.begin()
    assert absent.value.code == "official_panel_not_configured"
    manager.path.write_text(json.dumps({
        "origin": "https://evil.example", "credential_account": IDENTITY,
    }), encoding="utf-8")
    with pytest.raises(DomainError) as remote:
        await manager.begin()
    assert remote.value.code == "official_panel_configuration_invalid"


async def test_failure_does_not_leak_secrets_or_reuse_old_connection(bridge):
    _, _, manager, _ = bridge

    async def fail(_request):
        raise httpx.ConnectError(TOKEN)

    manager.transport = httpx.MockTransport(fail)
    with pytest.raises(DomainError) as error:
        await manager.begin()
    assert error.value.code == "official_panel_unavailable"
    assert TOKEN not in str(error.value)
