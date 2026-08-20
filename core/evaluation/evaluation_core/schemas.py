from __future__ import annotations

import math
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class EvaluationDecision(StrEnum):
    ACCEPT = "ACCEPT"
    RETRY_SAME_MODEL = "RETRY_SAME_MODEL"
    RETRY_REWRITE_PROMPT = "RETRY_REWRITE_PROMPT"
    SWITCH_MODEL = "SWITCH_MODEL"
    REJECT = "REJECT"


CHECK_NAMES = (
    "identity",
    "hair",
    "wardrobe",
    "body",
    "props",
    "scene",
    "blocking",
    "eyeline",
    "lighting",
    "camera",
    "dialogue",
    "text",
    "motion",
    "continuity",
)


class EvaluationExpectation(BaseModel):
    project_id: str
    generation_id: str | None = None
    shot_id: str | None = None
    canonical_reference_asset_ids: list[str] = Field(default_factory=list)
    previous_frame_asset_id: str | None = None
    generated_asset_id: str | None = None
    shot_requirement: dict[str, Any] = Field(default_factory=dict)
    expected_state: dict[str, Any] = Field(default_factory=dict)
    required_props: list[str] = Field(default_factory=list)
    forbid_camera_gaze: bool = True
    threshold: float = Field(default=0.82, ge=0, le=1)
    minimum_check_score: float = Field(default=0.65, ge=0, le=1)


class EvaluationEvidence(BaseModel):
    scores: dict[str, float] = Field(default_factory=dict)
    observations: dict[str, Any] = Field(default_factory=dict)
    detected_failures: list[str] = Field(default_factory=list)
    evidence_complete: bool = False
    judge_provider: str = "none"
    judge_model: str = ""

    @field_validator("scores", mode="before")
    @classmethod
    def validate_scores(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("evaluation scores must be an object")
        for name, raw_score in value.items():
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                raise ValueError(f"evaluation score {name!r} must be a JSON number")
            score = float(raw_score)
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError(f"evaluation score {name!r} must be finite and between zero and one")
        return value


class EvaluationResult(BaseModel):
    decision: EvaluationDecision
    overall_score: float
    critical_failure: bool
    scores: dict[str, float]
    checks: dict[str, dict[str, Any]]
    retry_reasons: list[str]
    retry_patch: str
    evidence_complete: bool
    evaluator_version: str
    judge_provider: str
    judge_model: str


class RetryPlan(BaseModel):
    action: EvaluationDecision
    attempt_number: int
    terminal: bool
    next_provider: str | None = None
    next_model: str | None = None
    prompt_patch: str = ""
    inject_stronger_references: bool = False
    reasons: list[str] = Field(default_factory=list)
