from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class GenerationRequest(BaseModel):
    project_id: str
    shot_id: str | None = None
    type: Literal["image", "video"]
    provider: str = "google_flow"
    model: str = "veo"
    prompt: str = Field(min_length=1, max_length=30_000)
    negative_prompt: str = ""
    duration: float | None = Field(default=None, ge=1, le=30)
    aspect_ratio: str = "9:16"
    start_frame_asset_id: str | None = None
    end_frame_asset_id: str | None = None
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=20)
    idempotency_key: str = Field(min_length=3, max_length=250)
    priority: int = Field(default=0, ge=-100, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_frames(self) -> GenerationRequest:
        if self.end_frame_asset_id and not self.start_frame_asset_id:
            raise ValueError("end_frame_asset_id requires start_frame_asset_id")
        if self.type == "video" and self.duration is None:
            self.duration = 8
        return self


class GenerationView(BaseModel):
    id: str
    status: str
    provider: str
    model: str
    provider_job_id: str | None = None
    output_asset_id: str | None = None
    safe_to_retry: bool
    error_code: str | None = None
    error_message: str | None = None


class ProjectCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""


class EpisodeCreate(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=200)
    episode_number: int = Field(ge=1)


class SceneCreate(BaseModel):
    episode_id: str
    sequence: int = Field(ge=1)
    location_id: str | None = None
    description: str = ""


class ShotCreate(BaseModel):
    scene_id: str
    sequence: int = Field(ge=1)
    duration: float = Field(default=8, ge=1, le=30)
    prompt: str = Field(min_length=1)
    negative_prompt: str = ""
    provider: str = "google_flow"
    model: str = "veo"
    previous_shot_id: str | None = None
    continuity_mode: str = "NONE"
