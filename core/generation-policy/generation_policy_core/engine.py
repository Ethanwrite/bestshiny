from __future__ import annotations

from dataclasses import asdict, dataclass

from platform_database import Database
from production_domain.models import (
    ContinuityMode,
    CostRecord,
    DecisionRecord,
    GenerationJob,
    GenerationPolicy,
    ProviderAccount,
)
from sqlalchemy import select


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_t2v: bool = True
    supports_i2v: bool = False
    supports_v2v: bool = False
    supports_reference_images: bool = False
    supports_character_reference: bool = False
    supports_start_frame: bool = False
    supports_end_frame: bool = False
    supports_start_end: bool = False
    supports_video_extension: bool = False
    supports_camera_instruction: bool = True
    supports_audio: bool = False
    max_reference_images: int = 0
    supported_aspect_ratios: tuple[str, ...] = ("9:16", "16:9")
    supported_durations: tuple[int, ...] = (5, 8)
    supported_resolutions: tuple[str, ...] = ("720p",)
    identity_reliability: float = 0.5
    camera_control_score: float = 0.5
    action_score: float = 0.5
    render_quality_score: float = 0.5


class ProviderCapabilityRegistry:
    def __init__(self):
        self._items: dict[str, ProviderCapabilities] = {
            "google_flow": ProviderCapabilities(
                supports_i2v=True,
                supports_reference_images=True,
                supports_character_reference=True,
                supports_start_frame=True,
                supports_end_frame=True,
                supports_start_end=True,
                supports_video_extension=True,
                max_reference_images=3,
                supported_durations=(5, 6, 8),
                supported_resolutions=("720p", "1080p"),
                identity_reliability=0.88,
                camera_control_score=0.90,
                action_score=0.86,
                render_quality_score=0.92,
            ),
            "seedance": ProviderCapabilities(
                supports_i2v=True,
                supports_reference_images=True,
                supports_start_frame=True,
                max_reference_images=4,
                supported_durations=(5, 10),
                supported_resolutions=("720p", "1080p"),
                identity_reliability=0.80,
                camera_control_score=0.78,
                action_score=0.90,
                render_quality_score=0.88,
            ),
            "veo_official": ProviderCapabilities(
                supports_i2v=True,
                supports_reference_images=True,
                supports_start_frame=True,
                supports_end_frame=True,
                supports_start_end=True,
                max_reference_images=3,
                supported_durations=(8,),
                supported_resolutions=("720p", "1080p"),
                identity_reliability=0.86,
                camera_control_score=0.90,
                action_score=0.88,
                render_quality_score=0.94,
            ),
            "grok": ProviderCapabilities(
                supports_i2v=True,
                supports_start_frame=True,
                identity_reliability=0.58,
                camera_control_score=0.68,
                action_score=0.73,
                render_quality_score=0.76,
            ),
            "kling": ProviderCapabilities(
                supports_i2v=True,
                supports_start_frame=True,
                supports_end_frame=True,
                identity_reliability=0.78,
                camera_control_score=0.82,
                action_score=0.84,
                render_quality_score=0.86,
            ),
            "runway": ProviderCapabilities(
                supports_i2v=True,
                supports_v2v=True,
                supports_start_frame=True,
                identity_reliability=0.72,
                camera_control_score=0.80,
                action_score=0.82,
                render_quality_score=0.83,
            ),
            "omni": ProviderCapabilities(supports_reference_images=True, max_reference_images=5),
        }

    def register(self, name: str, capabilities: ProviderCapabilities) -> None:
        self._items[name] = capabilities

    def get(self, name: str) -> ProviderCapabilities | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return sorted(self._items)


@dataclass(frozen=True)
class GenerationPlan:
    policy: str
    required_inputs: list[str]
    provider: str
    fallback_providers: list[str]
    quality_profile: str
    degraded_from: str | None = None
    reasons: tuple[str, ...] = ()


class GenerationPolicyInputError(ValueError):
    pass


@dataclass(frozen=True)
class AvailableGenerationAssets:
    previous_end_frame_asset_id: str | None = None
    previous_video_asset_id: str | None = None
    start_frame_asset_id: str | None = None
    end_frame_asset_id: str | None = None
    character_reference_asset_ids: tuple[str, ...] = ()
    scene_reference_asset_ids: tuple[str, ...] = ()
    generic_reference_asset_ids: tuple[str, ...] = ()
    character_binding: bool = False
    narrative_state: bool = True
    current_action_prompt: bool = True
    current_camera_prompt: bool = True

    def available_inputs(self) -> frozenset[str]:
        values: set[str] = set()
        if self.previous_end_frame_asset_id:
            # A previous end frame can be soft HYBRID context. It becomes a
            # strong start frame only when the selected policy promotes it.
            values.add("previous_end_frame")
        if self.previous_video_asset_id:
            values.add("previous_video")
        if self.start_frame_asset_id:
            values.add("start_frame")
        if self.end_frame_asset_id:
            values.add("end_frame")
        if self.character_reference_asset_ids:
            values.update({"character_reference", "character_master"})
        if self.scene_reference_asset_ids:
            values.add("scene_reference")
        if self.generic_reference_asset_ids:
            values.add("reference_images")
        if self.character_binding:
            values.add("character_binding")
        if self.narrative_state:
            values.add("narrative_state")
        if self.current_action_prompt:
            values.update({"current_action_prompt", "shot_prompt"})
        if self.current_camera_prompt:
            values.update({"current_camera_prompt", "current_camera_state"})
        return frozenset(values)


@dataclass(frozen=True)
class GenerationPolicyDecision:
    policy: str
    required_inputs: tuple[str, ...]
    reason_codes: tuple[str, ...]
    use_previous_end_frame_as_start: bool
    require_new_keyframe: bool


class GenerationPolicyEngine:
    """Translate continuity intent and canonical assets into a provider-neutral policy."""

    version = "generation-policy-rules-v2"

    def __init__(self, database: Database):
        self.database = database

    def decide(
        self,
        continuity_mode: str,
        assets: AvailableGenerationAssets,
        *,
        project_id: str | None = None,
        shot_id: str | None = None,
    ) -> GenerationPolicyDecision:
        available = assets.available_inputs()
        reasons: list[str]
        required: tuple[str, ...]
        if continuity_mode == ContinuityMode.HARD_CONTINUITY.value:
            policy = GenerationPolicy.CONTINUE_I2V.value
            required = ("previous_end_frame", "character_binding", "current_action_prompt")
            reasons = ["HARD_CONTINUITY", "INHERIT_PREVIOUS_END_FRAME"]
        elif continuity_mode == ContinuityMode.HYBRID.value:
            if {"previous_end_frame", "character_reference"}.issubset(available):
                policy = GenerationPolicy.HYBRID_REFERENCE.value
                required = (
                    "previous_end_frame",
                    "character_reference",
                    "current_camera_state",
                    "narrative_state",
                    "shot_prompt",
                )
                reasons = ["HYBRID_CONTINUITY", "END_FRAME_AS_SOFT_CONTEXT"]
            elif {"character_reference", "scene_reference"}.issubset(available):
                policy = GenerationPolicy.REANCHOR_FULL.value
                required = (
                    "character_reference",
                    "scene_reference",
                    "narrative_state",
                    "current_camera_prompt",
                )
                reasons = ["HYBRID_CONTEXT_INCOMPLETE", "CANONICAL_REANCHOR_SAFER"]
            else:
                raise GenerationPolicyInputError(
                    "hybrid continuity requires an end frame plus character reference, "
                    "or complete canonical re-anchor references"
                )
        elif continuity_mode == ContinuityMode.RE_ANCHOR.value:
            policy = GenerationPolicy.REANCHOR_FULL.value
            required = (
                "character_reference",
                "scene_reference",
                "narrative_state",
                "current_camera_prompt",
            )
            reasons = ["RE_ANCHOR", "IGNORE_PREVIOUS_END_FRAME", "CANONICAL_REFERENCES_REQUIRED"]
        elif assets.start_frame_asset_id and assets.end_frame_asset_id:
            policy = GenerationPolicy.START_END_FRAME.value
            required = ("start_frame", "end_frame")
            reasons = ["EXPLICIT_START_AND_END_FRAMES"]
        elif assets.start_frame_asset_id:
            policy = GenerationPolicy.IMAGE_TO_VIDEO.value
            required = ("start_frame", "narrative_state")
            reasons = ["EXPLICIT_START_FRAME"]
        elif assets.generic_reference_asset_ids:
            policy = GenerationPolicy.REFERENCE_TO_VIDEO.value
            required = ("reference_images", "narrative_state")
            reasons = ["REFERENCE_IMAGES_AVAILABLE"]
        else:
            policy = GenerationPolicy.TEXT_TO_VIDEO.value
            required = ("narrative_state", "current_action_prompt")
            reasons = ["NO_CONTINUITY_DEPENDENCY"]

        missing = sorted(set(required).difference(available))
        if missing:
            raise GenerationPolicyInputError(
                f"{policy} is missing required canonical inputs: {', '.join(missing)}"
            )
        decision = GenerationPolicyDecision(
            policy=policy,
            required_inputs=required,
            reason_codes=tuple(reasons),
            use_previous_end_frame_as_start=policy == GenerationPolicy.CONTINUE_I2V.value,
            require_new_keyframe=policy.startswith("REANCHOR_"),
        )
        if project_id or shot_id:
            with self.database.session() as session:
                session.add(
                    DecisionRecord(
                        project_id=project_id,
                        shot_id=shot_id,
                        decision_type="GENERATION_POLICY_DECISION",
                        input_features={
                            "continuity_mode": continuity_mode,
                            "available_inputs": sorted(available),
                            "required_inputs": list(required),
                        },
                        selected_action=policy,
                        reason_codes=list(decision.reason_codes),
                        model_version=self.version,
                        policy_version="generation-policy-v2",
                    )
                )
        return decision


class CapabilityResolver:
    version = "capability-rules-v1"

    requirements = {
        GenerationPolicy.TEXT_TO_VIDEO.value: ("supports_t2v",),
        GenerationPolicy.IMAGE_TO_VIDEO.value: ("supports_i2v", "supports_start_frame"),
        GenerationPolicy.CONTINUE_I2V.value: ("supports_i2v", "supports_start_frame"),
        GenerationPolicy.CONTINUE_V2V.value: ("supports_v2v",),
        GenerationPolicy.HYBRID_REFERENCE.value: ("supports_reference_images",),
        GenerationPolicy.REANCHOR_CHARACTER.value: ("supports_character_reference",),
        GenerationPolicy.REANCHOR_SCENE.value: ("supports_reference_images",),
        GenerationPolicy.REANCHOR_FULL.value: ("supports_character_reference", "supports_reference_images"),
        GenerationPolicy.START_END_FRAME.value: ("supports_start_end",),
        GenerationPolicy.REFERENCE_TO_VIDEO.value: ("supports_reference_images",),
    }

    required_inputs = {
        GenerationPolicy.TEXT_TO_VIDEO.value: ["narrative_state", "current_action_prompt"],
        GenerationPolicy.IMAGE_TO_VIDEO.value: ["start_frame", "narrative_state"],
        GenerationPolicy.CONTINUE_I2V.value: [
            "previous_end_frame",
            "character_binding",
            "current_action_prompt",
        ],
        GenerationPolicy.CONTINUE_V2V.value: ["previous_video", "narrative_state"],
        GenerationPolicy.HYBRID_REFERENCE.value: [
            "previous_end_frame",
            "character_reference",
            "current_camera_state",
            "narrative_state",
            "shot_prompt",
        ],
        GenerationPolicy.REANCHOR_CHARACTER.value: [
            "character_reference",
            "narrative_state",
            "current_camera_prompt",
        ],
        GenerationPolicy.REANCHOR_SCENE.value: ["scene_reference", "narrative_state"],
        GenerationPolicy.REANCHOR_FULL.value: [
            "character_reference",
            "scene_reference",
            "narrative_state",
            "current_camera_prompt",
        ],
        GenerationPolicy.START_END_FRAME.value: ["start_frame", "end_frame"],
        GenerationPolicy.REFERENCE_TO_VIDEO.value: ["reference_images", "narrative_state"],
    }

    def __init__(self, database: Database, registry: ProviderCapabilityRegistry):
        self.database = database
        self.registry = registry

    def _supports(self, provider: str, policy: str) -> bool:
        capabilities = self.registry.get(provider)
        return bool(
            capabilities and all(getattr(capabilities, field) for field in self.requirements.get(policy, ()))
        )

    def _routing_score(
        self, provider: str, preferred_provider: str, quality_profile: str
    ) -> tuple[float, dict]:
        with self.database.session() as session:
            costs = list(session.scalars(select(CostRecord).where(CostRecord.provider == provider)))
            accounts = list(
                session.scalars(select(ProviderAccount).where(ProviderAccount.provider == provider))
            )
            jobs = list(
                session.scalars(
                    select(GenerationJob).where(
                        GenerationJob.provider == provider,
                        GenerationJob.completed_at.is_not(None),
                    )
                )
            )
        attempts = len(costs)
        accepted = sum(1 for record in costs if record.accepted)
        total_cost = sum(record.actual_cost + record.retry_cost for record in costs)
        accepted_cost = total_cost / accepted if accepted else None
        successes = sum(account.success_count for account in accounts)
        failures = sum(account.error_count for account in accounts)
        reliability = successes / max(1, successes + failures) if accounts else None
        latencies = [
            (job.completed_at - job.started_at).total_seconds()
            for job in jobs
            if job.started_at and job.completed_at
        ]
        average_latency = sum(latencies) / len(latencies) if latencies else None
        preference_penalty = 0.0 if provider == preferred_provider else 0.18
        capabilities = self.registry.get(provider)
        assert capabilities is not None
        profile = quality_profile.upper()
        if profile in {"CLOSE_UP_CHARACTER", "DIALOGUE"}:
            expected_quality = (
                capabilities.identity_reliability * 0.65
                + capabilities.camera_control_score * 0.20
                + capabilities.render_quality_score * 0.15
            )
        elif profile == "ACTION":
            expected_quality = (
                capabilities.action_score * 0.55
                + capabilities.camera_control_score * 0.30
                + capabilities.identity_reliability * 0.15
            )
        else:
            expected_quality = (
                capabilities.render_quality_score * 0.50
                + capabilities.camera_control_score * 0.30
                + capabilities.action_score * 0.20
            )
        capability_penalty = (1 - expected_quality) * 0.45
        cost_penalty = min((accepted_cost or 0.0) / 10, 0.35) if accepted_cost is not None else 0.08
        reliability_penalty = (1 - reliability) * 0.35 if reliability is not None else 0.08
        latency_penalty = min((average_latency or 0.0) / 600, 0.2) if average_latency else 0.05
        score = round(
            preference_penalty + capability_penalty + cost_penalty + reliability_penalty + latency_penalty,
            4,
        )
        return score, {
            "attempts": attempts,
            "accepted": accepted,
            "cost_per_accepted_shot": accepted_cost,
            "reliability": reliability,
            "average_latency_seconds": average_latency,
            "user_preferred": provider == preferred_provider,
            "expected_task_quality": round(expected_quality, 4),
            "capability_penalty": round(capability_penalty, 4),
            "routing_score": score,
        }

    def _select(
        self, providers: list[str], policy: str, preferred: str, quality_profile: str
    ) -> tuple[str | None, dict]:
        eligible = [provider for provider in providers if self._supports(provider, policy)]
        if not eligible:
            return None, {}
        scored = {
            provider: self._routing_score(provider, preferred, quality_profile) for provider in eligible
        }
        selected = min(eligible, key=lambda provider: scored[provider][0])
        return selected, {provider: detail for provider, (_, detail) in scored.items()}

    def resolve(
        self,
        policy: str,
        preferred_provider: str,
        fallback_providers: list[str] | None = None,
        *,
        project_id: str | None = None,
        shot_id: str | None = None,
        quality_profile: str = "DIALOGUE",
        available_inputs: set[str] | frozenset[str] | None = None,
    ) -> GenerationPlan:
        fallbacks = fallback_providers or []
        candidates = [preferred_provider, *[name for name in fallbacks if name != preferred_provider]]
        if available_inputs is not None:
            missing = sorted(set(self.required_inputs.get(policy, [])).difference(available_inputs))
            if missing:
                raise GenerationPolicyInputError(
                    f"{policy} cannot be resolved without inputs: {', '.join(missing)}"
                )
        selected, routing_features = self._select(candidates, policy, preferred_provider, quality_profile)
        degraded_from = None
        resolved_policy = policy
        reasons: list[str] = []
        if selected is None and policy == GenerationPolicy.START_END_FRAME.value:
            resolved_policy = GenerationPolicy.IMAGE_TO_VIDEO.value
            degraded_from = policy
            selected, routing_features = self._select(
                candidates, resolved_policy, preferred_provider, quality_profile
            )
            reasons.append("START_END_UNSUPPORTED_DEGRADED_TO_I2V")
        if selected is None:
            raise LookupError(f"no provider can execute {policy}")
        plan = GenerationPlan(
            resolved_policy,
            self.required_inputs.get(resolved_policy, []),
            selected,
            [name for name in candidates if name != selected and self._supports(name, resolved_policy)],
            quality_profile,
            degraded_from,
            tuple(reasons or ["CAPABILITY_MATCH"]),
        )
        if project_id or shot_id:
            with self.database.session() as session:
                session.add(
                    DecisionRecord(
                        project_id=project_id,
                        shot_id=shot_id,
                        decision_type="CAPABILITY_RESOLUTION",
                        input_features={
                            "desired_policy": policy,
                            "preferred_provider": preferred_provider,
                            "capabilities": asdict(self.registry.get(selected)),
                            "provider_scores": routing_features,
                        },
                        selected_action=f"{selected}:{resolved_policy}",
                        reason_codes=list(plan.reasons),
                        model_version=self.version,
                        policy_version="v1",
                    )
                )
        return plan
