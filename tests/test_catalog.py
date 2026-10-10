import json

import pytest

from vidu_purchase.browser import read_bindings
from vidu_purchase.catalog import parse_monthly_card
from vidu_purchase.models import DomainError

# Anonymized visible card text observed on 2026-10-09, not a production price table.
PROMO_CARD = """标准版
7折
满足日常轻量创作需求
$
7
/
月
$10
下个月续费金额：$10
包月可随时取消，虚拟产品不支持退款
立即订阅
每月800积分
100积分=$0.88
"""


def test_intro_and_renewal_prices_are_kept_separate():
    plan = parse_monthly_card(PROMO_CARD)
    assert plan.amount_minor == 700
    assert plan.renewal_amount_minor == 1000
    assert plan.credits == 800
    assert plan.recurring is True
    assert plan.plan_id == "standard-monthly-800"


def test_unknown_renewal_is_not_invented():
    text = "专业版 $43/月 立即订阅 每月3500积分 100积分=$1.23"
    plan = parse_monthly_card(text)
    assert plan.amount_minor == 4300
    assert plan.renewal_amount_minor is None


def test_selected_ultimate_credit_variant_is_part_of_id():
    text = "旗舰版 $138/月 下个月续费金额：$230 立即订阅 每月20,000积分"
    plan = parse_monthly_card(text)
    assert plan.plan_id == "ultimate-monthly-20000"
    assert plan.amount_minor == 13800


def test_missing_price_is_not_treated_as_free():
    with pytest.raises(DomainError, match="无法可靠读取价格"):
        parse_monthly_card("标准版 实付金额 -- 立即订阅 每月800积分")


def test_annual_equivalent_monthly_price_cannot_be_used_as_a_monthly_charge():
    with pytest.raises(DomainError, match="年付套餐"):
        parse_monthly_card("标准版 $5.92/月 按年支付 $71 立即订阅 每月800积分")


@pytest.mark.parametrize("endpoint", ["http://public.example:9222", "file:///private"])
def test_browser_debug_port_cannot_be_public_plaintext(tmp_path, endpoint):
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps({"alice": {"cdp_url": endpoint}}))
    with pytest.raises(ValueError, match="TLS"):
        read_bindings(path)


def test_users_cannot_share_the_same_browser_endpoint(tmp_path):
    path = tmp_path / "bindings.json"
    path.write_text(
        json.dumps(
            {
                "alice": {"cdp_url": "http://127.0.0.1:9222"},
                "bob": {"cdp_url": "http://127.0.0.1:9222"},
            }
        )
    )
    with pytest.raises(ValueError, match="distinct"):
        read_bindings(path)
