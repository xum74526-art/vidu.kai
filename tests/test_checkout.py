import pytest

from vidu_purchase.catalog import parse_monthly_card
from vidu_purchase.checkout import stripe_reference, validate_stripe_text
from vidu_purchase.models import DomainError


@pytest.fixture
def plan():
    return parse_monthly_card("标准版 $7/月 下个月续费金额：$10 立即订阅 每月800积分")


def test_discounted_total_is_not_confused_with_recurring_line_price(plan):
    validate_stripe_text("订阅 Vidu Standard\nUS$7.00\n然后从下个月开始每月\nUS$10.00", plan)


def test_stale_full_price_session_is_rejected(plan):
    with pytest.raises(DomainError, match="应付金额与本次报价不同"):
        validate_stripe_text("订阅 Vidu Standard\nUS$10.00\n每个月", plan)


def test_changed_renewal_is_rejected(plan):
    with pytest.raises(DomainError, match="续费金额"):
        validate_stripe_text("订阅 Vidu Standard\nUS$7.00\n然后从下个月开始每月\nUS$12.00", plan)


def test_loading_summary_does_not_become_payable(plan):
    with pytest.raises(DomainError, match="尚未显示"):
        validate_stripe_text("订阅 Vidu Standard\nLoading", plan)


def test_missing_recurring_terms_are_not_inferred_from_amount_alone(plan):
    same_price_plan = plan.model_copy(update={"renewal_amount_minor": 700})
    with pytest.raises(DomainError, match="按月续费"):
        validate_stripe_text("订阅 Vidu Standard\nUS$7.00", same_price_plan)


@pytest.mark.parametrize(
    "url",
    [
        "https://checkout.stripe.com.evil.example/f/pay/cs_live_test",
        "http://checkout.stripe.com/f/pay/cs_live_test",
        "https://checkout.stripe.com@evil.example/f/pay/cs_live_test",
        "https://checkout.stripe.com:444/f/pay/cs_live_test",
    ],
)
def test_checkout_origin_is_exactly_validated(url):
    with pytest.raises(DomainError):
        stripe_reference(url)


def test_only_current_live_checkout_reference_is_accepted():
    assert stripe_reference("https://checkout.stripe.com/f/pay/cs_live_example#display") == (
        "cs_live_example"
    )
    with pytest.raises(DomainError):
        stripe_reference("https://checkout.stripe.com/anything")
