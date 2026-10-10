import time
from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

from vidu_purchase.models import (
    Checkout,
    DomainError,
    Plan,
    Purchase,
    PurchaseState,
    Quote,
    Receipt,
)
from vidu_purchase.store import Store

SUCCESS_MESSAGE = "购买／充值成功，可以继续使用视频插件。"
CONTINUATION_PROMPT = "是否继续生成视频？"


class Provider(Protocol):
    mode: str

    async def list_plans(self, owner: str) -> list[Plan]: ...
    async def prepare_checkout(self, purchase: Purchase) -> Checkout: ...
    async def check_receipt(self, purchase: Purchase) -> Receipt | Checkout | None: ...


class PurchaseService:
    def __init__(self, store: Store, provider: Provider, *, clock: Callable[[], float] = time.time):
        self.store = store
        self.provider = provider
        self.clock = clock

    async def commerce_session(self, owner: str) -> dict:
        operation = getattr(self.provider, "session_status", None)
        if operation:
            return await operation(owner)
        return {
            "mode": self.provider.mode,
            "status": "not_available",
            "handoff_url": None,
            "message": "当前计费模式不使用官网浏览器会话。",
        }

    async def website_orders(self, owner: str) -> dict:
        operation = getattr(self.provider, "order_observation", None)
        if operation is None:
            raise DomainError("browser_not_configured", "当前未配置官网浏览器订单查询。")
        return await operation(owner)

    async def list_plans(self, owner: str) -> dict:
        now = self.clock()
        plans = await self.provider.list_plans(owner)
        quotes = []
        for plan in plans:
            quote = Quote(
                quote_id=uuid4().hex, owner=owner, plan=plan, created_at=now, expires_at=now + 180
            )
            self.store.save_quote(quote)
            quotes.append(
                {
                    "quote_id": quote.quote_id,
                    "expires_at": quote.expires_at,
                    "plan": plan.model_dump(exclude={"source_text", "account_ref"}),
                    "checkout_available": getattr(self.provider, "can_checkout", lambda _p: True)(
                        plan
                    ),
                }
            )
        return {
            "mode": self.provider.mode,
            "quotes": quotes,
            "billing_cycle": "monthly",
            "message": (
                "请同时展示首期价格、续费价格、积分及自动续费信息。"
                "金额单位为分，官网结账页的税费及最终应付金额由用户确认。"
                "当前首版支持月付套餐及官网当前选中的旗舰版档位。"
            ),
        }

    def view(self, purchase: Purchase) -> dict:
        # Owners, raw browser content and service credentials never leave the service.
        result = purchase.model_dump(exclude={"owner", "quote", "idempotency_key"})
        result["plan"] = purchase.quote.plan.model_dump(exclude={"source_text", "account_ref"})
        result.update(
            {
                "mode": self.provider.mode,
                "auto_generate": False,
                "requires_generation_confirmation": True,
                "continuation_prompt": (
                    CONTINUATION_PROMPT if purchase.state == PurchaseState.READY else None
                ),
                # A new affirmative reply to the continuation prompt counts as an instruction.
                "requires_new_video_instruction": True,
                "payment_confirmation_by": "user",
            }
        )
        result["payment_action"] = (
            {
                "type": "open_official_checkout",
                "url": purchase.handoff_url,
                "purchase_id": purchase.purchase_id,
                "confirmation_by": "user",
                "embedded": False,
                "after_return": "check_purchase",
            }
            if purchase.state == PurchaseState.AWAITING_PAYMENT and purchase.handoff_url
            else None
        )
        if self.provider.mode == "demo":
            result["message"] = "【演示，不产生真实订单或扣款】" + result["message"]
        return result

    async def prepare_purchase(self, owner: str, quote_id: str, idempotency_key: str) -> dict:
        if not 8 <= len(idempotency_key) <= 128:
            raise DomainError(
                "invalid_idempotency_key", "同一次购买需使用固定的 8–128 位请求标识。"
            )
        existing = self.store.by_key(owner, idempotency_key)
        if existing:
            if existing.quote.quote_id != quote_id:
                raise DomainError("idempotency_conflict", "这个请求标识已用于另一份报价。")
            # A retry never clicks Subscribe again, even after a timeout or a restart.
            return self.view(existing)
        quote = self.store.quote(owner, quote_id)
        if quote.expires_at <= self.clock():
            raise DomainError("quote_expired", "报价已过期，请重新获取官网套餐和价格。")
        now = self.clock()
        purchase = Purchase(
            purchase_id=uuid4().hex,
            owner=owner,
            quote=quote,
            idempotency_key=idempotency_key,
            state=PurchaseState.PREPARING,
            created_at=now,
            updated_at=now,
            message="正在准备官方支付确认页面。",
        )
        purchase, created = self.store.insert_purchase(purchase)
        if not created:
            if purchase.quote.quote_id != quote_id:
                raise DomainError("idempotency_conflict", "这个请求标识已用于另一份报价。")
            return self.view(purchase)
        try:
            checkout = await self.provider.prepare_checkout(purchase)
            allowed = {
                PurchaseState.LOGIN_REQUIRED,
                PurchaseState.AWAITING_PAYMENT,
                PurchaseState.VERIFICATION_REQUIRED,
                PurchaseState.PRICE_CHANGED,
                PurchaseState.BLOCKED,
            }
            if checkout.state not in allowed:
                raise DomainError("invalid_checkout_state", "创建结账不能作为已付款凭据。")
            purchase = purchase.model_copy(
                update={
                    "state": checkout.state,
                    "message": checkout.message,
                    "handoff_url": checkout.handoff_url,
                    "provider_reference": checkout.provider_reference,
                    "updated_at": self.clock(),
                }
            )
        except DomainError as error:
            purchase = purchase.model_copy(
                update={
                    "state": PurchaseState.BLOCKED,
                    "message": f"{error.code}: {error}",
                    "updated_at": self.clock(),
                }
            )
        except Exception:
            # An uncertain network outcome must not create another payment attempt.
            purchase = purchase.model_copy(
                update={
                    "state": PurchaseState.VERIFICATION_REQUIRED,
                    "message": "官网响应不确定，请检查同一浏览器中的订单；不要重复创建购买。",
                    "updated_at": self.clock(),
                }
            )
        return self.view(self.store.update(purchase))

    async def check_purchase(self, owner: str, purchase_id: str) -> dict:
        purchase = self.store.purchase(owner, purchase_id)
        if purchase.state == PurchaseState.READY:
            return self.view(purchase)
        receipt = await self.provider.check_receipt(purchase)
        if receipt is None:
            return self.view(purchase)
        if isinstance(receipt, Checkout):
            if receipt.state in {PurchaseState.READY, PurchaseState.PAID_PENDING_ENTITLEMENT}:
                raise DomainError("receipt_required", "仅有结账页面不能确认付款或到账。")
            purchase = purchase.model_copy(
                update={
                    "state": receipt.state,
                    "message": receipt.message,
                    "handoff_url": receipt.handoff_url,
                    "provider_reference": receipt.provider_reference or purchase.provider_reference,
                    "updated_at": self.clock(),
                }
            )
            return self.view(self.store.update(purchase))
        expected = {
            "owner": owner,
            "purchase_id": purchase_id,
            "plan_id": purchase.quote.plan.plan_id,
            "currency": purchase.quote.plan.currency,
            "amount_minor": purchase.quote.plan.amount_minor,
        }
        if any(getattr(receipt, field) != value for field, value in expected.items()):
            raise DomainError(
                "receipt_mismatch", "订单归属、套餐或金额与购买记录不一致，不能确认到账。"
            )
        if not receipt.payment_succeeded:
            return self.view(purchase)
        state = (
            PurchaseState.READY
            if receipt.entitlement_active
            else PurchaseState.PAID_PENDING_ENTITLEMENT
        )
        purchase = purchase.model_copy(
            update={
                "state": state,
                "receipt_reference": receipt.order_reference,
                "message": (
                    SUCCESS_MESSAGE
                    if state == PurchaseState.READY
                    else "已确认付款，官网权益尚未确认到账，请稍后查询。"
                ),
                "updated_at": self.clock(),
            }
        )
        # Only persist fulfillment; there is intentionally no generation callback or task queue.
        return self.view(self.store.update(purchase, receipt=receipt.order_reference))
