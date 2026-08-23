from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PromptContinuityContext(BaseModel):
    """Explicit continuity evidence supplied to prompt compilation.

    A transition label is routing metadata, not permission to infer facts.
    Only entries in ``facts`` may become continuity assertions.
    """

    model_config = ConfigDict(frozen=True)

    transition: str | None = None
    facts: list[str | dict[str, Any]] = Field(default_factory=list)


class PromptCompilerInput(BaseModel):
    """Provider-neutral input envelope consumed by the prompt compiler Skill."""

    model_config = ConfigDict(frozen=True)

    shot_spec: dict[str, Any]
    asset_bindings: list[str] = Field(default_factory=list)
    continuity_context: PromptContinuityContext


class PromptCompilerOutput(BaseModel):
    """The only output contract accepted from prompt compilation."""

    model_config = ConfigDict(frozen=True)

    status: Literal["COMPILED", "NOT_COMPILABLE"]
    positive_prompt: str | None = None
    negative_prompt: str | None = None
    asset_bindings: list[str] = Field(default_factory=list)
    continuity_assertions: list[str] = Field(default_factory=list)
    qc_checklist: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    review_reason: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> PromptCompilerOutput:
        if self.status == "COMPILED":
            if not self.positive_prompt or not self.negative_prompt:
                raise ValueError("compiled output requires positive_prompt and negative_prompt")
            if self.missing_fields or self.review_reason:
                raise ValueError("compiled output cannot report missing fields or review reason")
        else:
            if self.positive_prompt is not None or self.negative_prompt is not None:
                raise ValueError("non-compilable output cannot contain partial prompts")
            if self.asset_bindings or self.continuity_assertions or self.qc_checklist:
                raise ValueError("non-compilable output cannot contain partial compiled artifacts")
            if not self.review_reason:
                raise ValueError("non-compilable output requires review_reason")
        return self
