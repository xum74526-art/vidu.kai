"""Lifecycle and isolation tests; test launchers never contact Vidu or fake payments."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from test_console import ConsoleKeyringFixture

from vidu_purchase.browser import ViduBrowserProvider, account_reference
from vidu_purchase.browser_pool import BrowserPool
from vidu_purchase.credentials import AccountVaults
from vidu_purchase.hosted import create_hosted_app
from vidu_purchase.models import DomainError
from vidu_purchase.store import Store
from vidu_purchase.video_provider import WebsiteVideoProvider


class LauncherFixture:
    def __init__(self):
        self.calls = []
        self.close = AsyncMock()

    async def __call__(self, profile):
        await asyncio.sleep(0)
        handle = SimpleNamespace(close=AsyncMock())
        self.calls.append((profile, handle))
        return f"http://127.0.0.1:{9300 + len(self.calls)}", handle


async def test_concurrent_requests_share_only_the_same_users_browser(tmp_path):
    launcher = LauncherFixture()
    pool = BrowserPool(tmp_path, launcher=launcher)
    a, duplicate, b = await asyncio.gather(
        pool.ensure("alice"), pool.ensure("alice"), pool.ensure("bob"),
    )
    assert a is duplicate and a.cdp_url != b.cdp_url and a.session_id != b.session_id
    assert len(launcher.calls) == 2
    assert all(path.is_relative_to(tmp_path) for path, _ in launcher.calls)
    await pool.close()
    assert all(handle.close.await_count == 1 for _, handle in launcher.calls)


async def test_restarts_preserve_identity_without_reusing_old_cdp_endpoint(tmp_path):
    launcher = LauncherFixture()
    pool = BrowserPool(tmp_path, launcher=launcher)
    first = await pool.ensure("alice")
    await pool.close()
    restarted = BrowserPool(tmp_path, launcher=launcher)
    second = await restarted.ensure("alice")
    assert first.cdp_url != second.cdp_url and first.session_id == second.session_id
    assert account_reference("alice", first, "vidu_abc123") == account_reference(
        "alice", second, "vidu_abc123",
    )
    await restarted.close()


async def test_active_panel_and_leases_are_not_evicted(tmp_path):
    now = [0]
    pool = BrowserPool(tmp_path, max_active=1, idle_seconds=60,
                       clock=lambda: now[0], launcher=LauncherFixture())
    await pool.ensure("alice")
    now[0] = 100
    pool.protected = lambda: {"alice"}
    with pytest.raises(DomainError) as full:
        await pool.ensure("bob")
    assert full.value.code == "browser_capacity"
    pool.protected = lambda: set()
    async with pool.lease("alice"):
        now[0] = 200
        await pool.reap()
        assert "alice" in pool.bindings
    now[0] = 300
    await pool.ensure("bob")
    assert set(pool.bindings) == {"bob"}
    await pool.close()


async def test_pool_refuses_second_process_and_does_not_reuse_missing_profile(tmp_path):
    launcher = LauncherFixture()
    first = BrowserPool(tmp_path, launcher=launcher)
    binding = await first.ensure("alice")
    second = BrowserPool(tmp_path, launcher=LauncherFixture())
    with pytest.raises(DomainError) as collision:
        await second.ensure("bob")
    assert collision.value.code == "browser_pool_in_use"
    await first.close()
    launcher.calls[0][0].rmdir()  # Empty test profile only, never a real browser.
    replacement = await second.ensure("alice")
    assert replacement.session_id != binding.session_id
    await second.close()


async def test_authenticated_new_users_get_isolated_panels_without_an_operator_mapping(tmp_path):
    launcher = LauncherFixture()
    pool = BrowserPool(tmp_path / "profiles", launcher=launcher)
    provider = ViduBrowserProvider({}, pool=pool)
    vaults = AccountVaults(tmp_path / "accounts", (), backend=ConsoleKeyringFixture(),
                          authenticated_users=True)
    verifier = StaticTokenVerifier(tokens={
        name: {"client_id": "test", "sub": name, "scopes": scopes}
        for name, scopes in {
            "alice": ["vidu:video", "vidu:purchase"],
            "bob": ["vidu:video", "vidu:purchase"], "limited": ["vidu:video"],
        }.items()
    })
    app = create_hosted_app(
        store=Store(tmp_path / "store.sqlite3"), verifier=verifier,
        video=WebsiteVideoProvider({}, credentials=vaults.credential),
        billing=provider, account_vaults=vaults,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://127.0.0.1:8787") as api:
        for token in ("invalid", "limited"):
            denied = await api.post("/v1/browser-panels", json={"purpose": "account"},
                                    headers={"Authorization": f"Bearer {token}"})
            assert denied.status_code in {401, 403}
        assert not launcher.calls
        for name in ("alice", "bob"):
            panel = await api.post("/v1/browser-panels", json={"purpose": "account"},
                                   headers={"Authorization": f"Bearer {name}"})
            assert panel.status_code == 200
            assert "930" not in panel.text and "cdp" not in panel.text
        assert len(launcher.calls) == 2
        assert vaults.revision("alice") != vaults.revision("bob")
        assert app.state.agent_api.capabilities["automatic_user_sessions"] is True
    await app.state.panels.close()
    await pool.close()
