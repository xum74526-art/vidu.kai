"""Persisted video requests and provider contracts, separate from payment evidence."""

from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from vidu_purchase.models import Record

ModelVersion = Literal["3.0", "3.1", "3.2", "3.2_a"]
Resolution = Literal["1080p", "720p"]
AspectRatio = Literal["16:9", "9:16", "1:1", "4:3", "3:4"]
MODEL_DURATION_RANGES = {"3.0": (5, 5), "3.1": (2, 8), "3.2": (1, 16), "3.2_a": (4, 15)}
MODEL_RESOLUTIONS = {
    model: ("1080p", "720p") if model == "3.2_a" else ("1080p",)
    for model in MODEL_DURATION_RANGES
}


class VideoConfig(Record):
    prompt: str = Field(min_length=1, max_length=8000)
    task_type: Literal["text2video"] = "text2video"
    model_version: ModelVersion = "3.2"
    duration: int = Field(default=8, ge=1, le=16, strict=True)
    resolution: Resolution = "1080p"
    aspect_ratio: AspectRatio = "16:9"
    sample_count: int = Field(default=1, ge=1, le=4, strict=True)
    codec: Literal["h264", "h265"] = "h264"
    transition: Literal["pro", "speed"] | None = None
    schedule_mode: Literal["auto", "normal", "claw_pass"] = "auto"

    @field_validator("prompt")
    @classmethod
    def nonempty_prompt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("视频描述不能为空")
        return value

    @model_validator(mode="after")
    def validate_model_options(self):
        low, high = MODEL_DURATION_RANGES[self.model_version]
        if not low <= self.duration <= high:
            raise ValueError(f"模型 {self.model_version} 的视频时长需为 {low}–{high} 秒")
        if self.resolution == "720p" and self.model_version != "3.2_a":
            raise ValueError("当前适配仅允许 3.2_a 使用 720p")
        if self.transition is not None and self.model_version != "3.2":
            raise ValueError("文生视频的 pro/speed 选项仅适用于 3.2")
        return self


class ExplicitVideoConfig(VideoConfig):
    """Direct hosted calls cannot silently skip the four conversational choices."""

    model_version: ModelVersion = Field(...)
    duration: int = Field(ge=1, le=16, strict=True)
    resolution: Resolution = Field(...)
    aspect_ratio: AspectRatio = Field(...)


class AccountSnapshot(Record):
    status: Literal["connected", "not_connected", "login_required"]
    region: Literal["global", "cn"] = "global"
    credits: int | None = Field(default=None, ge=0, strict=True)
    connection_url: str | None = None
    message: str


class VideoEstimate(Record):
    cost_credits: int = Field(ge=0, strict=True)
    current_credits: int = Field(ge=0, strict=True)
    can_submit: bool = Field(strict=True)
    schedule_mode: Literal["normal", "claw_pass"]
    reason: Literal["available", "insufficient_credits", "quota_or_entitlement_blocked"]


class VideoTask(Record):
    task_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    state: Literal["queued", "running", "success", "failed"]
    media_urls: list[str] = Field(default_factory=list)


class WorkflowState(StrEnum):
    CHECKING = "checking"
    ACCOUNT_REQUIRED = "account_required"
    NEEDS_PURCHASE = "needs_purchase"
    PREPARING_PURCHASE = "preparing_purchase"
    AWAITING_PAYMENT = "awaiting_payment"
    VERIFYING_PAYMENT = "verifying_payment"
    AWAITING_ENTITLEMENT = "awaiting_entitlement"
    AWAITING_CONFIRMATION = "awaiting_generation_confirmation"
    SUBMITTING = "submitting"
    SUBMISSION_UNKNOWN = "submission_unknown"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class VideoWorkflow(Record):
    workflow_id: str
    owner: str
    idempotency_key: str
    mode: str
    binding_revision: str | None = None
    config: VideoConfig
    state: WorkflowState = WorkflowState.CHECKING
    revision: int = 0
    created_at: float
    updated_at: float
    account: AccountSnapshot | None = None
    estimate: VideoEstimate | None = None
    estimate_expires_at: float | None = None
    confirmation_id: str | None = None
    requires_continuation_confirmation: bool = False
    purchase_id: str | None = None
    purchase_quote_id: str | None = None
    purchase_attempt: int = 0
    task: VideoTask | None = None
    message: str = "正在检查账号和生成条件。"
