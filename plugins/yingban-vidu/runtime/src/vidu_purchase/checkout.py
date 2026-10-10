"""Read-only validation of the observed hosted Stripe checkout summary."""

import re
from urllib.parse import urlsplit

from vidu_purchase.catalog import minor_units
from vidu_purchase.models import DomainError, Plan

# Product headings observed on real, unpaid official checkouts on 2026-10-09.
CHECKOUT_PRODUCTS = {"标准版": "Vidu Standard", "专业版": "Vidu Premium"}


def stripe_reference(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "checkout.stripe.com"
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise DomainError(
            "unexpected_checkout_origin", "支付页面不属于已验证的官方 Stripe 结账域名。"
        )
    match = re.fullmatch(r"/(?:f|c)/pay/(cs_live_[A-Za-z0-9]+)", parsed.path)
    if not match:
        raise DomainError("unknown_checkout_url", "无法识别官方支付会话地址。")
    return match.group(1)


def validate_stripe_text(text: str, plan: Plan) -> None:
    product = CHECKOUT_PRODUCTS.get(plan.name)
    if not product or not re.search(r"订阅\s+" + re.escape(product) + r"\b", text):
        raise DomainError(
            "unverified_checkout_product", "支付页商品不匹配或此套餐尚未完成支付页验证。"
        )
    # Observed header: US$7.00, then next-month US$10.00. The first amount is due now.
    amounts = re.findall(r"US\$\s*([\d,]+(?:\.\d{1,2})?)", text)
    if not amounts:
        raise DomainError("checkout_amount_missing", "官方支付页尚未显示应付金额。")
    if minor_units(amounts[0]) != plan.amount_minor:
        raise DomainError(
            "checkout_amount_changed", "官方支付页应付金额与本次报价不同，禁止继续付款。"
        )
    renewal = re.search(r"然后从下个月开始每月\s*US\$\s*([\d,]+(?:\.\d{1,2})?)", text)
    if plan.renewal_amount_minor is not None:
        if not renewal and not re.search(r"每个月|每月", text):
            raise DomainError("renewal_amount_missing", "支付页未明确显示按月续费信息。")
        actual_renewal = minor_units(renewal.group(1)) if renewal else minor_units(amounts[0])
        if actual_renewal != plan.renewal_amount_minor:
            raise DomainError(
                "renewal_amount_changed", "官方支付页续费金额与报价不同，请重新确认。"
            )
