from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ImageTaskType = Literal[
    "auto",
    "portrait",
    "beauty_fashion",
    "product",
    "commercial",
    "scene_concept",
    "reference_character_regeneration",
]


class ImagePromptCorrectRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=30_000)
    task_type: ImageTaskType = "auto"
    reference_assets: list[str] = Field(default_factory=list, max_length=20)
    model_family: str | None = None


class PromptChange(BaseModel):
    category: str
    description: str


class ImagePromptCorrectResult(BaseModel):
    original_prompt: str
    corrected_prompt: str
    detected_type: str
    identity_preservation_mode: bool
    preserved_constraints: list[str]
    # Prose for humans lives in preserved_constraints; these are the raw
    # substrings a downstream fact lock can actually anchor to.
    verbatim_spans: list[str] = Field(default_factory=list)
    editable_variables: list[str]
    changes: list[PromptChange]
    corrector_version: str
