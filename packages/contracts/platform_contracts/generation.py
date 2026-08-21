from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from provider_sdk import AssetCriticality
from pydantic import BaseModel, ConfigDict, Field, model_validator

TIMELINE_FENCE_METADATA_KEY = "_server_authoritative_timeline_fence"


def authoritative_timeline_state_hash(
    state_json: dict[str, Any],
    *,
    previous_state_id: str | None,
) -> str:
    """Hash the SQL-authoritative state and its relational predecessor deterministically."""

    encoded = json.dumps(
        {
            "previous_state_id": previous_state_id,
            "state_json": state_json,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AuthoritativeTimelineFence(BaseModel):
    """Server-created snapshot that fences an Autopilot generation plan."""

    model_config = ConfigDict(frozen=True)

    version: Literal["authoritative-timeline-fence-v1"] = "authoritative-timeline-fence-v1"
    shot_id: str = Field(min_length=1)
    shot_status: str = Field(min_length=1)
    input_state_id: str = Field(min_length=1)
    input_state_hash: str = Field(min_length=64, max_length=64)
    output_state_id: str = Field(min_length=1)
    output_state_hash: str = Field(min_length=64, max_length=64)


class GenerationRequest(BaseModel):
    project_id: str
    shot_id: str | None = None
    candidate_id: str | None = None
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
    generation_policy: str = "TEXT_TO_VIDEO"
    asset_criticality: AssetCriticality = AssetCriticality.STANDARD
    cost_estimate: float = Field(default=0.0, ge=0)
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
    workspace_id: str | None = None
    default_aspect_ratio: str = "9:16"
    default_provider: str = "google_flow"
    default_language: str = "zh-CN"


class EpisodeCreate(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=200)
    episode_number: int = Field(ge=1)
    script_source: str = ""


class SceneCreate(BaseModel):
    episode_id: str
    sequence: int = Field(ge=1)
    location_id: str | None = None
    description: str = ""
    time_context: str = ""


class ShotCreate(BaseModel):
    scene_id: str
    sequence: int = Field(ge=1)
    duration: float = Field(default=8, ge=1, le=30)
    prompt: str = Field(min_length=1)
    shot_type: str = "MEDIUM"
    negative_prompt: str = ""
    provider: str = "google_flow"
    model: str = "veo"
    previous_shot_id: str | None = None
    continuity_mode: str = "NONE"
    generation_policy: str = "TEXT_TO_VIDEO"
