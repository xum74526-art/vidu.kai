"""Parse visible monthly plan cards. Never treat annual per-month prices as totals."""

import re
from decimal import Decimal

from vidu_purchase.models import DomainError, Plan

PLAN_NAMES = {"标准版": "standard", "专业版": "pro", "旗舰版": "ultimate"}


def minor_units(value: str) -> int:
    return int(Decimal(value.replace(",", "")) * 100)


def parse_monthly_card(text: str, *, source: str = "vidu_website") -> Plan:
    name = next((item for item in PLAN_NAMES if item in text), None)
    if not name:
        raise DomainError("unsupported_plan", "未识别官网套餐名称，请重新读取套餐。")
    # Restrict price extraction to the header, excluding price-per-credit marketing text.
    header = re.split(r"立即订阅|当前套餐|升级套餐", text, maxsplit=1)[0]
    if "按年支付" in header or re.search(r"/\s*年", header):
        raise DomainError("wrong_billing_cycle", "当前显示年付套餐，请切换月付后重新获取报价。")
    prices = re.findall(r"\$\s*([\d,]+(?:\.\d{1,2})?)", header)
    credits = re.search(r"每月\s*([\d,]+)\s*积分", text)
    if not prices or not credits:
        raise DomainError("catalog_changed", "官网套餐卡片结构有变化，无法可靠读取价格。")
    renewal = re.search(r"下个月续费金额\s*[：:]\s*\$\s*([\d,]+(?:\.\d{1,2})?)", text)
    credit_count = int(credits.group(1).replace(",", ""))
    return Plan(
        plan_id=f"{PLAN_NAMES[name]}-monthly-{credit_count}",
        name=name,
        amount_minor=minor_units(prices[0]),
        renewal_amount_minor=minor_units(renewal.group(1)) if renewal else None,
        credits=credit_count,
        source=source,
        source_text=text,
    )


def same_price(left: Plan, right: Plan) -> bool:
    fields = (
        "plan_id",
        "amount_minor",
        "renewal_amount_minor",
        "currency",
        "billing_cycle",
        "credits",
        "recurring",
        "source",
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields)
