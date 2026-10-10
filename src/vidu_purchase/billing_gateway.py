"""Client for OUR self-hosted commerce gateway contract, NOT a public Vidu billing API.

A deployment must implement the upstream gateway using its authorized Vidu billing
integration. No Vidu billing endpoint is guessed here, and no default catalog exists.
"""

import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import Field, model_validator

from vidu_purchase.checkout import stripe_reference
from vidu_purchase.models import (
    Checkout,
    DomainError,
    Plan,
    Purchase,
    PurchaseState,
    Receipt,
    Record,
)


class BillingUnavailable:
    mode = "billing_not_configured"

    async def list_plans(self, _owner: str):
        raise DomainError(
            "billing_not_configured", "尚未接入真实 Vidu 官网计费服务，不能提供套餐或创建订单。"
        )

    async def prepare_checkout(self, _purchase: Purchase):
        raise DomainError("billing_not_configured", "尚未接入真实 Vidu 下单服务。")

    async def check_receipt(self, _purchase: Purchase):
        raise DomainError("billing_not_configured", "尚未接入真实 Vidu 订单与权益核验。")


class GatewaySettings(Record):
    base_url: str
    token_env: str = Field(pattern=r"^VIDU_COMMERCE_[A-Z0-9_]+_TOKEN$")
    account_refs: dict[str, str]
    checkout_hosts: list[str] = Field(default_factory=lambda: ["checkout.stripe.com"])

    @model_validator(mode="after")
    def validate_settings(self):
        url = urlsplit(self.base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.port not in {None, 443}
        ):
            raise ValueError(
                "The authorized commerce gateway must use HTTPS without URL credentials"
            )
        if not self.account_refs or any(
            not key or not value or len(value) > 128 for key, value in self.account_refs.items()
        ):
            raise ValueError("Each verified user needs an explicit commerce account reference")
        if len(set(self.account_refs.values())) != len(self.account_refs):
            raise ValueError("Different users must not share a commerce account reference")
        if not self.checkout_hosts or any(
            not host or host != host.lower() or any(c in host for c in "/*:@?#\\")
            for host in self.checkout_hosts
        ):
            raise ValueError("Use exact checkout hostnames, without wildcards")
        return self


class GatewayPlan(Plan):
    # Money and renewal terms must be supplied, never inferred from model defaults.
    currency: Literal["USD"]
    billing_cycle: Literal["monthly"]
    recurring: bool
    renewal_amount_minor: int | None = Field(gt=0)

    @model_validator(mode="after")
    def validate_terms(self):
        if self.recurring and self.renewal_amount_minor is None:
            raise ValueError("Recurring plans require an explicit renewal price")
        return self


class CatalogResponse(Record):
    account_ref: str
    plans: list[GatewayPlan] = Field(min_length=1, max_length=32)


class CheckoutResponse(Record):
    account_ref: str
    request_id: str
    plan_id: str
    amount_minor: int = Field(gt=0, strict=True)
    currency: Literal["USD"]
    billing_cycle: Literal["monthly"]
    recurring: bool = Field(strict=True)
    renewal_amount_minor: int | None = Field(gt=0, strict=True)
    checkout_url: str
    checkout_reference: str = Field(min_length=1, max_length=256)


class OrderResponse(Record):
    account_ref: str
    request_id: str
    plan_id: str
    amount_minor: int = Field(gt=0, strict=True)
    currency: Literal["USD"]
    order_reference: str = Field(min_length=1, max_length=256)
    payment_succeeded: bool = Field(strict=True)
    entitlement_active: bool = Field(strict=True)


class HttpCommerceGateway:
    mode = "http_commerce_gateway"

    def __init__(self, settings: GatewaySettings, *, transport=None):
        self.settings = settings
        self.transport = transport

    def account_ref(self, owner: str) -> str:
        reference = self.settings.account_refs.get(owner)
        if not reference:
            raise DomainError("billing_account_not_connected", "当前用户尚未绑定官网计费账号。")
        return reference

    def purchase_ref(self, purchase: Purchase) -> str:
        reference = self.account_ref(purchase.owner)
        if purchase.quote.plan.account_ref != reference:
            raise DomainError(
                "billing_account_changed", "计费账号已变化，不能沿用旧账号报价或订单。"
            )
        return reference

    async def request(self, method: str, path: str, *, allow_missing=False, **kwargs):
        token = os.getenv(self.settings.token_env, "")
        if not token:
            raise DomainError("billing_not_configured", "计费网关凭据未配置。")
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=20, trust_env=False, follow_redirects=False
            ) as client:
                async with client.stream(
                    method,
                    self.settings.base_url.rstrip("/") + path,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    **kwargs,
                ) as response:
                    if allow_missing and response.status_code == 404:
                        return None
                    if not 200 <= response.status_code < 300:
                        raise DomainError("billing_upstream_error", "计费服务未确认本次请求。")
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 262144:
                            raise DomainError("billing_invalid_response", "计费响应超出允许大小。")
                        chunks.append(chunk)
            raw = b"".join(chunks)
            if not isinstance(json.loads(raw), dict):
                raise ValueError()
            return raw
        except httpx.HTTPError:
            raise DomainError(
                "billing_response_uncertain", "计费响应不确定，请查询原订单。"
            ) from None
        except (ValueError, UnicodeError):
            raise DomainError("billing_invalid_response", "无法可靠识别计费响应。") from None

    async def list_plans(self, owner: str) -> list[Plan]:
        reference = self.account_ref(owner)
        raw = await self.request("GET", "/v1/catalog", params={"account_ref": reference})
        try:
            catalog = CatalogResponse.model_validate_json(raw, strict=True)
        except ValueError:
            raise DomainError("billing_invalid_response", "套餐字段或金额无法可靠识别。") from None
        if catalog.account_ref != reference or any(
            p.source != "vidu_website" for p in catalog.plans
        ):
            raise DomainError("billing_account_mismatch", "套餐来源或账号不匹配。")
        if len({p.plan_id for p in catalog.plans}) != len(catalog.plans):
            raise DomainError("billing_invalid_response", "计费服务返回重复套餐标识。")
        return [plan.model_copy(update={"account_ref": reference}) for plan in catalog.plans]

    async def prepare_checkout(self, purchase: Purchase) -> Checkout:
        reference = self.purchase_ref(purchase)
        plan = purchase.quote.plan
        try:
            raw = await self.request(
                "POST",
                "/v1/checkouts",
                json={
                    "account_ref": reference,
                    "request_id": purchase.purchase_id,
                    "plan_id": plan.plan_id,
                    "amount_minor": plan.amount_minor,
                    "currency": plan.currency,
                    "billing_cycle": plan.billing_cycle,
                    "renewal_amount_minor": plan.renewal_amount_minor,
                    "recurring": plan.recurring,
                },
            )
            result = CheckoutResponse.model_validate_json(raw, strict=True)
            if (
                result.account_ref,
                result.request_id,
                result.plan_id,
                result.amount_minor,
                result.currency,
            ) != (reference, purchase.purchase_id, plan.plan_id, plan.amount_minor, plan.currency):
                raise ValueError()
            if (result.billing_cycle, result.recurring, result.renewal_amount_minor) != (
                plan.billing_cycle,
                plan.recurring,
                plan.renewal_amount_minor,
            ):
                raise ValueError()
            url = urlsplit(result.checkout_url)
            if (
                url.scheme != "https"
                or url.hostname not in self.settings.checkout_hosts
                or url.username
                or url.password
                or url.port not in {None, 443}
            ):
                raise ValueError()
            if url.hostname == "checkout.stripe.com":
                if stripe_reference(result.checkout_url) != result.checkout_reference:
                    raise ValueError()  # Reject test sessions and mismatched references.
        except (DomainError, ValueError):
            # The POST might have reached the merchant. Never auto-create another checkout.
            return Checkout(
                state=PurchaseState.VERIFICATION_REQUIRED,
                message="下单结果尚未可靠确认，请查询原购买；不会自动重复下单。",
            )
        return Checkout(
            state=PurchaseState.AWAITING_PAYMENT,
            handoff_url=result.checkout_url,
            provider_reference=result.checkout_reference,
            message="请用户在官方支付页核对金额及续费条件并确认付款，之后查询本次购买。",
        )

    async def check_receipt(self, purchase: Purchase) -> Receipt | None:
        reference = self.purchase_ref(purchase)
        raw = await self.request(
            "GET",
            f"/v1/orders/by-request/{quote(purchase.purchase_id, safe='')}",
            params={"account_ref": reference},
            allow_missing=True,
        )
        if raw is None:
            return None
        try:
            order = OrderResponse.model_validate_json(raw, strict=True)
        except ValueError:
            raise DomainError("billing_invalid_response", "官方订单凭据无法可靠识别。") from None
        if order.account_ref != reference or order.request_id != purchase.purchase_id:
            raise DomainError("receipt_mismatch", "订单与当前计费账号或请求不匹配。")
        return Receipt(
            owner=purchase.owner,
            purchase_id=purchase.purchase_id,
            plan_id=order.plan_id,
            order_reference=order.order_reference,
            amount_minor=order.amount_minor,
            currency=order.currency,
            payment_succeeded=order.payment_succeeded,
            entitlement_active=order.entitlement_active,
        )


def load_billing(path: Path | None):
    if path is None:
        return BillingUnavailable()
    settings = GatewaySettings.model_validate_json(path.read_bytes())
    if not os.getenv(settings.token_env):
        raise ValueError("Configured billing gateway credential is missing")
    return HttpCommerceGateway(settings)
