from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DomainError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Plan(Record):
    plan_id: str
    name: str
    currency: Literal["USD"] = "USD"
    billing_cycle: Literal["monthly"] = "monthly"
    amount_minor: int = Field(gt=0)
    renewal_amount_minor: int | None = Field(default=None, gt=0)
    credits: int = Field(gt=0)
    recurring: bool = True
    source: Literal["vidu_website", "demo"]
    source_text: str
    account_ref: str | None = Field(default=None, min_length=1, max_length=128)
    account_label: str | None = Field(default=None, min_length=1, max_length=128)


class Quote(Record):
    quote_id: str
    owner: str
    plan: Plan
    created_at: float
    expires_at: float


class PurchaseState(StrEnum):
    PREPARING = "preparing"
    LOGIN_REQUIRED = "login_required"
    AWAITING_PAYMENT = "awaiting_payment_confirmation"
    VERIFICATION_REQUIRED = "verification_required"
    PAID_PENDING_ENTITLEMENT = "paid_pending_entitlement"
    READY = "ready"
    PRICE_CHANGED = "price_changed"
    BLOCKED = "blocked"


class Checkout(Record):
    state: PurchaseState
    message: str
    handoff_url: str | None = None
    provider_reference: str | None = None


class Receipt(Record):
    """Trusted provider evidence, never accepted as MCP tool input."""

    order_reference: str
    purchase_id: str
    owner: str
    plan_id: str
    amount_minor: int = Field(gt=0)
    currency: str
    payment_succeeded: bool
    entitlement_active: bool


class Purchase(Record):
    purchase_id: str
    owner: str
    quote: Quote
    idempotency_key: str
    state: PurchaseState
    created_at: float
    updated_at: float
    message: str
    handoff_url: str | None = None
    provider_reference: str | None = None
    receipt_reference: str | None = None
