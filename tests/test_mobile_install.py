"""Remote installation and auth discovery, using isolated signed test identities."""

import json

import httpx
import pytest
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import RSAKeyPair, StaticTokenVerifier
from test_hosted import running

from vidu_purchase.hosted import AgentJWTVerifier, create_hosted_app
from vidu_purchase.remote_plugin import MANIFEST_PATH, installation_routes
from vidu_purchase.store import Store
from vidu_purchase.video_provider import WebsiteVideoProvider

ORIGIN = "https://video.example"


async def test_local_plugin_does_not_offer_a_phone_installation(tmp_path):
    app = create_hosted_app(
        store=Store(tmp_path / "local.sqlite3"), verifier=StaticTokenVerifier(tokens={}),
        video=WebsiteVideoProvider({}),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1",
    ) as api:
        response = await api.get(MANIFEST_PATH)
        manifest = response.json()
        assert manifest["installable"] is False and manifest["mcp"] is None
        assert "public_https_service" in manifest["missing_configuration"]
        assert "user_authentication" in manifest["missing_configuration"]
        assert "attachment" in response.headers["content-disposition"]
        assert "token" not in response.text and "9222" not in response.text


async def test_configured_download_contains_only_public_connection_information():
    endpoint = installation_routes(
        ORIGIN, authorization_servers=["https://login.example"],
        browser_accounts=True, generation_enabled=True,
    )[0].endpoint
    response = await endpoint(None)
    manifest = json.loads(response.body)
    assert manifest["installable"] is True
    assert manifest["mcp"]["url"] == ORIGIN + "/mcp"
    assert manifest["auth"]["resource"] == ORIGIN + "/mcp"
    assert manifest["missing_configuration"] == []
    assert manifest["generation"]["auto_retry_submission"] is False


async def test_remote_oauth_discovery_and_signed_mobile_client(tmp_path):
    keys = RSAKeyPair.generate()
    verifier = AgentJWTVerifier(
        public_key=keys.public_key, issuer="https://login.example", audience=ORIGIN + "/mcp",
    )
    app = create_hosted_app(
        store=Store(tmp_path / "remote.sqlite3"), verifier=verifier,
        video=WebsiteVideoProvider({}), public_url=ORIGIN,
        authorization_servers=["https://login.example"],
        cors_origins=["https://app.example"],
    )
    async with running(app) as url:
        async with httpx.AsyncClient(base_url=url) as api:
            metadata = await api.get("/.well-known/oauth-protected-resource/mcp")
            assert metadata.status_code == 200
            assert metadata.json()["resource"] == ORIGIN + "/mcp"
            assert metadata.json()["authorization_servers"] == ["https://login.example/"]
            assert "vidu:video" in metadata.json()["scopes_supported"]
            denied = await api.post("/mcp", json={})
            assert denied.status_code == 401
            assert "resource_metadata" in denied.headers["www-authenticate"]
            descriptor = (await api.get(MANIFEST_PATH)).json()
            assert descriptor["installable"] is False
            assert "per_user_browser_accounts" in descriptor["missing_configuration"]
        token = keys.create_token(
            subject="mobile-user", issuer="https://login.example", audience=ORIGIN + "/mcp",
            scopes=["vidu:video", "vidu:purchase"],
        )
        async with Client(url + "/mcp", auth=token) as mcp:
            account = (await mcp.call_tool("vidu_account_status", {})).data
            assert account["status"] == "not_connected"
            tools = {tool.name for tool in await mcp.list_tools()}
            assert {
                "vidu_open_official_panel", "vidu_get_workflow", "vidu_confirm_generation",
            } <= tools


@pytest.mark.parametrize("issuer", ["http://login.example", "https://user:secret@login.example",
                                    "https://login.example/?token=secret"])
def test_untrusted_auth_metadata_is_not_published(tmp_path, issuer):
    with pytest.raises(ValueError, match="HTTPS"):
        create_hosted_app(
            store=Store(tmp_path / "invalid.sqlite3"), verifier=StaticTokenVerifier(tokens={}),
            video=WebsiteVideoProvider({}), public_url=ORIGIN, authorization_servers=[issuer],
        )
