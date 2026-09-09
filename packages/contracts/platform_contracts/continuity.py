"""The Continuity Skill's output contract: does the handoff between two shots hold?

A review compares the previous shot's approved end state with the next shot's
start state and answers ``PASS``, ``REPAIRABLE`` or ``ESCALATE``. It names
what matched, what did not, the evidence it used and the smallest honest
repair. It never redesigns framing, movement or light, never rewrites an
action or a line, never promotes a frame to canon and never names a model or
provider: a review carrying any of those is refused as a whole.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CONTINUITY_VERDICTS = ("PASS", "REPAIRABLE", "ESCALATE")

CONTINUITY_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "camera",
        "framing",
        "lens",
        "lighting",
        "movement",
        "dominant_movement",
        "composition",
        "action",
        "dominant_action",
        "dialogue",
        "ending",
        "plot",
        "provider",
        "model",
        "model_id",
        "canonical_update",
        "promote",
    }
)


class ContinuityMismatch(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str = Field(min_length=1, max_length=160)
    previous: str = Field(default="", max_length=400)
    next: str = Field(default="", max_length=400)
    severity: Literal["MINOR", "MAJOR", "IDENTITY"] = "MINOR"


class ContinuityReview(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: Literal["PASS", "REPAIRABLE", "ESCALATE"]
    matched_state: list[str] = Field(default_factory=list, max_length=40)
    mismatches: list[ContinuityMismatch] = Field(default_factory=list, max_length=40)
    evidence: list[str] = Field(default_factory=list, max_length=20)
    minimal_repair: str | None = Field(default=None, max_length=600)
    approval_required: bool = False
    unresolved: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("verdict", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return str(value).strip().upper() if isinstance(value, str) else value


def continuity_authority_violations(raw: dict[str, Any]) -> list[str]:
    """Out-of-authority keys anywhere in a raw review, as ``path`` strings."""

    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_text = str(key)
                here = f"{path}.{key_text}" if path else key_text
                if key_text.lower() in CONTINUITY_FORBIDDEN_KEYS:
                    found.append(here)
                    continue
                walk(value, here)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(raw, "")
    return found
