from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SkillTarget = Literal["buyer", "seller", "portfolio", "general", "unknown"]


class MergeCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: uuid.UUID
    source_type: SkillTarget
    learning: str = Field(min_length=1, max_length=8_000)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)
    regression_cases: list[dict[str, str]] = Field(min_length=1, max_length=10)


class SecondReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID
    source_type: SkillTarget
    manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidates: list[MergeCandidateInput] = Field(min_length=1, max_length=50)
    active_skill_hashes: dict[str, str]
    active_skills: dict[str, str]


class CandidateRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: uuid.UUID
    decision: Literal["accept", "reject"]
    target_skill_key: SkillTarget | None = None

    @model_validator(mode="after")
    def validate_target(self) -> CandidateRouting:
        if self.decision == "accept" and self.target_skill_key is None:
            raise ValueError("accepted candidates require a target skill")
        if self.decision == "reject" and self.target_skill_key is not None:
            raise ValueError("rejected candidates must not have a target skill")
        return self


class SecondReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviews: list[CandidateRouting] = Field(min_length=1, max_length=50)
