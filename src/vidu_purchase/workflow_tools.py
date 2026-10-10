"""Explicit MCP operations for the video workflow; no generic command executor."""

from vidu_purchase.video_models import ExplicitVideoConfig, VideoConfig
from vidu_purchase.workflow import WorkflowService


def register_workflow_tools(
    mcp, workflow: WorkflowService, invoke, *, require_explicit_parameters=False
) -> None:
    read = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
    write = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
    config_type = ExplicitVideoConfig if require_explicit_parameters else VideoConfig

    @mcp.tool(annotations=read)
    async def vidu_account_status() -> dict:
        """检查当前用户的 Vidu 连接状态。未连接不等于没有账号，不创建账号或收集凭据。"""
        return await invoke(workflow.account_status, ("vidu:video",))

    @mcp.tool(annotations={**write, "idempotentHint": True})
    async def vidu_start_video_request(config: config_type, idempotency_key: str) -> dict:
        """保存已选好全部参数的视频需求并检查账号与额度，不购买、不提交生成。

        对话参数尚未明确且 vidu_prepare_video_request 可用时，先使用参数对话入口。
        相同需求重试复用标识，不把未选择的参数当作用户已经接受默认值。
        """
        return await invoke(
            lambda owner: workflow.start(owner, config, idempotency_key), ("vidu:video",)
        )

    @mcp.tool(annotations=write)
    async def vidu_update_video_request(
        workflow_id: str, config: config_type, revision: int
    ) -> dict:
        """更新尚未提交的视频配置，传最新 revision；旧生成确认失效，重新检查费用。"""
        return await invoke(
            lambda owner: workflow.update(owner, workflow_id, config, revision), ("vidu:video",)
        )

    @mcp.tool(annotations=write)
    async def vidu_get_workflow(workflow_id: str) -> dict:
        """查询并保存账号、支付或视频任务进度，不下单或生成。支付待核验时不能确认到账。"""
        return await invoke(lambda owner: workflow.get(owner, workflow_id), ("vidu:video",))

    @mcp.tool(annotations=write)
    async def vidu_cancel_video_request(workflow_id: str, revision: int) -> dict:
        """用户暂不继续时结束未提交的视频请求；不会取消官网订单、订阅或退款。"""
        return await invoke(
            lambda owner: workflow.cancel(owner, workflow_id, revision), ("vidu:video",)
        )

    @mcp.tool(annotations={**write, "destructiveHint": True, "idempotentHint": True})
    async def vidu_confirm_generation(workflow_id: str, confirmation_id: str) -> dict:
        """按已确认配置提交视频，会消耗 Vidu 额度。购买后必须先询问并得到用户新的明确确认。

        使用最新工作流返回的 confirmation_id。用户说“继续”可确认此前配置；支付不能视为
        生成确认。费用或配置变化时返回新确认要求，不自动重试提交结果不明的任务。
        """
        return await invoke(
            lambda owner: workflow.confirm(owner, workflow_id, confirmation_id), ("vidu:video",)
        )

    if workflow.commerce_enabled:

        @mcp.tool(annotations=write)
        async def vidu_recommend_plans(workflow_id: str) -> dict:
            """额度不足且用户愿意购买时比较实时月付套餐，按本次预计积分消耗筛选，不下单。"""
            return await invoke(
                lambda owner: workflow.recommend(owner, workflow_id),
                ("vidu:video", "vidu:purchase"),
            )

        @mcp.tool(annotations={**write, "idempotentHint": True})
        async def vidu_purchase_for_video(workflow_id: str, quote_id: str, revision: int) -> dict:
            """用户选定套餐后为视频请求准备官方支付页，使用最新 revision，不代替用户付款。

            同一视频请求、报价重复调用复用原购买。付款后查询 vidu_get_workflow，不再次下单。
            """
            return await invoke(
                lambda owner: workflow.buy(owner, workflow_id, quote_id, revision),
                ("vidu:video", "vidu:purchase"),
            )
