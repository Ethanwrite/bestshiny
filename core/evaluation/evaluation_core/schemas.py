from __future__ import annotations

import math
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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


class StatePathObservation(BaseModel):
    """One judge observation for an explicit path in the expected end state."""

    path: str = Field(min_length=1, max_length=500)
    value: Any = None
    confidence: float = Field(default=0.0, ge=0, le=1)
    observable: bool = True
    source: str = Field(default="", max_length=120)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "path" not in normalized and "state_path" in normalized:
            normalized["path"] = normalized.pop("state_path")
        if "value" not in normalized:
            for alias in ("observed_value", "observed"):
                if alias in normalized:
                    normalized["value"] = normalized.pop(alias)
                    break
        return normalized

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("state observation path cannot be blank")
        return normalized

    @field_validator("confidence", mode="before")
    @classmethod
    def validate_confidence(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("state observation confidence must be a JSON number")
        confidence = float(value)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("state observation confidence must be finite and between zero and one")
        return value


class EvaluationEvidence(BaseModel):
    scores: dict[str, float] = Field(default_factory=dict)
    observations: dict[str, Any] = Field(default_factory=dict)
    state_observations: list[StatePathObservation] = Field(default_factory=list, max_length=200)
    detected_failures: list[str] = Field(default_factory=list)
    evidence_complete: bool = False
    judge_provider: str = "none"
    judge_model: str = ""
    model_execution_record_id: str | None = Field(default=None, max_length=36)

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

    @field_validator("state_observations", mode="before")
    @classmethod
    def normalize_state_observations(cls, value: Any) -> Any:
        """Accept both the typed list and a path-keyed compatibility object."""

        if value is None:
            return []
        if not isinstance(value, dict):
            return value
        normalized: list[dict[str, Any]] = []
        for path, raw_observation in value.items():
            if isinstance(raw_observation, dict):
                normalized.append({**raw_observation, "path": path})
            else:
                # A legacy scalar contains no confidence evidence. Preserve it,
                # but make it review-only instead of silently trusting it.
                normalized.append({"path": path, "value": raw_observation, "confidence": 0.0})
        return normalized

    @field_validator("state_observations")
    @classmethod
    def state_observation_paths_are_unique(
        cls,
        value: list[StatePathObservation],
    ) -> list[StatePathObservation]:
        paths = [item.path for item in value]
        if len(paths) != len(set(paths)):
            raise ValueError("state observation paths must be unique")
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
    state_decision: Literal["NOT_REQUIRED", "PASS", "REVIEW", "REJECT"] = "NOT_REQUIRED"
    state_observations: list[StatePathObservation] = Field(default_factory=list)
    model_execution_record_id: str | None = Field(default=None, max_length=36)


class RetryPlan(BaseModel):
    action: EvaluationDecision
    attempt_number: int
    terminal: bool
    next_provider: str | None = None
    next_model: str | None = None
    prompt_patch: str = ""
    inject_stronger_references: bool = False
    reasons: list[str] = Field(default_factory=list)
