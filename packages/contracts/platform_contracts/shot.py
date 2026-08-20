from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, model_validator


class CanonicalSubjectSpec(BaseModel):
    name: str = "subject"
    asset_id: str | None = None
    asset_version_id: str | None = None
    screen_position: str = "center"
    body_orientation: str = "three-quarter toward scene"
    eyeline_target: str = Field(default="approved scene target, never the camera", min_length=1)
    pose: str = "preserve approved pose"
    wardrobe_version_id: str | None = None
    identity_constraints: list[str] = Field(default_factory=list)


class CanonicalCameraSpec(BaseModel):
    position: str = "approved position"
    angle: str = "eye level"
    framing: str = "medium"
    dominant_movement: str = Field(
        default="locked-off", validation_alias=AliasChoices("dominant_movement", "movement")
    )
    speed: str = "steady"
    path: str = "none"
    focus: str = "primary subject"
    screen_axis: str = "preserve established axis"


class CanonicalLightingSpec(BaseModel):
    direction: str = "preserve established direction"
    quality: str = "preserve established quality"
    contrast: str = "preserve established contrast"
    color_temperature: str = "preserve established color temperature"
    practicals: list[str] = Field(default_factory=list)


class CanonicalShotSpec(BaseModel):
    schema_version: str = "canonical-shot-v1"
    project_id: str = ""
    shot_id: str | None = None
    scene_id: str | None = None
    intent: str
    dominant_action: str = Field(min_length=1, validation_alias=AliasChoices("dominant_action", "action"))
    duration: float = Field(default=8, ge=1, le=60)
    aspect_ratio: str = "9:16"
    resolution: str = "720p"
    subjects: list[CanonicalSubjectSpec] = Field(default_factory=list)
    props: list[dict[str, Any]] = Field(default_factory=list)
    start_state: dict[str, Any] = Field(default_factory=dict)
    end_state: dict[str, Any] = Field(default_factory=dict)
    blocking: dict[str, Any] = Field(default_factory=dict)
    camera: CanonicalCameraSpec = Field(default_factory=CanonicalCameraSpec)
    lighting: CanonicalLightingSpec = Field(default_factory=CanonicalLightingSpec)
    dialogue: str = ""
    language: str = "zh-CN"
    audio: dict[str, Any] = Field(default_factory=dict)
    continuity: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    allow_camera_gaze: bool = False
    generation_policy: str = "TEXT_TO_VIDEO"
    profile: Literal["generic", "action", "commercial_hero", "dialogue"] = "generic"

    @model_validator(mode="after")
    def enforce_shot_invariants(self) -> CanonicalShotSpec:
        if any(not subject.eyeline_target.strip() for subject in self.subjects):
            raise ValueError("every subject requires an explicit eyeline target")
        if not self.camera.dominant_movement.strip():
            raise ValueError("one dominant camera movement is required")
        return self


class PassengerGenerationCommand(BaseModel):
    project_id: str
    media_type: Literal["image", "video"]
    provider: str
    model: str
    prompt: str = Field(min_length=1, max_length=30_000)
    negative_prompt: str = ""
    duration: float | None = Field(default=None, ge=1, le=60)
    aspect_ratio: str = "9:16"
    resolution: str = "720p"
    reference_asset_ids: list[str] = Field(default_factory=list)
    start_frame_asset_id: str | None = None
    end_frame_asset_id: str | None = None
    idempotency_key: str = Field(min_length=3, max_length=250)
    estimated_cost: float = Field(default=0, ge=0)
