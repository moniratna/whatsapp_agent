from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SourceType = Literal["buyer", "seller", "portfolio", "general", "unknown"]
EvidenceReference = Annotated[str, Field(pattern=r"^ev_[A-Za-z0-9_-]{1,80}$")]


class RegressionCase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    case: str = Field(min_length=10, max_length=500)
    expected_behavior: str = Field(min_length=10, max_length=500)


class LearningReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["learning", "no_learning"]
    lesson: str | None = Field(default=None, min_length=8, max_length=500)
    source_conversation_type: SourceType
    trigger: str | None = Field(default=None, min_length=4, max_length=300)
    instruction: str | None = Field(default=None, min_length=8, max_length=800)
    evidence_refs: list[EvidenceReference] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)
    duplicate_or_novel: Literal["novel", "duplicate", "not_applicable"]
    policy_conflicts: list[str] = Field(default_factory=list, max_length=10)
    regression_cases: list[RegressionCase] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_status_fields(self) -> LearningReviewOutput:
        if self.status == "no_learning":
            if any(
                (
                    self.lesson,
                    self.trigger,
                    self.instruction,
                    self.evidence_refs,
                    self.regression_cases,
                )
            ):
                raise ValueError("no_learning output must not include a candidate")
            if self.duplicate_or_novel == "novel":
                raise ValueError("no_learning output cannot be marked novel")
            return self

        if not all((self.lesson, self.trigger, self.instruction)):
            raise ValueError("learning output requires lesson, trigger, and instruction")
        if self.duplicate_or_novel == "not_applicable":
            raise ValueError("learning output must be classified novel or duplicate")
        if not self.evidence_refs:
            raise ValueError("learning output requires evidence references")
        if not self.regression_cases:
            raise ValueError("learning output requires regression cases")
        return self


class ExistingLearning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    status: Literal["active", "pending"]
    text: str = Field(min_length=1, max_length=131_072)
    fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class LearningReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_conversation_type: SourceType
    snapshot: dict[str, Any]
    active_skills: dict[str, str]
    existing_learnings: list[ExistingLearning] = Field(max_length=5_000)

