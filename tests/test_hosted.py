import asyncio
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import RSAKeyPair

from vidu_purchase.hosted import AgentJWTVerifier, create_hosted_app
from vidu_purchase.video_models import VideoConfig
from vidu_purchase.video_provider import WebsiteVideoProvider


def rest(app, token="alice"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers={"Authorization": f"Bearer {token}"},
    )


async def buy(client, key="purchase-request-001"):
    catalog = (await client.get("/v1/plans")).json()
    return await client.post(
        "/v1/purchases",
        json={"quote_id": catalog["quotes"][0]["quote_id"]},
        headers={"Idempotency-Key": key},
    )


@asynccontextmanager
async def running(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("HTTP server did not start")
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()


async def test_unconfigured_live_service_returns_no_fictional_plans(protocol_system):
    _, fixture, verifier, store, _, _ = protocol_system
    app = create_hosted_app(store=store, verifier=verifier, video=WebsiteVideoProvider({}))
    async with rest(app) as client:
        caps = (await client.get("/v1/capabilities")).json()
        assert caps["billing_adapter_configured"] is False
        assert caps["real_payment_verification_available"] is False
        plans = await client.get("/v1/plans")
        assert plans.status_code == 501
        assert plans.json()["error"]["code"] == "billing_not_configured"
        account = (await client.get("/v1/account")).json()
        assert account["status"] == "not_connected" and account["credits"] is None
        video = (
            await client.post(
                "/v1/videos",
                json={"config": VideoConfig(prompt="真实请求").model_dump()},
                headers={"Idempotency-Key": "new-video-001"},
            )
        ).json()
        assert video["state"] == "account_required"
    assert fixture.calls == []


@pytest.mark.parametrize("token", ["invalid", "no-subject", "expired"])
async def test_rest_requires_verified_person_identity(protocol_system, token):
    async with rest(protocol_system[0], token) as client:
        assert (await client.get("/healthz")).status_code == 200
        response = await client.get("/v1/plans")
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"


async def test_rest_scopes_ownership_input_and_host_boundaries(protocol_system):
    app = protocol_system[0]
    async with rest(app) as alice, rest(app, "bob") as bob:
        quote = (await alice.get("/v1/plans")).json()["quotes"][0]
        assert "account_ref" not in quote["plan"] and "source_text" not in quote["plan"]
        wrong_owner = await bob.post(
            "/v1/purchases",
            json={"quote_id": quote["quote_id"]},
            headers={"Idempotency-Key": "bob-reads-alice"},
        )
        assert wrong_owner.status_code == 404
        purchase = (await buy(alice)).json()
        for suffix in ("", "/events"):
            assert (
                await bob.get(f"/v1/purchases/{purchase['purchase_id']}{suffix}")
            ).status_code == 404
        injected = await alice.post(
            "/v1/purchases",
            json={"quote_id": quote["quote_id"], "payment_succeeded": True},
            headers={"Idempotency-Key": "injected-purchase"},
        )
        assert injected.status_code == 422
        assert (await alice.post("/v1/mark-paid", json={})).status_code == 404
        oversized = await alice.post("/v1/purchases", content=b"x" * 65537)
        assert oversized.status_code == 413
        assert (
            await alice.get("/healthz", headers={"Host": "attacker.example"})
        ).status_code == 400
    async with rest(app, "purchase-only") as limited:
        assert (await limited.get("/v1/account")).status_code == 403


async def test_payment_updates_do_not_submit_and_confirmation_submits_only_once(protocol_system):
    app, fixture, *_ = protocol_system
    async with rest(app) as client:
        first = (
            await client.post(
                "/v1/videos",
                json={"config": VideoConfig(prompt="产品视频", duration=10).model_dump()},
                headers={"Idempotency-Key": "video-request-001"},
            )
        ).json()
        assert first["state"] == "needs_purchase"
        path = f"/v1/videos/{first['workflow_id']}"
        plans = (await client.post(path + "/recommend")).json()
        body = {"quote_id": plans["recommended_quote_id"], "revision": plans["revision"]}
        pending = (await client.post(path + "/purchase", json=body)).json()
        repeated = (await client.post(path + "/purchase", json=body)).json()
        assert pending["purchase_id"] == repeated["purchase_id"]
        assert fixture.post_count("/v1/checkouts") == 1
        assert pending["state"] == "awaiting_payment"
        fixture.payment = 1
        assert (await client.get(path)).json()["state"] == "awaiting_entitlement"
        fixture.payment, fixture.credits = 2, 80
        ready = (await client.get(path)).json()
        assert ready["state"] == "awaiting_generation_confirmation"
        assert ready["auto_generate"] is False
        assert ready["continuation_prompt"] == "是否继续生成刚才的视频？"
        assert fixture.post_count("/vidu/v1/tasks") == 0
        wrong = await client.post(path + "/confirm", json={"confirmation_id": "0" * 32})
        assert wrong.status_code == 409
        body = {"confirmation_id": ready["confirmation_id"]}
        assert (await client.post(path + "/confirm", json=body)).json()["state"] == "generating"
        await client.post(path + "/confirm", json=body)
        assert fixture.post_count("/vidu/v1/tasks") == 1
        assert (await client.get(path)).json()["state"] == "completed"


async def test_sse_reports_verified_payment_then_entitlement_without_generation(protocol_system):
    app, fixture, *_ = protocol_system
    async with rest(app) as client:
        purchase = (await buy(client)).json()
        fixture.receipt_steps.extend([1, 2])
        response = await client.get(f"/v1/purchases/{purchase['purchase_id']}/events")
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"state":"paid_pending_entitlement"' in response.text
        assert '"state":"ready"' in response.text
        assert fixture.post_count("/vidu/v1/tasks") == 0
        assert fixture.post_count("/v1/checkouts") == 1


async def test_http_and_mcp_share_state_and_identity_over_real_local_transport(protocol_system):
    app, fixture, *_ = protocol_system
    async with running(app) as url:
        async with httpx.AsyncClient(
            base_url=url, headers={"Authorization": "Bearer alice"}
        ) as client:
            purchase = (await buy(client)).json()
            caps = (await client.get("/v1/capabilities")).json()
        async with Client(url + "/mcp", auth="alice") as mcp:
            tools = await mcp.list_tools()
            assert len(tools) == 20
            mcp_caps = (await mcp.call_tool("vidu_purchase_capabilities", {})).data
            assert all(mcp_caps[key] == value for key, value in caps.items())
            result = (
                await mcp.call_tool(
                    "vidu_check_purchase",
                    {
                        "purchase_id": purchase["purchase_id"],
                    },
                )
            ).data
            assert result["purchase_id"] == purchase["purchase_id"]
        async with Client(url + "/mcp", auth="bob") as mcp:
            with pytest.raises(ToolError, match="purchase_not_found"):
                await mcp.call_tool("vidu_check_purchase", {"purchase_id": purchase["purchase_id"]})
        assert fixture.post_count("/v1/checkouts") == 1


async def test_openapi_references_and_explicit_cors_origins(protocol_system):
    _, _, verifier, store, video, _ = protocol_system
    app = create_hosted_app(
        store=store,
        verifier=verifier,
        video=video,
        cors_origins=["https://agent.example"],
    )
    async with rest(app) as client:
        schema = (await client.get("/v1/openapi.json")).json()
        assert sum(len(operations) for operations in schema["paths"].values()) == 22

        def references(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "$ref":
                        assert child.rsplit("/", 1)[1] in schema["components"]["schemas"]
                    references(child)
            elif isinstance(value, list):
                for child in value:
                    references(child)

        references(schema)
        for origin, status in [("https://agent.example", 200), ("https://evil.example", 400)]:
            response = await client.options(
                "/v1/plans",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization",
                },
            )
            assert response.status_code == status
            assert response.headers.get("access-control-allow-origin") != "*"


async def test_jwt_signature_issuer_audience_and_expiry_are_enforced(protocol_system):
    _, _, _, store, video, _ = protocol_system
    keys = RSAKeyPair.generate()
    verifier = AgentJWTVerifier(
        public_key=keys.public_key, issuer="https://issuer.example", audience="yingban-api"
    )
    app = create_hosted_app(store=store, verifier=verifier, video=video)
    good = {
        "subject": "alice",
        "issuer": "https://issuer.example",
        "audience": "yingban-api",
        "scopes": ["vidu:video"],
    }
    for changes in (
        {"issuer": "https://evil.example"},
        {"audience": "some-other-api"},
        {"expires_in_seconds": -120},
        {"subject": ""},
        {"additional_claims": {"exp": None}},
        {"additional_claims": {"nbf": 9999999999}},
    ):
        async with rest(app, keys.create_token(**{**good, **changes})) as client:
            assert (await client.get("/v1/account")).status_code == 401
    async with rest(app, RSAKeyPair.generate().create_token(**good)) as client:
        assert (await client.get("/v1/account")).status_code == 401
    async with rest(app, keys.create_token(**good)) as client:
        assert (await client.get("/v1/account")).status_code == 200
        assert (await client.get("/v1/plans")).status_code == 403
