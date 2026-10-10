import asyncio
import socket
from contextlib import asynccontextmanager

import httpx2
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from vidu_purchase.demo import DemoProvider
from vidu_purchase.demo_video import DemoVideoProvider
from vidu_purchase.server import create_server
from vidu_purchase.service import PurchaseService
from vidu_purchase.store import Store
from vidu_purchase.workflow import WorkflowService


@asynccontextmanager
async def running_server(tmp_path):
    verifier = StaticTokenVerifier(
        tokens={
            "alice-test": {
                "client_id": "same-client",
                "sub": "alice",
                "scopes": ["vidu:purchase", "vidu:video"],
            },
            "bob-test": {
                "client_id": "same-client",
                "sub": "bob",
                "scopes": ["vidu:purchase", "vidu:video"],
            },
            "purchase-only": {
                "client_id": "same-client",
                "sub": "alice",
                "scopes": ["vidu:purchase"],
            },
            "video-only": {"client_id": "same-client", "sub": "alice", "scopes": ["vidu:video"]},
            "no-subject": {"client_id": "same-client", "scopes": ["vidu:purchase"]},
        },
        required_scopes=[],
    )
    service = PurchaseService(Store(tmp_path / "http.db"), DemoProvider())
    mcp = create_server(
        service, auth=verifier, workflow=WorkflowService(service, DemoVideoProvider())
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(mcp.http_app(path="/mcp"), log_level="error", lifespan="on")
    server = uvicorn.Server(config)
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
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()


async def test_http_verified_subject_isolates_users_sharing_an_oauth_client(tmp_path):
    async with running_server(tmp_path) as url:
        async with Client(url, auth="alice-test") as alice:
            quotes = (await alice.call_tool("vidu_list_plans", {})).data
            quote_id = quotes["quotes"][0]["quote_id"]
        async with Client(url, auth="bob-test") as bob:
            with pytest.raises(ToolError, match="quote_not_found"):
                await bob.call_tool(
                    "vidu_prepare_purchase",
                    {
                        "quote_id": quote_id,
                        "idempotency_key": "bob-tries-alice",
                    },
                )
        async with Client(url, auth="alice-test") as alice:
            order = (
                await alice.call_tool(
                    "vidu_prepare_purchase",
                    {
                        "quote_id": quote_id,
                        "idempotency_key": "alice-own-order",
                    },
                )
            ).data
            assert order["state"] == "awaiting_payment_confirmation"


async def test_http_client_id_is_not_used_as_a_user_identity(tmp_path):
    async with running_server(tmp_path) as url:
        async with Client(url, auth="no-subject") as client:
            with pytest.raises(ToolError, match="subject_required"):
                await client.call_tool("vidu_list_plans", {})


async def test_http_invalid_token_cannot_call_tools(tmp_path):
    async with running_server(tmp_path) as url:
        async with httpx2.AsyncClient() as client:
            response = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {},
                },
                headers={
                    "Authorization": "Bearer invalid-token",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert response.status_code == 401


async def test_http_workflow_user_cannot_read_another_users_prompt(tmp_path):
    async with running_server(tmp_path) as url:
        async with Client(url, auth="alice-test") as alice:
            request = (
                await alice.call_tool(
                    "vidu_start_video_request",
                    {
                        "config": {"prompt": "Alice private product description"},
                        "idempotency_key": "alice-private-video",
                    },
                )
            ).data
        async with Client(url, auth="bob-test") as bob:
            with pytest.raises(ToolError, match="workflow_not_found"):
                await bob.call_tool("vidu_get_workflow", {"workflow_id": request["workflow_id"]})


async def test_http_purchase_and_video_permissions_are_separate(tmp_path):
    async with running_server(tmp_path) as url:
        async with Client(url, auth="purchase-only") as client:
            await client.call_tool("vidu_list_plans", {})
            with pytest.raises(ToolError, match="insufficient_scope"):
                await client.call_tool(
                    "vidu_start_video_request",
                    {"config": {"prompt": "视频"}, "idempotency_key": "permission-check"},
                )
        async with Client(url, auth="video-only") as client:
            await client.call_tool("vidu_account_status", {})
            with pytest.raises(ToolError, match="insufficient_scope"):
                await client.call_tool("vidu_list_plans", {})
