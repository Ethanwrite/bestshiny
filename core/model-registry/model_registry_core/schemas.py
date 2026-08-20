from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelCapabilityProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: str
    provider: str
    version: str
    status: Literal["active", "disabled", "experimental"] = "active"
    confidence_level: Literal["initial", "experimental", "validated"] = "initial"
    max_duration: float | None = None
    supported_resolutions: list[str] = Field(default_factory=list)
    supports_text_to_video: bool = True
    supports_image_to_video: bool = False
    supports_start_frame: bool = False
    supports_end_frame: bool = False
    supports_reference_images: bool = False
    supports_reference_video: bool = False
    supports_native_audio: bool = False
    supports_dialogue: bool = False
    supports_chinese_dialogue: bool = False
    supports_text_rendering: bool = False
    capability_prior: dict[str, float] = Field(default_factory=dict)
    failure_priors: dict[str, float] = Field(default_factory=dict)
    cost: dict[str, float] = Field(default_factory=dict)
    latency: dict[str, float] = Field(default_factory=dict)
    adapter: str
    source: str = "configuration"

    @model_validator(mode="after")
    def validate_scores(self) -> ModelCapabilityProfile:
        values = [*self.capability_prior.values(), *self.failure_priors.values()]
        if any(value < 0 or value > 1 for value in values):
            raise ValueError("capability and failure prior scores must be in the 0.0-1.0 range")
        return self

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model_id}"


class ShotRequirements(BaseModel):
    duration: float = Field(default=8, ge=1, le=60)
    resolution: str = "720p"
    characters: int = Field(default=1, ge=0, le=20)
    profile: Literal["generic", "action", "commercial_hero", "dialogue"] = "generic"
    requires_image_to_video: bool = False
    requires_start_frame: bool = False
    requires_end_frame: bool = False
    requires_reference_images: bool = False
    requires_reference_video: bool = False
    requires_native_audio: bool = False
    requires_dialogue: bool = False
    requires_chinese_dialogue: bool = False
    requires_text_rendering: bool = False
    requires_character_consistency: bool = False
    requires_scene_consistency: bool = False
    requires_complex_action: bool = False
    requires_physical_plausibility: bool = False
    requires_camera_control: bool = False
    requires_multi_character: bool = False
    requires_end_frame_profile: bool = False
    requires_rear_view_ending: bool = False
    forbid_camera_gaze: bool = False
    visual_quality_priority: float = Field(default=0.5, ge=0, le=1)
    product_fidelity_priority: float = Field(default=0.0, ge=0, le=1)
    cost_priority: float = Field(default=0.3, ge=0, le=1)
    latency_priority: float = Field(default=0.2, ge=0, le=1)
    preferred_provider: str | None = None


class ModelCandidate(BaseModel):
    provider: str
    model: str
    version: str
    adapter: str
    score: float
    reasons: list[str]
    penalties: list[str]
    components: dict[str, float]
    confidence_level: str


class RouterDecision(BaseModel):
    recommended: str
    provider: str
    candidates: list[ModelCandidate]
    router_version: str
    profile: str
