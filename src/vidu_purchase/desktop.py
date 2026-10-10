"""Live-only, single-user desktop MCP entry point for the Yingban plugin."""

import argparse
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from platformdirs import user_data_path

from vidu_purchase.credentials import CredentialVault
from vidu_purchase.desktop_connection import OfficialConnectionManager
from vidu_purchase.models import DomainError
from vidu_purchase.service import PurchaseService
from vidu_purchase.store import Store
from vidu_purchase.video_provider import WebsiteVideoProvider
from vidu_purchase.workflow import WorkflowService
from vidu_purchase.workflow_tools import register_workflow_tools

INSTRUCTIONS = """映伴通过 Vidu 官方开源 CLI 所使用的接口连接用户自己的官网账号。
这是独立开发的工具，不是 Vidu 官方插件。只处理文生视频、费用与任务状态，不提供套餐销售。
先调用 vidu_account_status；未连接不等于用户没有账号。需要连接时调用 vidu_connect_account，
打开返回的官方登录面板，由用户登录或注册，再点击「我已登录，授权并连接」。
不要求用户复制 Vidu Token，不通过聊天、工具参数或智能体浏览器操作收集凭据。
用户确认授权后服务验证并保存到系统凭据库；后续自动连接。服务不可用时说明错误，不回退旧表单。
视频需求使用 vidu_start_video_request 保存，设置模型、时长、分辨率、比例、数量和额度类型。
查询到真实积分费用后向用户确认；用户明确同意当前配置和费用才调用 vidu_confirm_generation。
vidu_get_workflow 只查询原请求，绝不能触发新生成。提交结果不明时停止重复提交。
不足额时准确说明权益不足，不展示套餐、引导订阅、建立结账或宣称付款成功。
用户在外部处理额度后，刷新已有 workflow_id，通知可以使用并询问是否继续生成，等待确认。
费用或配置改变要重新确认。仅在真实任务返回 success 时报告完成；没有链接时说明尚无可用链接。
用户明确要求断开账号时使用 vidu_disconnect_account。切换账号不继承旧账号的生成确认。
"""


class CommerceUnavailable:
    """Reject unsupported operations. This is not a payment provider or simulation."""

    mode = "unavailable"

    async def list_plans(self, *_args):
        raise DomainError("commerce_unavailable", "此插件不提供套餐购买。")

    async def prepare_checkout(self, *_args):
        raise DomainError("commerce_unavailable", "此插件不提供套餐购买。")

    async def check_receipt(self, *_args):
        raise DomainError("commerce_unavailable", "尚无官方订单核验接口。")


def create_desktop_server(data_dir: Path, *, vault=None) -> FastMCP:
    vault = vault or CredentialVault(data_dir)
    connection = OfficialConnectionManager(vault)
    video = WebsiteVideoProvider({}, generation_enabled=True, credentials=vault.credential)
    purchases = PurchaseService(Store(data_dir / "workflows.sqlite3"), CommerceUnavailable())
    workflow = WorkflowService(
        purchases, video, commerce_enabled=False, always_confirm_generation=True
    )

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {}
        finally:
            connection.close()

    mcp = FastMCP(
        "映伴 · Vidu 视频",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        strict_input_validation=True,
        mask_error_details=True,
    )

    async def invoke(operation, _scopes=()):
        # This entry point only exposes stdio to the current OS user, never shared HTTP.
        try:
            return await operation(vault.owner())
        except DomainError as error:
            raise ToolError(f"{error.code}: {error}") from None
        except Exception:
            raise ToolError("vidu_unavailable: 无法可靠取得 Vidu 结果，请稍后重试查询。") from None

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def vidu_capabilities() -> dict:
        """查询本机真实接口版支持的功能及未接入能力。"""
        return {
            "version": "0.3.1",
            "mode": "live",
            "account_authentication": "official_browser_panel_and_os_credential_store",
            "official_browser_panel_configured": connection.configured,
            "manual_token_required": False,
            "text_to_video": True,
            "automatic_registration": False,
            "commerce_available": False,
            "payment_verification_available": False,
            "auto_generate": False,
            "generation_requires_confirmation": True,
            "public_directory_published": False,
            "message": "连接你自己的 Vidu 账号后使用真实接口；本版本没有套餐交易功能。",
        }

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    async def vidu_connect_account() -> dict:
        """打开官方登录面板，由用户登录并确认授权；不用复制 Token，不接收任何秘密。"""
        try:
            return await connection.begin()
        except DomainError as error:
            raise ToolError(f"{error.code}: {error}") from None

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def vidu_disconnect_account() -> dict:
        """用户要求断开时删除本机保存的授权。不会取消已提交的视频任务或官网订阅。"""
        try:
            connection.close()
            vault.disconnect()
        except DomainError as error:
            raise ToolError(f"{error.code}: {error}") from None
        return {"status": "disconnected", "message": "已删除本机保存的 Vidu 授权。"}

    register_workflow_tools(mcp, workflow, invoke)
    return mcp


def main():
    parser = argparse.ArgumentParser(description="映伴 Vidu 真实接口桌面 MCP")
    parser.add_argument("--data-dir", type=Path, default=user_data_path("YingbanVidu", "Yingban"))
    args = parser.parse_args()
    create_desktop_server(args.data_dir).run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
