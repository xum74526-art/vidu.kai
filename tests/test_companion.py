"""The desktop helper exposes account UI only, with explicit grant and native storage."""

import json

import httpx
from test_browser_pool import LauncherFixture
from test_console import ConsoleKeyringFixture

from vidu_purchase.browser_pool import BrowserPool
from vidu_purchase.companion import create_companion
from vidu_purchase.credentials import CredentialVault
from vidu_purchase.desktop_connection import OfficialConnectionManager, register_official_panel


async def test_companion_first_connect_allocates_browser_and_no_billing_routes(tmp_path):
    origin, identity, secret = "http://127.0.0.1:8787", "a" * 24 + ":8787", "isolated-test-secret"
    vault = CredentialVault(tmp_path, backend=ConsoleKeyringFixture())
    vault.backend.set_password("yingban-selfhost/loopback", identity, secret)
    register_official_panel(tmp_path, origin, identity)
    launcher = LauncherFixture()
    pool = BrowserPool(tmp_path / "profiles", launcher=launcher)
    app = create_companion(vault, origin, secret, pool=pool)
    transport = httpx.ASGITransport(app=app)
    manager = OfficialConnectionManager(vault, transport=transport)
    async with httpx.AsyncClient(transport=transport, base_url=origin) as client:
        assert (await client.get("/healthz", headers={"X-Yingban-Console": "1"})).status_code == 401
        for path in ("/v1/plans", "/mcp", "/console/account", "/assets/console.js"):
            assert (await client.get(path)).status_code == 404
        headers = {"Authorization": "Bearer " + secret, "X-Yingban-Console": "1"}
        denied = await client.post("/console/desktop-panel", headers={
            **headers, "Origin": "https://untrusted.example",
        }, json={"binding_revision": vault.owner()})
        assert denied.status_code == 403 and not launcher.calls
        result = await manager.begin()
        assert result["connection_mode"] == "official_browser_panel"
        assert secret not in json.dumps(result)
        assert len(launcher.calls) == 1 and vault.credential(vault.owner()) is None
        assert (await client.get("/assets/panel-input.js")).status_code == 200
        assert "Vidu Token" not in (await client.get("/panel")).text
        huge = await client.post("/panel/input", content=b"x" * 65537)
        assert huge.status_code == 413
    await app.state.panels.close()
    await pool.close()
