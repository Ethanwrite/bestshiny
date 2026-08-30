from __future__ import annotations

from typing import Any, Literal

from provider_sdk import AssetCriticality
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
    style_lock: dict[str, Any] = Field(default_factory=dict)
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


IMAGE_CREATIVE_TASKS = (
    "auto",
    "portrait",
    "beauty_fashion",
    "product",
    "commercial",
    "scene_concept",
    "reference_character_regeneration",
)


class PassengerGenerationCommand(BaseModel):
    project_id: str
    media_type: Literal["image", "video"]
    # Image requests never name a model: the caller states creative intent and the
    # router resolves the target before anything is quoted or reserved. Video
    # requests may name one, and then that exact model runs or the request fails.
    provider: str = ""
    model: str = ""
    image_task: Literal[IMAGE_CREATIVE_TASKS] = "auto"  # type: ignore[valid-type]
    # A public image-quality level ("shiny" / "shinier" / "shiniest"), mapped
    # to a concrete model server-side. Never a model ID: the browser cannot
    # name one for images.
    image_tier: str | None = Field(default=None, max_length=40)
    model_role: str | None = Field(default=None, max_length=80)
    asset_criticality: AssetCriticality = AssetCriticality.STANDARD
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
    estimated_credits: int | None = Field(default=None, ge=1, exclude=True)
    pricing_version: str = Field(default="", max_length=80, exclude=True)

    @model_validator(mode="after")
    def apply_starter_video_duration(self) -> PassengerGenerationCommand:
        """Keep the default Free starter request within the audited 50-credit grant.

        Explicit durations remain authoritative and are still rejected by admission
        when the workspace cannot reserve their server-calculated price.
        """

        if self.media_type == "video" and self.duration is None:
            self.duration = 4
        return self
