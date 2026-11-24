from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminMessageRequest(StrictRequest):
    body_text: str = Field(min_length=1, max_length=4000)


class InteractiveOption(StrictRequest):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    label: str = Field(min_length=1, max_length=128)


class InteractiveMessageRequest(StrictRequest):
    prompt: str = Field(min_length=1, max_length=500)
    options: list[InteractiveOption] = Field(min_length=2, max_length=10)
    fallback_text: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def stable_unique_ids(self) -> "InteractiveMessageRequest":
        ids = [option.id for option in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError("interactive option IDs must be unique")
        return self


class HandoverRequest(StrictRequest):
    reason_code: Literal[
        "unknown_question",
        "low_confidence",
        "sensitive_topic",
        "customer_requests_human",
        "contradiction",
        "manual",
    ]
    reason_summary: str | None = Field(default=None, max_length=2000)
    assigned_admin_subject: str | None = Field(default=None, max_length=256)
    take_over: bool = True


class ContextNoteRequest(StrictRequest):
    note_text: str = Field(min_length=1, max_length=5000)


class HandbackRequest(StrictRequest):
    summary: str | None = Field(default=None, max_length=5000)


class ContextCorrectionRequest(StrictRequest):
    purpose_notes: str | None = Field(default=None, max_length=5000)
    other_shared_context: dict[str, Any] | None = None


class ConsentCorrectionRequest(StrictRequest):
    consent_status: Literal["opted_in", "opted_out"]
    expected_status: Literal["unknown", "opted_in", "opted_out"]
    source_message_id: UUID
    reason: str = Field(min_length=1, max_length=2000)


class RequirementCreateRequest(StrictRequest):
    transaction_type: Literal["buy", "sell"]
    property_type: str | None = Field(default=None, max_length=64)
    locations: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    budget_min: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    budget_max: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    bedrooms_min: int | None = Field(default=None, ge=0, le=100)
    bedrooms_max: int | None = Field(default=None, ge=0, le=100)
    size_min: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    size_max: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    size_unit: str | None = Field(default=None, max_length=24)
    timing_from: date | None = None
    timing_to: date | None = None
    timing_text: str | None = Field(default=None, max_length=512)
    purpose: str | None = Field(default=None, max_length=128)
    preferences: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = Field(default=None, max_length=5000)
    match_readiness: Literal["incomplete", "ready", "needs_review"] = "incomplete"

    @model_validator(mode="after")
    def validate_ranges(self) -> "RequirementCreateRequest":
        if self.budget_min is not None and self.budget_max is not None:
            if self.budget_min > self.budget_max:
                raise ValueError("budget_min must not exceed budget_max")
        if self.size_min is not None and self.size_max is not None:
            if self.size_min > self.size_max:
                raise ValueError("size_min must not exceed size_max")
        if self.bedrooms_min is not None and self.bedrooms_max is not None:
            if self.bedrooms_min > self.bedrooms_max:
                raise ValueError("bedrooms_min must not exceed bedrooms_max")
        if self.timing_from is not None and self.timing_to is not None:
            if self.timing_from > self.timing_to:
                raise ValueError("timing_from must not exceed timing_to")
        return self


class RequirementPatchRequest(StrictRequest):
    property_type: str | None = Field(default=None, max_length=64)
    locations: list[dict[str, Any]] | None = Field(default=None, max_length=50)
    budget_min: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    budget_max: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    bedrooms_min: int | None = Field(default=None, ge=0, le=100)
    bedrooms_max: int | None = Field(default=None, ge=0, le=100)
    size_min: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    size_max: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    size_unit: str | None = Field(default=None, max_length=24)
    timing_from: date | None = None
    timing_to: date | None = None
    timing_text: str | None = Field(default=None, max_length=512)
    purpose: str | None = Field(default=None, max_length=128)
    preferences: dict[str, Any] | None = None
    notes: str | None = Field(default=None, max_length=5000)
    status: Literal["active", "paused", "fulfilled", "withdrawn", "superseded"] | None = None
    match_readiness: Literal["incomplete", "ready", "needs_review"] | None = None


class PortfolioAssetRequest(StrictRequest):
    property_type: str | None = Field(default=None, max_length=64)
    location: dict[str, Any] | None = None
    ownership_status: str | None = Field(default=None, max_length=64)
    bedrooms: int | None = Field(default=None, ge=0, le=100)
    size: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    size_unit: str | None = Field(default=None, max_length=24)
    value: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    value_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    acquisition_date: date | None = None
    portfolio_notes: str | None = Field(default=None, max_length=5000)
    status: Literal["current", "sold", "unknown"] = "unknown"


class PortfolioAssetPatchRequest(StrictRequest):
    property_type: str | None = Field(default=None, max_length=64)
    location: dict[str, Any] | None = None
    ownership_status: str | None = Field(default=None, max_length=64)
    bedrooms: int | None = Field(default=None, ge=0, le=100)
    size: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    size_unit: str | None = Field(default=None, max_length=24)
    value: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    value_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    acquisition_date: date | None = None
    portfolio_notes: str | None = Field(default=None, max_length=5000)
    status: Literal["current", "sold", "unknown"] | None = None


class RolloutControlRequest(StrictRequest):
    ai_mode: Literal["off", "shadow", "assisted", "canary", "active"]
    kill_switch: bool
    reason: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(ge=1)


class RolloutAllowlistRequest(StrictRequest):
    reason: str = Field(min_length=1, max_length=2000)
    expires_at: datetime | None = None


class ConversationCloseRequest(StrictRequest):
    reason: str = Field(min_length=1, max_length=2000)


class TranscriptExportRequest(StrictRequest):
    conversation_ids: list[str] = Field(default_factory=list, max_length=1000)
    from_date: datetime | None = None
    to_date: datetime | None = None
    handover_reason: str | None = Field(default=None, max_length=64)
    skill_key: str | None = Field(default=None, max_length=128)
    deidentified: bool = True


class SkillReviewRequest(StrictRequest):
    from_date: datetime
    to_date: datetime
    min_distinct_conversations: int = Field(default=3, ge=3, le=100)


class ProposalDecisionRequest(StrictRequest):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=2000)
