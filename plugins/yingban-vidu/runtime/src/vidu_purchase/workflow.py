"""Account → estimate → purchase → confirmation → video, with no submission on refresh."""

import asyncio
import time
from collections.abc import Callable
from uuid import uuid4

from vidu_purchase.models import DomainError, PurchaseState
from vidu_purchase.service import PurchaseService
from vidu_purchase.video_models import VideoConfig, VideoWorkflow, WorkflowState
from vidu_purchase.video_provider import VideoProvider
from vidu_purchase.workflow_store import WorkflowStore

SUBMITTED = {
    WorkflowState.SUBMITTING,
    WorkflowState.SUBMISSION_UNKNOWN,
    WorkflowState.GENERATING,
    WorkflowState.COMPLETED,
    WorkflowState.FAILED,
}
TERMINAL = {WorkflowState.COMPLETED, WorkflowState.FAILED, WorkflowState.CANCELLED}


class WorkflowService:
    def __init__(
        self,
        purchases: PurchaseService,
        video: VideoProvider,
        *,
        clock: Callable[[], float] = time.time,
        commerce_enabled: bool = True,
        always_confirm_generation: bool = False,
        binding_revision: Callable[[str], str | None] | None = None,
    ):
        self.purchases = purchases
        self.video = video
        self.store = WorkflowStore(purchases.store)
        self.clock = clock
        self.commerce_enabled = commerce_enabled
        self.always_confirm_generation = always_confirm_generation
        self.binding_revision = binding_revision or (lambda _owner: None)
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, workflow_id: str) -> asyncio.Lock:
        return self.locks.setdefault(workflow_id, asyncio.Lock())

    def load(self, owner: str, workflow_id: str) -> VideoWorkflow:
        workflow = self.store.get(owner, workflow_id)
        if workflow.mode != self.video.mode:
            raise DomainError("workflow_mode_mismatch", "演示和真实请求不能混用，请使用对应部署。")
        if workflow.binding_revision != self.binding_revision(owner):
            raise DomainError("account_binding_changed", "账号连接已更换，请创建新的视频请求。")
        return workflow

    def save(self, workflow: VideoWorkflow, **changes) -> VideoWorkflow:
        return self.store.save(workflow, updated_at=self.clock(), **changes)

    def view(self, workflow: VideoWorkflow) -> dict:
        result = workflow.model_dump(
            exclude={"owner", "idempotency_key", "created_at", "updated_at", "binding_revision"}
        )
        ready = workflow.state == WorkflowState.AWAITING_CONFIRMATION
        result.update(
            {
                "auto_generate": False,
                "continuation_prompt": (
                    "是否继续生成刚才的视频？"
                    if ready and workflow.requires_continuation_confirmation
                    else None
                ),
                "next_action": {
                    WorkflowState.ACCOUNT_REQUIRED: "login_or_register",
                    WorkflowState.NEEDS_PURCHASE: "compare_plans",
                    WorkflowState.PREPARING_PURCHASE: "check_existing_purchase",
                    WorkflowState.AWAITING_PAYMENT: "user_confirm_payment",
                    WorkflowState.VERIFYING_PAYMENT: "check_existing_purchase",
                    WorkflowState.AWAITING_ENTITLEMENT: "wait_for_entitlement",
                    WorkflowState.AWAITING_CONFIRMATION: (
                        "ask_generation_confirmation"
                        if workflow.requires_continuation_confirmation
                        else "submit_authorized_generation"
                    ),
                    WorkflowState.SUBMITTING: "do_not_resubmit",
                    WorkflowState.SUBMISSION_UNKNOWN: "reconcile_do_not_resubmit",
                    WorkflowState.GENERATING: "check_workflow",
                }.get(workflow.state, "none"),
            }
        )
        if workflow.purchase_id and self.commerce_enabled:
            purchase = self.purchases.store.purchase(workflow.owner, workflow.purchase_id)
            result["purchase"] = {
                "purchase_id": purchase.purchase_id,
                "state": purchase.state,
                "handoff_url": purchase.handoff_url,
                "message": purchase.message,
                "plan": purchase.quote.plan.model_dump(exclude={"source_text", "account_ref"}),
            }
            # A host can render this action alongside the conversation. An official
            # checkout URL does not imply that an embedded payment SDK is available.
            result["payment_action"] = (
                {
                    "type": "open_official_checkout",
                    "url": purchase.handoff_url,
                    "purchase_id": purchase.purchase_id,
                    "confirmation_by": "user",
                    "embedded": False,
                    "after_return": "check_workflow",
                }
                if purchase.handoff_url and purchase.state == PurchaseState.AWAITING_PAYMENT
                else None
            )
        if self.video.mode == "demo":
            result["message"] = "【演示，不扣款、不生成真实视频】" + result["message"]
        return result

    async def account_status(self, owner: str) -> dict:
        account = await self.video.account(owner)
        return {
            "mode": self.video.mode,
            **account.model_dump(),
            "automatic_registration_available": False,
        }

    async def start(self, owner: str, config: VideoConfig, idempotency_key: str) -> dict:
        config = VideoConfig.model_validate(config.model_dump())
        if not 8 <= len(idempotency_key) <= 128:
            raise DomainError(
                "invalid_idempotency_key", "同一次视频请求需使用固定的 8–128 位标识。"
            )
        now = self.clock()
        workflow, created = self.store.insert(
            VideoWorkflow(
                workflow_id=uuid4().hex,
                owner=owner,
                idempotency_key=idempotency_key,
                mode=self.video.mode,
                binding_revision=self.binding_revision(owner),
                config=config,
                requires_continuation_confirmation=self.always_confirm_generation,
                created_at=now,
                updated_at=now,
            )
        )
        workflow = self.load(owner, workflow.workflow_id)
        if workflow.config != config:
            raise DomainError(
                "idempotency_conflict", "这个请求标识已用于其他配置，请使用更新视频请求工具。"
            )
        if not created:
            return self.view(workflow)
        return await self.get(owner, workflow.workflow_id)

    async def get(self, owner: str, workflow_id: str) -> dict:
        async with self.lock(workflow_id):
            workflow = await self.refresh(self.load(owner, workflow_id))
            return self.view(workflow)

    async def refresh(self, workflow: VideoWorkflow) -> VideoWorkflow:
        if workflow.state in TERMINAL:
            return workflow
        if workflow.state in {WorkflowState.SUBMITTING, WorkflowState.SUBMISSION_UNKNOWN}:
            return workflow  # A process may have died after POST; never issue that POST again.
        if workflow.task:
            task = await self.video.task(workflow.owner, workflow.task.task_id)
            if task.task_id != workflow.task.task_id:
                raise DomainError("task_mismatch", "返回任务与当前请求不匹配。")
            state = {"success": WorkflowState.COMPLETED, "failed": WorkflowState.FAILED}.get(
                task.state, WorkflowState.GENERATING
            )
            message = {
                WorkflowState.COMPLETED: "视频任务已完成。",
                WorkflowState.FAILED: "视频任务失败；如需再次提交，请明确发起新的生成请求。",
            }.get(state, "视频生成中，请稍后查询。")
            return self.save(workflow, task=task, state=state, message=message)
        if workflow.purchase_quote_id and not workflow.purchase_id:
            existing = self.purchases.store.by_key(workflow.owner, self.purchase_key(workflow))
            if not existing:
                return workflow  # Only an explicit purchase call may start the checkout.
            workflow = self.save(workflow, purchase_id=existing.purchase_id)
        if workflow.purchase_id:
            if not self.commerce_enabled:
                return self.save(
                    workflow,
                    state=WorkflowState.BLOCKED,
                    confirmation_id=None,
                    message="当前部署不支持此购买流程。",
                )
            progress = await self.purchases.check_purchase(workflow.owner, workflow.purchase_id)
            if progress["state"] != PurchaseState.READY:
                state = {
                    PurchaseState.PREPARING: WorkflowState.PREPARING_PURCHASE,
                    PurchaseState.AWAITING_PAYMENT: WorkflowState.AWAITING_PAYMENT,
                    PurchaseState.PAID_PENDING_ENTITLEMENT: WorkflowState.AWAITING_ENTITLEMENT,
                }.get(progress["state"], WorkflowState.VERIFYING_PAYMENT)
                return self.save(
                    workflow, state=state, confirmation_id=None, message=progress["message"]
                )
        account = await self.video.account(workflow.owner)
        if account.status != "connected":
            return self.save(
                workflow,
                account=account,
                state=WorkflowState.ACCOUNT_REQUIRED,
                estimate=None,
                estimate_expires_at=None,
                confirmation_id=None,
                message=account.message,
                requires_continuation_confirmation=True,
            )
        estimate = await self.video.estimate(workflow.owner, workflow.config)
        if not estimate.can_submit:
            needs_purchase = (
                estimate.reason == "insufficient_credits"
                and self.commerce_enabled
                and account.region == "global"
            )
            message = (
                "当前积分不足，可比较套餐；本次视频需求已保存。"
                if needs_purchase
                else "当前额度或权益不允许提交。请检查额度刷新时间、生成配置或账号权限。"
            )
            return self.save(
                workflow,
                account=account,
                estimate=estimate,
                state=WorkflowState.NEEDS_PURCHASE if needs_purchase else WorkflowState.BLOCKED,
                confirmation_id=None,
                estimate_expires_at=None,
                message=message,
                requires_continuation_confirmation=True,
            )
        if not self.video.generation_enabled:
            return self.save(
                workflow,
                account=account,
                estimate=estimate,
                state=WorkflowState.BLOCKED,
                confirmation_id=None,
                message="额度和费用已查询，部署尚未启用真实视频提交。",
            )
        old = workflow.estimate
        same_terms = bool(
            old
            and old.cost_credits == estimate.cost_credits
            and old.schedule_mode == estimate.schedule_mode
        )
        still_valid = bool(
            workflow.estimate_expires_at and workflow.estimate_expires_at > self.clock()
        )
        reuse = same_terms and still_valid and workflow.confirmation_id is not None
        changed = old is not None and (
            not same_terms
            or (workflow.state == WorkflowState.AWAITING_CONFIRMATION and not still_valid)
        )
        needs_confirmation = workflow.requires_continuation_confirmation or changed
        return self.save(
            workflow,
            account=account,
            estimate=estimate,
            estimate_expires_at=(workflow.estimate_expires_at if reuse else self.clock() + 180),
            confirmation_id=workflow.confirmation_id if reuse else uuid4().hex,
            state=WorkflowState.AWAITING_CONFIRMATION,
            requires_continuation_confirmation=needs_confirmation,
            message=(
                "费用或确认有效期已更新，请按最新配置和费用重新确认生成。"
                if changed
                else "购买成功，权益已到账，可以继续使用视频插件。"
                if workflow.purchase_id
                else "账号和额度已就绪，视频配置与预计积分消耗已确认。"
            ),
        )

    async def update(
        self, owner: str, workflow_id: str, config: VideoConfig, revision: int
    ) -> dict:
        config = VideoConfig.model_validate(config.model_dump())
        async with self.lock(workflow_id):
            workflow = self.load(owner, workflow_id)
            self.require_editable(workflow, revision)
            workflow = self.save(
                workflow,
                config=config,
                estimate=None,
                estimate_expires_at=None,
                confirmation_id=None,
                requires_continuation_confirmation=True,
                state=WorkflowState.CHECKING,
                message="视频需求已更新，正在重新检查费用。",
            )
            return self.view(await self.refresh(workflow))

    @staticmethod
    def require_editable(workflow: VideoWorkflow, revision: int) -> None:
        if workflow.revision != revision:
            raise DomainError("workflow_changed", "请求版本已变化，请读取最新状态。")
        if workflow.state in SUBMITTED or workflow.state == WorkflowState.CANCELLED:
            raise DomainError("workflow_not_editable", "当前请求已提交或结束，不能更改或重新提交。")

    async def cancel(self, owner: str, workflow_id: str, revision: int) -> dict:
        async with self.lock(workflow_id):
            workflow = self.load(owner, workflow_id)
            self.require_editable(workflow, revision)
            workflow = self.save(
                workflow,
                state=WorkflowState.CANCELLED,
                confirmation_id=None,
                message="本次视频请求已结束。此操作不会撤销官网订单或订阅。",
            )
            return self.view(workflow)

    async def recommend(self, owner: str, workflow_id: str) -> dict:
        if not self.commerce_enabled:
            raise DomainError("commerce_disabled", "当前部署不提供购买工具。")
        async with self.lock(workflow_id):
            workflow = await self.refresh(self.load(owner, workflow_id))
            if workflow.state != WorkflowState.NEEDS_PURCHASE:
                raise DomainError(
                    "purchase_not_needed", "当前流程不需要选购套餐，请按照当前状态处理。"
                )
            catalog = await self.purchases.list_plans(owner)
            eligible = [
                q
                for q in catalog["quotes"]
                if q["plan"]["credits"] >= workflow.estimate.cost_credits
                and q.get("checkout_available", True)
            ]
            eligible.sort(key=lambda q: (q["plan"]["amount_minor"], q["plan"]["credits"]))
            return {
                **catalog,
                "workflow_id": workflow_id,
                "revision": workflow.revision,
                "estimated_cost_credits": workflow.estimate.cost_credits,
                "recommended_quote_id": eligible[0]["quote_id"] if eligible else None,
                "recommendation_basis": (
                    "按月积分覆盖本次预计消耗、首期价格较低筛选；到账后仍需重新核验可生成条件。"
                ),
            }

    @staticmethod
    def purchase_key(workflow: VideoWorkflow) -> str:
        return f"video-{workflow.workflow_id}-{workflow.purchase_attempt}"

    async def buy(self, owner: str, workflow_id: str, quote_id: str, revision: int) -> dict:
        if not self.commerce_enabled:
            raise DomainError("commerce_disabled", "当前部署不提供购买工具。")
        async with self.lock(workflow_id):
            workflow = self.load(owner, workflow_id)
            if workflow.state in SUBMITTED or workflow.state == WorkflowState.CANCELLED:
                raise DomainError("workflow_not_editable", "视频请求已提交或结束。")
            if workflow.purchase_quote_id == quote_id:
                if workflow.purchase_id:
                    return self.view(await self.refresh(workflow))
                # Recover the same deterministic purchase after an interrupted response.
            else:
                self.require_editable(workflow, revision)
                if workflow.state != WorkflowState.NEEDS_PURCHASE:
                    raise DomainError("purchase_not_needed", "请先检查当前账号是否需要购买。")
                if workflow.purchase_id:
                    previous = self.purchases.store.purchase(owner, workflow.purchase_id)
                    if previous.state != PurchaseState.READY:
                        raise DomainError(
                            "purchase_in_progress", "已有未完成购买，请先处理原订单。"
                        )
                quote = self.purchases.store.quote(owner, quote_id)
                if quote.expires_at <= self.clock():
                    raise DomainError("quote_expired", "报价已过期，请重新比较套餐。")
                workflow = self.save(
                    workflow,
                    purchase_quote_id=quote_id,
                    purchase_id=None,
                    purchase_attempt=workflow.purchase_attempt + 1,
                    state=WorkflowState.PREPARING_PURCHASE,
                    confirmation_id=None,
                    requires_continuation_confirmation=True,
                    message="正在准备所选套餐的官方支付页。",
                )
            purchase = await self.purchases.prepare_purchase(
                owner, quote_id, self.purchase_key(workflow)
            )
            workflow = self.save(workflow, purchase_id=purchase["purchase_id"])
            return self.view(await self.refresh(workflow))

    async def confirm(self, owner: str, workflow_id: str, confirmation_id: str) -> dict:
        async with self.lock(workflow_id):
            workflow = self.load(owner, workflow_id)
            if workflow.state in SUBMITTED:
                return self.view(workflow)
            if (
                workflow.state != WorkflowState.AWAITING_CONFIRMATION
                or not confirmation_id
                or confirmation_id != workflow.confirmation_id
            ):
                raise DomainError(
                    "confirmation_required", "请先读取当前配置和费用，取得用户明确的生成确认。"
                )
            fresh = await self.refresh(workflow)
            if (
                fresh.state != WorkflowState.AWAITING_CONFIRMATION
                or fresh.confirmation_id != confirmation_id
            ):
                return self.view(fresh)  # Changed cost/quota/expiry requires a new user decision.
            if fresh.binding_revision != self.binding_revision(owner):
                raise DomainError("account_binding_changed", "账号连接已更换，请创建新的视频请求。")
            # Atomic claim is committed BEFORE the billable POST, including across processes.
            claimed = self.save(
                fresh,
                state=WorkflowState.SUBMITTING,
                confirmation_id=None,
                message="视频正在提交，请勿重复请求。",
            )
            try:
                task = await self.video.submit(owner, claimed.config, claimed.estimate)
            except Exception:
                return self.view(
                    self.save(
                        claimed,
                        state=WorkflowState.SUBMISSION_UNKNOWN,
                        message="提交结果尚不确定，请核对官网任务。不会自动重试或再次消耗积分。",
                    )
                )
            state = {"success": WorkflowState.COMPLETED, "failed": WorkflowState.FAILED}.get(
                task.state, WorkflowState.GENERATING
            )
            return self.view(
                self.save(
                    claimed, task=task, state=state, message="视频任务已提交，可查询生成结果。"
                )
            )
