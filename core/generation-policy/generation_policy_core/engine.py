from __future__ import annotations

from dataclasses import asdict, dataclass

from platform_database import Database
from production_domain.models import (
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
            ),
            "seedance": ProviderCapabilities(
                supports_i2v=True,
                supports_reference_images=True,
                supports_start_frame=True,
                max_reference_images=4,
                supported_durations=(5, 10),
                supported_resolutions=("720p", "1080p"),
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
            ),
            "grok": ProviderCapabilities(supports_i2v=True, supports_start_frame=True),
            "kling": ProviderCapabilities(
                supports_i2v=True, supports_start_frame=True, supports_end_frame=True
            ),
            "runway": ProviderCapabilities(supports_i2v=True, supports_v2v=True, supports_start_frame=True),
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
        GenerationPolicy.TEXT_TO_VIDEO.value: ["narrative_state"],
        GenerationPolicy.IMAGE_TO_VIDEO.value: ["start_frame", "narrative_state"],
        GenerationPolicy.CONTINUE_I2V.value: ["previous_end_frame", "character_binding"],
        GenerationPolicy.CONTINUE_V2V.value: ["previous_video", "narrative_state"],
        GenerationPolicy.HYBRID_REFERENCE.value: ["previous_end_frame", "character_reference"],
        GenerationPolicy.REANCHOR_CHARACTER.value: ["character_reference", "scene_reference"],
        GenerationPolicy.REANCHOR_SCENE.value: ["scene_reference", "narrative_state"],
        GenerationPolicy.REANCHOR_FULL.value: ["character_reference", "scene_reference"],
        GenerationPolicy.START_END_FRAME.value: ["start_frame", "end_frame"],
        GenerationPolicy.REFERENCE_TO_VIDEO.value: ["reference_images"],
    }

    def __init__(self, database: Database, registry: ProviderCapabilityRegistry):
        self.database = database
        self.registry = registry

    def _supports(self, provider: str, policy: str) -> bool:
        capabilities = self.registry.get(provider)
        return bool(
            capabilities and all(getattr(capabilities, field) for field in self.requirements.get(policy, ()))
        )

    def _routing_score(self, provider: str, preferred_provider: str) -> tuple[float, dict]:
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
        cost_penalty = min((accepted_cost or 0.0) / 10, 0.35) if accepted_cost is not None else 0.08
        reliability_penalty = (1 - reliability) * 0.35 if reliability is not None else 0.08
        latency_penalty = min((average_latency or 0.0) / 600, 0.2) if average_latency else 0.05
        score = round(preference_penalty + cost_penalty + reliability_penalty + latency_penalty, 4)
        return score, {
            "attempts": attempts,
            "accepted": accepted,
            "cost_per_accepted_shot": accepted_cost,
            "reliability": reliability,
            "average_latency_seconds": average_latency,
            "user_preferred": provider == preferred_provider,
            "routing_score": score,
        }

    def _select(self, providers: list[str], policy: str, preferred: str) -> tuple[str | None, dict]:
        eligible = [provider for provider in providers if self._supports(provider, policy)]
        if not eligible:
            return None, {}
        scored = {provider: self._routing_score(provider, preferred) for provider in eligible}
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
    ) -> GenerationPlan:
        fallbacks = fallback_providers or []
        candidates = [preferred_provider, *[name for name in fallbacks if name != preferred_provider]]
        selected, routing_features = self._select(candidates, policy, preferred_provider)
        degraded_from = None
        resolved_policy = policy
        reasons: list[str] = []
        if selected is None and policy == GenerationPolicy.START_END_FRAME.value:
            resolved_policy = GenerationPolicy.IMAGE_TO_VIDEO.value
            degraded_from = policy
            selected, routing_features = self._select(candidates, resolved_policy, preferred_provider)
            reasons.append("START_END_UNSUPPORTED_DEGRADED_TO_I2V")
        if selected is None:
            resolved_policy = GenerationPolicy.TEXT_TO_VIDEO.value
            degraded_from = degraded_from or policy
            selected, routing_features = self._select(candidates, resolved_policy, preferred_provider)
            reasons.append("POLICY_UNSUPPORTED_DEGRADED_TO_T2V")
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
