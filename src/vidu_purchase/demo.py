"""Explicit, local-only test provider. It does not use Vidu or any payment service."""

from vidu_purchase.models import Checkout, Plan, Purchase, PurchaseState, Receipt


class DemoProvider:
    mode = "demo"

    def __init__(self):
        self.receipts: dict[str, Receipt] = {}
        self.checkout_count = 0

    async def list_plans(self, owner: str) -> list[Plan]:
        return [
            Plan(
                plan_id="demo-monthly",
                name="演示月付套餐（非官网报价）",
                amount_minor=100,
                renewal_amount_minor=100,
                credits=100,
                source="demo",
                source_text="测试数据",
            )
        ]

    async def prepare_checkout(self, purchase: Purchase) -> Checkout:
        self.checkout_count += 1
        return Checkout(
            state=PurchaseState.AWAITING_PAYMENT,
            message="等待测试程序模拟用户付款确认。这个模式没有真实支付链接。",
            provider_reference=f"demo-{purchase.purchase_id}",
        )

    async def check_receipt(self, purchase: Purchase) -> Receipt | None:
        return self.receipts.get(purchase.purchase_id)

    def confirm_for_test(self, purchase: Purchase, *, entitlement_active: bool = True) -> None:
        """Test harness only. Never registered as an MCP tool or public HTTP route."""
        self.receipts[purchase.purchase_id] = Receipt(
            order_reference=f"demo-{purchase.purchase_id}",
            purchase_id=purchase.purchase_id,
            owner=purchase.owner,
            plan_id=purchase.quote.plan.plan_id,
            amount_minor=purchase.quote.plan.amount_minor,
            currency=purchase.quote.plan.currency,
            payment_succeeded=True,
            entitlement_active=entitlement_active,
        )
