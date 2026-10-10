"""Browser console security and local binding tests; fixtures never ship in the runtime."""

import httpx
import pytest
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from vidu_purchase.credentials import CredentialVault
from vidu_purchase.hosted import create_hosted_app
from vidu_purchase.store import Store
from vidu_purchase.video_models import AccountSnapshot, VideoConfig
from vidu_purchase.video_provider import WebsiteVideoProvider

ORIGIN = "http://127.0.0.1:8787"
TOKEN = "isolated-test-service-token"


class ConsoleKeyringFixture:
    def __init__(self):
        self.values = {}

    def get_password(self, service, owner):
        return self.values.get((service, owner))

    def set_password(self, service, owner, value):
        self.values[service, owner] = value

    def delete_password(self, service, owner):
        del self.values[service, owner]


@pytest.fixture
def console_app(tmp_path):
    vault = CredentialVault(tmp_path / "account", backend=ConsoleKeyringFixture())
    verifier = StaticTokenVerifier(
        tokens={
            TOKEN: {
                "client_id": "local-client",
                "sub": "local-user",
                "scopes": ["vidu:video", "vidu:purchase"],
            }
        }
    )
    video = WebsiteVideoProvider(
        {},
        credentials=lambda _: vault.credential(vault.owner()),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"credits": 21})),
    )
    app = create_hosted_app(
        store=Store(tmp_path / "console.sqlite3"),
        verifier=verifier,
        video=video,
        local_console=True,
        console_vault=vault,
    )
    return app, vault


def browser(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)


async def login(client):
    launch = await client.post("/console/link", headers={"Authorization": "Bearer " + TOKEN})
    assert launch.status_code == 200
    ticket = launch.json()["url"].split("#connect=")[1]
    headers = {"Origin": ORIGIN, "X-Yingban-Console": "1"}
    exchange = await client.post("/console/session", json={"ticket": ticket}, headers=headers)
    assert exchange.status_code == 200
    cookie = exchange.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    headers["X-CSRF-Token"] = exchange.json()["csrf"]
    return ticket, headers


async def test_homepage_has_a_real_interface_and_no_service_secrets(console_app):
    async with browser(console_app[0]) as client:
        page = await client.get("/")
        assert page.status_code == 200 and "Vidu 视频助手" in page.text
        assert 'id="video-form"' in page.text and TOKEN not in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert (await client.get("/assets/console.js")).status_code == 200
        assert (await client.get("/assets/nonexistent.js")).status_code == 404
        assert (await client.post("/console/link")).status_code == 401
        assert (await client.get("/console/api/v1/account")).status_code in {401, 403}


async def test_launch_ticket_is_one_use_and_cookie_does_not_unlock_regular_api(console_app):
    async with browser(console_app[0]) as client:
        ticket, headers = await login(client)
        repeated = await client.post("/console/session", json={"ticket": ticket}, headers=headers)
        assert repeated.status_code == 401
        assert (await client.get("/console/api/v1/account", headers=headers)).status_code == 200
        assert (await client.get("/v1/account")).status_code == 401
        assert (
            await client.get("/console/session", headers={"X-Yingban-Console": "1"})
        ).status_code == 200
        assert TOKEN not in (await client.get("/console/api/v1/capabilities", headers=headers)).text


async def test_cross_origin_and_missing_csrf_cannot_read_or_write(console_app):
    async with browser(console_app[0]) as client:
        _, headers = await login(client)
        for changed in (
            {**headers, "Origin": "https://attacker.example"},
            {**headers, "Sec-Fetch-Site": "cross-site"},
            {**headers, "X-CSRF-Token": "wrong"},
            {"X-Yingban-Console": "1"},
        ):
            assert (await client.get("/console/api/v1/account", headers=changed)).status_code == 403
            assert (await client.delete("/console/account", headers=changed)).status_code == 403


async def test_ui_uses_real_workflow_and_reports_missing_billing(console_app):
    async with browser(console_app[0]) as client:
        _, headers = await login(client)
        headers["Idempotency-Key"] = "console-workflow-001"
        created = await client.post(
            "/console/api/v1/videos",
            json={"config": VideoConfig(prompt="画面").model_dump()}, headers=headers
        )
        assert created.status_code == 201
        assert created.json()["state"] == "account_required"
        assert created.json()["auto_generate"] is False
        assert (await client.get("/console/api/v1/plans", headers=headers)).status_code == 501


async def test_token_is_validated_stored_in_vault_and_disconnect_invalidates_pending_work(
    console_app,
):
    app, vault = console_app

    async def validated(token, region):
        assert token == "fixture-only-secret"
        return AccountSnapshot(
            status="connected", region=region, credits=21, message="test fixture"
        )

    app.state.console.validator = validated
    async with browser(app) as client:
        _, headers = await login(client)
        first = (
            await client.post(
                "/console/api/v1/videos",
                json={"config": VideoConfig(prompt="画面").model_dump()},
                headers={**headers, "Idempotency-Key": "before-connection"},
            )
        ).json()
        saved = await client.post(
            "/console/account",
            json={"region": "global", "token": "fixture-only-secret"},
            headers=headers,
        )
        assert saved.status_code == 200
        assert "fixture-only-secret" not in saved.text
        assert vault.credential(vault.owner())[1] == "fixture-only-secret"
        assert b"fixture-only-secret" not in vault.path.read_bytes()
        assert (await client.delete("/console/account", headers=headers)).status_code == 200
        old = await client.get("/console/api/v1/videos/" + first["workflow_id"], headers=headers)
        assert old.status_code == 409
        assert old.json()["error"]["code"] == "account_binding_changed"


async def test_unverified_token_is_not_saved_and_error_details_are_not_returned(console_app):
    app, vault = console_app

    async def unavailable(_token, _region):
        raise RuntimeError("private-provider-secret")

    app.state.console.validator = unavailable
    async with browser(app) as client:
        _, headers = await login(client)
        result = await client.post(
            "/console/account",
            json={"region": "global", "token": "fixture-only-secret"},
            headers=headers,
        )
        assert result.status_code == 503
        assert "secret" not in result.text
        assert vault.credential(vault.owner()) is None


def test_browser_console_cannot_be_enabled_on_a_public_origin(console_app):
    app, _ = console_app
    with pytest.raises(ValueError, match="cannot be exposed"):
        create_hosted_app(
            store=app.state.agent_api.purchases.store,
            verifier=app.state.console.verifier,
            video=WebsiteVideoProvider({}),
            public_url="https://api.example",
            local_console=True,
        )
