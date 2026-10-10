"""Explicit user choices and permission to recommend; omission is not consent."""

from typing import Literal

from pydantic import Field, field_validator

from vidu_purchase.models import Record
from vidu_purchase.video_models import AspectRatio, ModelVersion, Resolution, VideoConfig

VideoField = Literal["model_version", "duration", "aspect_ratio", "resolution"]
IntendedUse = Literal["general", "vertical_short", "landscape", "square"]
VIDEO_FIELDS = ("model_version", "duration", "aspect_ratio", "resolution")


class VideoPreferences(Record):
    model_version: ModelVersion | None = None
    duration: int | None = Field(default=None, ge=1, le=16, strict=True)
    aspect_ratio: AspectRatio | None = None
    resolution: Resolution | None = None


class BriefInput(Record):
    prompt: str = Field(min_length=1, max_length=8000)
    preferences: VideoPreferences = Field(default_factory=VideoPreferences)
    use_recommended_for: list[VideoField] = Field(default_factory=list, max_length=4)
    intended_use: IntendedUse = "general"

    @field_validator("prompt")
    @classmethod
    def nonempty_prompt(cls, value):
        return VideoConfig.nonempty_prompt(value)


class BriefAnswers(Record):
    revision: int = Field(ge=0, strict=True)
    preferences: VideoPreferences = Field(default_factory=VideoPreferences)
    use_recommended_for: list[VideoField] = Field(default_factory=list, max_length=4)
    intended_use: IntendedUse | None = None


class VideoBrief(Record):
    brief_id: str
    owner: str
    idempotency_key: str
    initial_input: BriefInput
    preferences: VideoPreferences
    use_recommended_for: list[VideoField]
    intended_use: IntendedUse
    revision: int = 0
    # Freeze the resolved config before linking to a workflow, so a restart cannot
    # choose a different default or create a second request after a lost response.
    config: VideoConfig | None = None
    workflow_id: str | None = None
    last_answer_digest: str | None = None
    last_answer_revision: int | None = None
