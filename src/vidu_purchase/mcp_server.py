from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.tools import ToolResult

from vidu_purchase.models import DomainError
from vidu_purchase.service import PurchaseService
from vidu_purchase.workflow import WorkflowService
from vidu_purchase.workflow_tools import register_workflow_tools

if TYPE_CHECKING:
    from vidu_purchase.brief import BriefService

INSTRUCTIONS = """你提供 Vidu 视频请求和官网套餐购买协助。
先读取能力信息，不把演示能力当作真实集成。工作流工具可用时，从 vidu_start_video_request 开始。
保存用户的视频描述和已选参数，之后通过 workflow_id 延续；不要求付款后重述需求。
尚未绑定或登录不等于没有 Vidu 账号；提示已有账号登录、没有账号注册，不可自行断言。
登录和注册在官方页面进行，当前不支持自动创建账号或通过工具接收 Token。
仅在额度不足且用户愿意购买时使用 vidu_recommend_plans，再以用户选择调用 vidu_purchase_for_video。
已有足够额度时，用户当前明确的生成指令可授权提交，不要求先购买。
先读取实时套餐，准确展示首期价格、续费价格、自动续费、积分和币种。
当前版本只支持月付套餐与页面当前选中的旗舰版档位，不支持 API 充值或年付。
用户选定套餐后，用报价 ID 和同一次购买固定的 idempotency_key 准备支付确认页。
付款由用户在 Vidu 官方页面确认，禁止收集密码、验证码或支付卡信息。
准备结账不等于付款成功；跳转成功、用户声称付款或余额变化也不等于已核验订单。
独立购买仅当 vidu_check_purchase 返回 ready 时通知到账。关联视频的购买用 vidu_get_workflow 查询。
关联购买只有返回 awaiting_generation_confirmation 才提出 continuation_prompt。
等待支付时查询同一 purchase_id，不重复准备购买；已付款但权益未生效时不询问继续生成。
付款后禁止自动继续任何视频任务，必须等待用户新的明确生成确认。
用户对继续生成的询问回复“继续”即算新的确认，不要求重复之前的视频需求。
用户暂不继续则调用 vidu_cancel_video_request；更改参数则调用 vidu_update_video_request。
用户明确确认当前参数和费用后，才调用 vidu_confirm_generation；查询状态从不触发生成。
费用变化、确认失效、提交结果不确定时，不得自行确认新费用或重复提交。
demo 数据必须显著注明是演示，不会产生真实视频；browser_experimental 的真实订单核验尚未接入。
这些购买工具只供允许数字服务交易的环境联调，不得用于禁止该行为的公共插件市场。
"""


def authenticated_owner() -> str:
    token = get_access_token()
    if token is None:
        raise ToolError("authentication_required: 请先连接并授权插件。")
    subject = token.subject or token.claims.get("sub")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 512:
        # OAuth client_id identifies an application, not a person.
        raise ToolError("subject_required: 授权凭据缺少用户标识。")
    return subject


def create_server(
    service: PurchaseService,
    *,
    auth=None,
    local_owner: str | None = None,
    commerce_enabled: bool = True,
    workflow: WorkflowService | None = None,
    briefs: BriefService | None = None,
    name: str = "Vidu video assistant development",
    instructions: str = INSTRUCTIONS,
    capabilities: dict | None = None,
    browser_tools: bool = False,
    connections=None,
    panels=None,
) -> FastMCP:
    if auth is not None and local_owner is not None:
        raise ValueError("Authenticated HTTP must not fall back to a local owner")
    if workflow and (
        workflow.purchases is not service or workflow.commerce_enabled != commerce_enabled
    ):
        raise ValueError(
            "Workflow and purchase service must share identity storage and commerce settings"
        )
    if briefs and briefs.workflow is not workflow:
        raise ValueError("Conversation and video must share the same workflow service")
    if briefs:
        from vidu_purchase.brief import CONVERSATION_INSTRUCTIONS

        instructions = CONVERSATION_INSTRUCTIONS + instructions
    mcp = FastMCP(
        name,
        instructions=instructions,
        auth=auth,
        strict_input_validation=True,
        mask_error_details=True,
    )

    def owner() -> str:
        return local_owner if local_owner is not None else authenticated_owner()

    async def invoke(
        operation: Callable[[str], Awaitable[dict]],
        scopes: tuple[str, ...] = ("vidu:purchase",),
    ) -> dict:
        principal = owner()
        if local_owner is None:
            token = get_access_token()
            if token is None or not set(scopes).issubset(set(token.scopes)):
                raise ToolError("insufficient_scope: 当前授权未包含该功能所需的权限。")
        try:
            return await operation(principal)
        except DomainError as error:
            raise ToolError(f"{error.code}: {error}") from None
        except Exception:
            # Upstream/browser exceptions can contain private endpoint credentials.
            raise ToolError("provider_unavailable: 官网连接不可用，请检查绑定会话。") from None

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def vidu_purchase_capabilities() -> dict:
        """查询当前购买集成的真实状态。演示与官网实验模式不能混淆。"""
        owner()
        return {
            "mode": service.provider.mode,
            "commerce_enabled": commerce_enabled,
            "real_payment_verification_available": False,
            "auto_generate": False,
            "post_payment_behavior": "notify_then_ask_generation_confirmation",
            "workflow_available": workflow is not None,
            "video_brief_available": briefs is not None,
            "video_mode": workflow.video.mode if workflow else None,
            "account_status_check_available": workflow is not None,
            "account_registration_available": False,
            "video_generation_available": bool(workflow and workflow.video.generation_enabled),
            "real_video_generation_enabled": bool(
                workflow and workflow.video.mode != "demo" and workflow.video.generation_enabled
            ),
            "embedded_payment_available": False,
            "message": "真实官网订单及权益核验仍待接入；当前不能承诺完整真实购买闭环。",
            **(capabilities or {}),
        }

    if commerce_enabled:

        if browser_tools:

            @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
            async def vidu_commerce_session() -> dict:
                """检查官网购买浏览器是否配置、登录；不是视频 Token 的连接状态。"""
                return await invoke(service.commerce_session)

            @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
            async def vidu_website_orders() -> dict:
                """读取官网可见订单表。payment_verified=false 时不能据此宣布支付成功。"""
                return await invoke(service.website_orders)

        @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
        async def vidu_list_plans() -> dict:
            """读取当前账号的官网月付套餐和有效期三分钟的报价。金额单位为分。"""
            return await invoke(service.list_plans)

        @mcp.tool(
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": True,
            }
        )
        async def vidu_prepare_purchase(quote_id: str, idempotency_key: str) -> dict:
            """用户选定套餐后准备官方支付确认页，不代替用户付款。

            Args:
                quote_id: vidu_list_plans 返回的报价 ID；价格失效后重新获取。
                idempotency_key: 同一次购买固定使用的请求标识，重试时必须保持相同。
            """
            return await invoke(
                lambda principal: service.prepare_purchase(
                    principal,
                    quote_id,
                    idempotency_key,
                )
            )

        @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
        async def vidu_check_purchase(purchase_id: str) -> dict:
            """查询付款及权益到账；ready 时通知并询问是否继续生成，等待用户确认。"""
            return await invoke(lambda principal: service.check_purchase(principal, purchase_id))

    if workflow:
        register_workflow_tools(
            mcp, workflow, invoke, require_explicit_parameters=briefs is not None
        )
    if panels:
        from vidu_purchase.browser_panel import APP_URI, STATIC, PanelRequest

        @mcp.tool(
            app={"resourceUri": APP_URI},
            annotations={"readOnlyHint": False, "destructiveHint": False},
        )
        async def vidu_open_official_panel(
            purpose: str = "account", purchase_id: str | None = None,
        ) -> ToolResult:
            """在对话内展示用户操作的官网面板。account 用于登录/注册；payment 必须传原购买 ID。
            不接收输入内容、密码、验证码、卡号或网址；不代表支付成功，不会生成视频。
            宿主须支持 MCP Apps 并只把 _meta 交给用户界面，不放入模型上下文。
            """
            body = PanelRequest(purpose=purpose, purchase_id=purchase_id)
            result = await invoke(
                lambda principal: panels.issue(principal, get_access_token(), body),
                ("vidu:video", "vidu:purchase"),
            )
            public = {k: v for k, v in result.items() if k != "panel_url"}
            return ToolResult(structured_content=public, meta={"yingbanPanel": result})

        @mcp.resource(
            APP_URI,
            app={"csp": {"frameDomains": [panels.origin]}, "prefersBorder": True},
        )
        def official_panel_app() -> str:
            """映伴官方操作面板；不含账号凭据或会话授权。"""
            return (STATIC / "mcp-panel.html").read_text(encoding="utf-8").replace(
                "__YINGBAN_ORIGIN__", panels.origin
            )

    if connections:

        @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
        async def vidu_connection_status() -> dict:
            """查询官网和视频授权的关联状态，不打开登录窗口或读取 Token。"""
            return await invoke(connections.status, ("vidu:video", "vidu:purchase"))

        @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False})
        async def vidu_connect_account() -> dict:
            """提供本机账号连接窗口。用户在窗口确认授权；不能在聊天里接收任何凭据。"""
            return await invoke(
                lambda principal: connections.begin(principal, get_access_token()),
                ("vidu:video", "vidu:purchase"),
            )
    if briefs:
        from vidu_purchase.brief import register_brief_tools

        register_brief_tools(mcp, briefs, invoke)
    return mcp
