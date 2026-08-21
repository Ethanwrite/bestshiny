from __future__ import annotations

from enum import StrEnum
from typing import Literal

from provider_sdk import AssetCriticality, ProviderTrustLevel, provider_can_handle
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelRole(StrEnum):
    DIRECTOR = "DIRECTOR"
    ASSISTANT_DIRECTOR = "ASSISTANT_DIRECTOR"
    CINEMATOGRAPHY_REASONING = "CINEMATOGRAPHY_REASONING"
    PROMPT_COMPILER = "PROMPT_COMPILER"
    PROMPT_REFINER = "PROMPT_REFINER"
    PROMPT_REFINER_LOW_COST = "PROMPT_REFINER_LOW_COST"
    PROMPT_REFINER_FALLBACK = "PROMPT_REFINER_FALLBACK"
    NARRATIVE_COMPILER = "NARRATIVE_COMPILER"
    CONTINUITY_REASONER = "CONTINUITY_REASONER"
    GENERATION_POLICY_REASONER = "GENERATION_POLICY_REASONER"
    VLM_REVIEWER = "VLM_REVIEWER"
    MULTIMODAL_EMBEDDING = "MULTIMODAL_EMBEDDING"
    VIDEO_KLING_STANDARD = "VIDEO_KLING_STANDARD"
    VIDEO_KLING_PRO = "VIDEO_KLING_PRO"
    VIDEO_FLOW = "VIDEO_FLOW"
    VIDEO_SEEDANCE = "VIDEO_SEEDANCE"
    VIDEO_VEO = "VIDEO_VEO"
    VIDEO_GROK = "VIDEO_GROK"
    VIDEO_WAN = "VIDEO_WAN"


class ModelBindingKind(StrEnum):
    PRIMARY = "PRIMARY"
    FALLBACK = "FALLBACK"


ROLE_CAPABILITY: dict[ModelRole, str] = {
    ModelRole.DIRECTOR: "director_reasoning",
    ModelRole.ASSISTANT_DIRECTOR: "assistant_director",
    ModelRole.CINEMATOGRAPHY_REASONING: "cinematography_reasoning",
    ModelRole.PROMPT_COMPILER: "prompt_compilation",
    ModelRole.PROMPT_REFINER: "prompt_refinement",
    ModelRole.PROMPT_REFINER_LOW_COST: "prompt_refinement",
    ModelRole.PROMPT_REFINER_FALLBACK: "prompt_refinement",
    ModelRole.NARRATIVE_COMPILER: "narrative_compilation",
    ModelRole.CONTINUITY_REASONER: "continuity_reasoning",
    ModelRole.GENERATION_POLICY_REASONER: "generation_policy_reasoning",
    ModelRole.VLM_REVIEWER: "multimodal_review",
    ModelRole.MULTIMODAL_EMBEDDING: "multimodal_embedding",
    ModelRole.VIDEO_KLING_STANDARD: "video_generation",
    ModelRole.VIDEO_KLING_PRO: "video_generation",
    ModelRole.VIDEO_FLOW: "video_generation",
    ModelRole.VIDEO_SEEDANCE: "video_generation",
    ModelRole.VIDEO_VEO: "video_generation",
    ModelRole.VIDEO_GROK: "video_generation",
    ModelRole.VIDEO_WAN: "video_generation",
}


class ModelDefinitionConfig(BaseModel):
    """Version-controlled defaults for a persisted model definition."""

    model_config = ConfigDict(frozen=True)

    logical_name: str = Field(min_length=1, max_length=160)
    provider: str = Field(min_length=1, max_length=80)
    provider_model_id: str = Field(min_length=1, max_length=255)
    modality: str = Field(min_length=1, max_length=50)
    capabilities: list[str] = Field(min_length=1)
    quality_tier: str = "STANDARD"
    cost_class: str = "STANDARD"
    provider_trust_level: ProviderTrustLevel
    criticality_allowed: list[AssetCriticality] = Field(min_length=1)
    enabled: bool = True
    live_enabled: bool = False
    context_window: int | None = Field(default=None, gt=0)
    max_duration: float | None = Field(default=None, gt=0)
    supported_aspect_ratios: list[str] = Field(default_factory=list)
    metadata_json: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_hard_policy(self) -> ModelDefinitionConfig:
        if self.live_enabled and not self.enabled:
            raise ValueError("a disabled model cannot be live-enabled")
        if self.provider_model_id.startswith("CONFIGURE_") and self.enabled:
            raise ValueError("placeholder provider model IDs must remain disabled")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("model capabilities must be unique")
        if len(self.criticality_allowed) != len(set(self.criticality_allowed)):
            raise ValueError("allowed asset criticalities must be unique")
        incompatible = [
            item
            for item in self.criticality_allowed
            if not provider_can_handle(self.provider_trust_level, item)
        ]
        if incompatible:
            values = ", ".join(item.value for item in incompatible)
            raise ValueError(f"provider trust cannot allow criticalities: {values}")
        return self


class ModelRoleBindingConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: ModelRole
    model_logical_name: str = Field(min_length=1, max_length=160)
    plan_tier: str = Field(default="ALL", min_length=1, max_length=40, pattern=r"^[A-Z0-9_]+$")
    binding_kind: ModelBindingKind = ModelBindingKind.PRIMARY
    priority: int = Field(default=0, ge=0)
    enabled: bool = True
    metadata_json: dict[str, object] = Field(default_factory=dict)


class ModelInfrastructureConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str = Field(min_length=1, max_length=80)
    models: list[ModelDefinitionConfig] = Field(min_length=1)
    role_bindings: list[ModelRoleBindingConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_bindings(self) -> ModelInfrastructureConfig:
        by_name = {item.logical_name: item for item in self.models}
        if len(by_name) != len(self.models):
            raise ValueError("model logical names must be unique")
        binding_keys: set[tuple[ModelRole, str, str]] = set()
        for binding in self.role_bindings:
            model = by_name.get(binding.model_logical_name)
            if model is None:
                raise ValueError(f"role binding references unknown model: {binding.model_logical_name}")
            required_capability = ROLE_CAPABILITY[binding.role]
            if required_capability not in model.capabilities:
                raise ValueError(f"{binding.role.value} requires capability {required_capability}")
            key = (binding.role, binding.plan_tier, binding.model_logical_name)
            if key in binding_keys:
                raise ValueError("duplicate role/model binding in the same plan scope")
            binding_keys.add(key)
        return self


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
    provider_trust_level: ProviderTrustLevel = ProviderTrustLevel.PRODUCTION
    criticality_allowed: list[AssetCriticality] = Field(default_factory=lambda: list(AssetCriticality))
    source: str = "configuration"

    @model_validator(mode="after")
    def validate_scores(self) -> ModelCapabilityProfile:
        values = [*self.capability_prior.values(), *self.failure_priors.values()]
        if any(value < 0 or value > 1 for value in values):
            raise ValueError("capability and failure prior scores must be in the 0.0-1.0 range")
        if any(
            not provider_can_handle(self.provider_trust_level, criticality)
            for criticality in self.criticality_allowed
        ):
            raise ValueError("criticality_allowed exceeds provider trust level")
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
    asset_criticality: AssetCriticality = AssetCriticality.STANDARD


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
