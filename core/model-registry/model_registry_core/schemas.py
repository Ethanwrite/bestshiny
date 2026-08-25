from __future__ import annotations

from enum import StrEnum
from typing import Literal

from provider_sdk import AssetCriticality, ProviderTrustLevel, provider_can_handle
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelRole(StrEnum):
    DIRECTOR = "DIRECTOR"
    ASSISTANT_DIRECTOR = "ASSISTANT_DIRECTOR"
    CINEMATOGRAPHY_REASONING = "CINEMATOGRAPHY_REASONING"
    CAMERA_MOVEMENT = "CAMERA_MOVEMENT"
    CAMERA_OPERATOR = "CAMERA_OPERATOR"
    USER_QA = "USER_QA"
    PROMPT_COMPILER = "PROMPT_COMPILER"
    PROMPT_REFINER = "PROMPT_REFINER"
    PROMPT_REFINER_LOW_COST = "PROMPT_REFINER_LOW_COST"
    PROMPT_REFINER_FALLBACK = "PROMPT_REFINER_FALLBACK"
    NARRATIVE_COMPILER = "NARRATIVE_COMPILER"
    CONTINUITY_REASONER = "CONTINUITY_REASONER"
    GENERATION_POLICY_REASONER = "GENERATION_POLICY_REASONER"
    VLM_REVIEWER = "VLM_REVIEWER"
    MULTIMODAL_EMBEDDING = "MULTIMODAL_EMBEDDING"
    STYLE_SEMANTIC_EMBEDDING = "STYLE_SEMANTIC_EMBEDDING"
    IMAGE_GENERATION = "IMAGE_GENERATION"
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
    ModelRole.CAMERA_MOVEMENT: "camera_movement_reasoning",
    ModelRole.CAMERA_OPERATOR: "camera_operation_reasoning",
    ModelRole.USER_QA: "user_qa",
    ModelRole.PROMPT_COMPILER: "prompt_compilation",
    ModelRole.PROMPT_REFINER: "prompt_refinement",
    ModelRole.PROMPT_REFINER_LOW_COST: "prompt_refinement",
    ModelRole.PROMPT_REFINER_FALLBACK: "prompt_refinement",
    ModelRole.NARRATIVE_COMPILER: "narrative_compilation",
    ModelRole.CONTINUITY_REASONER: "continuity_reasoning",
    ModelRole.GENERATION_POLICY_REASONER: "generation_policy_reasoning",
    ModelRole.VLM_REVIEWER: "multimodal_review",
    ModelRole.MULTIMODAL_EMBEDDING: "multimodal_embedding",
    ModelRole.STYLE_SEMANTIC_EMBEDDING: "style_semantic_embedding",
    ModelRole.IMAGE_GENERATION: "image_generation",
    ModelRole.VIDEO_KLING_STANDARD: "video_generation",
    ModelRole.VIDEO_KLING_PRO: "video_generation",
    ModelRole.VIDEO_FLOW: "video_generation",
    ModelRole.VIDEO_SEEDANCE: "video_generation",
    ModelRole.VIDEO_VEO: "video_generation",
    ModelRole.VIDEO_GROK: "video_generation",
    ModelRole.VIDEO_WAN: "video_generation",
}


class ModelCapabilityProfileConfig(BaseModel):
    """Version-controlled bootstrap for the persisted authoritative profile."""

    model_config = ConfigDict(frozen=True)

    profile_version: str = "1"
    confidence_level: Literal["initial", "experimental", "validated"] = "initial"
    supported_operations: list[str] = Field(min_length=1)
    supports_t2v: bool = False
    supports_i2v: bool = False
    supports_v2v: bool = False
    supports_reference_image: bool = False
    supports_multi_reference: bool = False
    supports_start_frame: bool = False
    supports_end_frame: bool = False
    supports_start_end: bool = False
    supports_character_reference: bool = False
    supports_video_extension: bool = False
    supports_camera_instruction: bool = False
    supports_audio: bool = False
    # Distinct from supports_audio, which is native audio *out*. This is a voice
    # or audio asset carried *in*, as a reference the model conditions on.
    # Conflating them means a profile can promise voice conditioning that no
    # adapter is able to send.
    supports_reference_voice: bool = False
    supports_text_rendering: bool = False
    max_reference_images: int = Field(default=0, ge=0)
    min_duration: float | None = Field(default=None, gt=0)
    max_duration: float | None = Field(default=None, gt=0)
    supported_aspect_ratios: list[str] = Field(default_factory=list)
    supported_resolutions: list[str] = Field(default_factory=list)
    physics_prior: float = Field(default=0.5, ge=0, le=1)
    identity_prior: float = Field(default=0.5, ge=0, le=1)
    camera_prior: float = Field(default=0.5, ge=0, le=1)
    render_prior: float = Field(default=0.5, ge=0, le=1)
    action_prior: float = Field(default=0.5, ge=0, le=1)
    dialogue_prior: float = Field(default=0.5, ge=0, le=1)
    text_render_prior: float = Field(default=0.5, ge=0, le=1)
    provider_metadata: dict[str, object] = Field(default_factory=dict)
    source: Literal["MANUAL_PRIOR"] = "MANUAL_PRIOR"

    @model_validator(mode="after")
    def validate_profile(self) -> ModelCapabilityProfileConfig:
        if len(self.supported_operations) != len(set(self.supported_operations)):
            raise ValueError("supported operations must be unique")
        if self.supports_multi_reference and self.max_reference_images < 2:
            raise ValueError("multi-reference support requires max_reference_images >= 2")
        if self.supports_reference_image and self.max_reference_images < 1:
            raise ValueError("reference-image support requires max_reference_images >= 1")
        if self.supports_start_end and not (self.supports_start_frame and self.supports_end_frame):
            raise ValueError("start/end support requires both frame capabilities")
        if self.min_duration and self.max_duration and self.min_duration > self.max_duration:
            raise ValueError("minimum duration cannot exceed maximum duration")
        return self


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
    capability_profile: ModelCapabilityProfileConfig | None = None

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
        if self.capability_profile is not None:
            if set(self.capability_profile.supported_operations) != set(self.capabilities):
                raise ValueError("capability profile operations must match the legacy bootstrap mirror")
            if self.max_duration != self.capability_profile.max_duration:
                raise ValueError("model max_duration must match its capability profile")
            if self.supported_aspect_ratios != self.capability_profile.supported_aspect_ratios:
                raise ValueError("model aspect ratios must match its capability profile")
        return self

    @property
    def resolved_capability_profile(self) -> ModelCapabilityProfileConfig:
        if self.capability_profile is not None:
            return self.capability_profile
        metadata = dict(self.metadata_json)
        metadata.setdefault("adapter", self.provider)
        return ModelCapabilityProfileConfig(
            supported_operations=list(self.capabilities),
            supports_t2v=self.modality == "video" and "video_generation" in self.capabilities,
            supports_camera_instruction=self.modality == "video",
            max_duration=self.max_duration,
            supported_aspect_ratios=list(self.supported_aspect_ratios),
            provider_metadata=metadata,
        )


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

    model_definition_id: str
    logical_name: str
    model_id: str
    provider: str
    modality: str
    version: str
    status: Literal["active", "disabled", "experimental"] = "active"
    confidence_level: Literal["initial", "experimental", "validated"] = "initial"
    supported_operations: list[str] = Field(default_factory=list)
    supports_t2v: bool = False
    supports_i2v: bool = False
    supports_v2v: bool = False
    supports_reference_image: bool = False
    supports_multi_reference: bool = False
    max_duration: float | None = None
    min_duration: float | None = None
    supported_aspect_ratios: list[str] = Field(default_factory=list)
    supported_resolutions: list[str] = Field(default_factory=list)
    supports_start_frame: bool = False
    supports_end_frame: bool = False
    supports_start_end: bool = False
    supports_character_reference: bool = False
    supports_video_extension: bool = False
    supports_camera_instruction: bool = False
    supports_audio: bool = False
    supports_reference_voice: bool = False
    supports_text_rendering: bool = False
    max_reference_images: int = 0
    physics_prior: float = Field(default=0.5, ge=0, le=1)
    identity_prior: float = Field(default=0.5, ge=0, le=1)
    camera_prior: float = Field(default=0.5, ge=0, le=1)
    render_prior: float = Field(default=0.5, ge=0, le=1)
    action_prior: float = Field(default=0.5, ge=0, le=1)
    dialogue_prior: float = Field(default=0.5, ge=0, le=1)
    text_render_prior: float = Field(default=0.5, ge=0, le=1)
    provider_metadata: dict[str, object] = Field(default_factory=dict)
    provider_trust_level: ProviderTrustLevel = ProviderTrustLevel.PRODUCTION
    criticality_allowed: list[AssetCriticality] = Field(default_factory=lambda: list(AssetCriticality))
    source: str = "MANUAL_PRIOR"

    @model_validator(mode="after")
    def validate_scores(self) -> ModelCapabilityProfile:
        if any(
            not provider_can_handle(self.provider_trust_level, criticality)
            for criticality in self.criticality_allowed
        ):
            raise ValueError("criticality_allowed exceeds provider trust level")
        return self

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model_id}"

    @property
    def supports_text_to_video(self) -> bool:
        return self.supports_t2v

    @property
    def supports_image_to_video(self) -> bool:
        return self.supports_i2v

    @property
    def supports_reference_images(self) -> bool:
        return self.supports_reference_image

    @property
    def supports_reference_video(self) -> bool:
        return self.supports_v2v

    @property
    def supports_native_audio(self) -> bool:
        """Audio the model *produces*. Not the same as conditioning on a voice."""

        return self.supports_audio

    @property
    def supports_voice_reference(self) -> bool:
        """A voice or audio asset the model conditions *on*."""

        return self.supports_reference_voice

    @property
    def supports_dialogue(self) -> bool:
        return self.supports_audio and self.dialogue_prior > 0

    @property
    def supports_chinese_dialogue(self) -> bool:
        return self.supports_dialogue

    @property
    def adapter(self) -> str:
        return str(self.provider_metadata.get("adapter") or self.provider)

    @property
    def failure_priors(self) -> dict[str, float]:
        value = self.provider_metadata.get("failure_priors", {})
        return {str(key): float(score) for key, score in value.items()} if isinstance(value, dict) else {}

    @property
    def cost(self) -> dict[str, float]:
        value = self.provider_metadata.get("cost", {})
        return {str(key): float(score) for key, score in value.items()} if isinstance(value, dict) else {}

    @property
    def latency(self) -> dict[str, float]:
        value = self.provider_metadata.get("latency", {})
        return {str(key): float(score) for key, score in value.items()} if isinstance(value, dict) else {}

    @property
    def capability_prior(self) -> dict[str, float]:
        return {
            "visual_quality": self.render_prior,
            "character_consistency": self.identity_prior,
            "scene_consistency": self.render_prior,
            "complex_motion": self.action_prior,
            "physical_plausibility": self.physics_prior,
            "camera_control": self.camera_prior,
            "multi_character": self.identity_prior,
            "dialogue": self.dialogue_prior,
            "chinese_dialogue": self.dialogue_prior,
            "text_rendering": self.text_render_prior,
            "product_fidelity": self.render_prior,
            "long_form": self.action_prior,
            "lighting": self.render_prior,
            "material": self.render_prior,
            "lip_sync": self.dialogue_prior,
        }


class ShotRequirements(BaseModel):
    duration: float = Field(default=8, ge=1, le=60)
    resolution: str = "720p"
    aspect_ratio: str = "9:16"
    reference_image_count: int = Field(default=0, ge=0)
    characters: int = Field(default=1, ge=0, le=20)
    profile: Literal["generic", "action", "commercial_hero", "dialogue"] = "generic"
    requires_image_to_video: bool = False
    requires_start_frame: bool = False
    requires_end_frame: bool = False
    requires_reference_images: bool = False
    requires_multi_reference: bool = False
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


class RoutingEvidence(BaseModel):
    """The measured evidence in force for one ranking, frozen at the call.

    Passed per request rather than held on the router, so that two concurrent
    rankings cannot read each other's metrics and so that a recorded decision
    can be replayed against the evidence that actually produced it.
    """

    model_config = ConfigDict(frozen=True)

    benchmark_adjustments: dict[str, dict[str, float]] = Field(default_factory=dict)
    production_adjustments: dict[str, dict[str, float]] = Field(default_factory=dict)
    production_sample_counts: dict[str, int] = Field(default_factory=dict)


class RejectedModel(BaseModel):
    """A model the router refused, and the machine-readable reason why.

    A rejected model must not merely vanish from the candidate list. Without
    this record there is no way to answer "why was that one not chosen?" after
    the fact, which is the question every routing audit actually asks.
    """

    provider: str
    model: str
    modality: str
    reason_codes: list[str]
    details: list[str] = Field(default_factory=list)


class RouterDecision(BaseModel):
    recommended: str
    provider: str
    candidates: list[ModelCandidate]
    rejected: list[RejectedModel] = Field(default_factory=list)
    router_version: str
    profile: str
