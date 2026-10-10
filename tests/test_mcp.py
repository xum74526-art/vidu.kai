import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from vidu_purchase.demo import DemoProvider
from vidu_purchase.server import create_server
from vidu_purchase.service import PurchaseService
from vidu_purchase.store import Store


async def test_mcp_schema_and_purchase_flow(tmp_path):
    provider = DemoProvider()
    service = PurchaseService(Store(tmp_path / "mcp.db"), provider)
    mcp = create_server(service, local_owner="alice")
    async with Client(mcp) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == {
            "vidu_purchase_capabilities",
            "vidu_list_plans",
            "vidu_prepare_purchase",
            "vidu_check_purchase",
        }
        for tool in tools:
            assert "owner" not in tool.input_schema.get("properties", {})
            assert "paid" not in tool.input_schema.get("properties", {})
            assert "generate" not in tool.name
        catalog = (await client.call_tool("vidu_list_plans", {})).data
        result = (
            await client.call_tool(
                "vidu_prepare_purchase",
                {
                    "quote_id": catalog["quotes"][0]["quote_id"],
                    "idempotency_key": "one-intent-only",
                },
            )
        ).data
        assert result["auto_generate"] is False
        assert result["continuation_prompt"] is None
        purchase = service.store.purchase("alice", result["purchase_id"])
        provider.confirm_for_test(purchase)
        ready = (
            await client.call_tool(
                "vidu_check_purchase",
                {
                    "purchase_id": result["purchase_id"],
                },
            )
        ).data
        assert ready["state"] == "ready"
        assert ready["continuation_prompt"] == "是否继续生成视频？"
        assert ready["requires_generation_confirmation"] is True
        assert ready["auto_generate"] is False
        assert ready["requires_new_video_instruction"] is True


async def test_missing_identity_is_rejected(tmp_path):
    service = PurchaseService(Store(tmp_path / "auth.db"), DemoProvider())
    async with Client(create_server(service)) as client:
        with pytest.raises(ToolError, match="authentication_required"):
            await client.call_tool("vidu_list_plans", {})


async def test_restricted_distribution_hides_all_commerce_tools(tmp_path):
    service = PurchaseService(Store(tmp_path / "restricted.db"), DemoProvider())
    async with Client(
        create_server(service, local_owner="alice", commerce_enabled=False)
    ) as client:
        assert [tool.name for tool in await client.list_tools()] == ["vidu_purchase_capabilities"]


def test_authenticated_http_cannot_silently_use_fixed_local_user(tmp_path):
    service = PurchaseService(Store(tmp_path / "auth.db"), DemoProvider())
    with pytest.raises(ValueError, match="fall back"):
        create_server(service, auth=object(), local_owner="alice")
