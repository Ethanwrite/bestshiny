from __future__ import annotations

from collections.abc import Mapping

from provider_sdk import provider_can_handle
from router_evidence_core import TaskType, router_scenario, router_task_type

from .registry import ModelCapabilityRegistry
from .scene_champions import SceneChampionTable
from .schemas import (
    ModelCandidate,
    ModelCapabilityProfile,
    RejectedModel,
    RouterDecision,
    RoutingEvidence,
    ShotRequirements,
)

# What each derived task type minimally requires of a profile. The existing
# ``hard_capabilities`` map only fires when a ``requires_*`` flag is set, so a
# plain text-to-video request never checked anything — a model that cannot do
# T2V at all was scoreable. This closes that gap for every task type.
_TASK_CAPABILITY: dict[TaskType, str] = {
    TaskType.T2V: "supports_t2v",
    TaskType.I2V: "supports_i2v",
    TaskType.R2V: "supports_reference_image",
    TaskType.V2V: "supports_v2v",
}

# The media role each requirement puts on the wire, in the vocabulary a
# provider declares under ``provider_metadata.modes.<mode>.accepts``. Profile
# flags such as ``supports_end_frame`` are earned by *some* mode; whether the
# mode this request resolves to carries the role is a separate fact, and the
# one the adapter enforces.
_REQUIREMENT_ROLES: tuple[tuple[str, str], ...] = (
    ("requires_start_frame", "first_frame"),
    ("requires_image_to_video", "first_frame"),
    ("requires_end_frame", "last_frame"),
    ("requires_reference_images", "reference_image"),
    ("requires_multi_reference", "reference_image"),
    ("requires_reference_video", "reference_video"),
)


class VideoModelRouter:
    # v3: scene-champion selection. The router hard-filters deterministically,
    # then selects within the hand-authored champion table for the derived
    # scenario, and only reorders champions on sufficient production evidence.
    # Open scoring survives as the fallback when no champion is defined or none
    # survives the filter.
    version = "video-router-v3"

    # This router ranks video generators and nothing else. The capability
    # registry holds every modality — chat, embedding, image — and joins them
    # all into ``all()``, so without these two the scored list silently
    # includes models that cannot produce a frame.
    modality = "video"
    required_operation = "video_generation"

    profile_weights: dict[str, dict[str, float]] = {
        "generic": {
            "visual_quality": 0.22,
            "character_consistency": 0.18,
            "scene_consistency": 0.13,
            "physical_plausibility": 0.12,
            "camera_control": 0.12,
            "complex_motion": 0.08,
            "dialogue": 0.05,
            "long_form": 0.05,
            "product_fidelity": 0.05,
        },
        "action": {
            "physical_plausibility": 0.22,
            "complex_motion": 0.20,
            "character_consistency": 0.18,
            "camera_control": 0.15,
            "visual_quality": 0.10,
            "multi_character": 0.05,
            "dialogue": 0.05,
            "scene_consistency": 0.05,
        },
        "commercial_hero": {
            "visual_quality": 0.28,
            "product_fidelity": 0.20,
            "lighting": 0.18,
            "material": 0.15,
            "camera_control": 0.08,
            "scene_consistency": 0.06,
            "character_consistency": 0.05,
        },
        "dialogue": {
            "dialogue": 0.25,
            "chinese_dialogue": 0.20,
            "character_consistency": 0.18,
            "lip_sync": 0.15,
            "scene_consistency": 0.10,
            "visual_quality": 0.07,
            "camera_control": 0.05,
        },
    }

    requirement_dimensions = {
        "requires_character_consistency": "character_consistency",
        "requires_scene_consistency": "scene_consistency",
        "requires_complex_action": "complex_motion",
        "requires_physical_plausibility": "physical_plausibility",
        "requires_camera_control": "camera_control",
        "requires_multi_character": "multi_character",
        "requires_dialogue": "dialogue",
        "requires_chinese_dialogue": "chinese_dialogue",
        "requires_text_rendering": "text_rendering",
    }

    hard_capabilities = {
        "requires_image_to_video": "supports_image_to_video",
        "requires_start_frame": "supports_start_frame",
        "requires_end_frame": "supports_end_frame",
        "requires_reference_images": "supports_reference_image",
        "requires_multi_reference": "supports_multi_reference",
        "requires_reference_video": "supports_v2v",
        "requires_native_audio": "supports_audio",
        "requires_dialogue": "supports_dialogue",
        "requires_chinese_dialogue": "supports_chinese_dialogue",
        "requires_text_rendering": "supports_text_rendering",
        "requires_camera_control": "supports_camera_instruction",
    }

    def __init__(
        self,
        registry: ModelCapabilityRegistry,
        *,
        benchmark_adjustments: Mapping[str, Mapping[str, float]] | None = None,
        production_adjustments: Mapping[str, Mapping[str, float]] | None = None,
        production_sample_counts: Mapping[str, int] | None = None,
        require_live_lifecycle: bool = True,
        scene_champions: SceneChampionTable | None = None,
    ):
        self.registry = registry
        self.require_live_lifecycle = require_live_lifecycle
        self.scene_champions = scene_champions
        self._baseline_evidence = RoutingEvidence(
            benchmark_adjustments={key: dict(value) for key, value in (benchmark_adjustments or {}).items()},
            production_adjustments={
                key: dict(value) for key, value in (production_adjustments or {}).items()
            },
            production_sample_counts=dict(production_sample_counts or {}),
        )

    @property
    def baseline_evidence(self) -> RoutingEvidence:
        """Evidence used when a caller supplies none of its own.

        Deliberately read-only. This router is a container singleton shared by
        every concurrent request; when live metrics were written onto it the
        adjustments in force during one ranking were whatever the last caller
        happened to leave behind, and no decision could be replayed. Per-request
        evidence is passed to :meth:`rank` instead.
        """

        return self._baseline_evidence

    @property
    def benchmark_adjustments(self) -> Mapping[str, Mapping[str, float]]:
        return self._baseline_evidence.benchmark_adjustments

    @property
    def production_adjustments(self) -> Mapping[str, Mapping[str, float]]:
        return self._baseline_evidence.production_adjustments

    @property
    def production_sample_counts(self) -> Mapping[str, int]:
        return self._baseline_evidence.production_sample_counts

    @staticmethod
    def _effective_capability(
        evidence: RoutingEvidence, model_key: str, dimension: str, prior: float
    ) -> float:
        benchmark = evidence.benchmark_adjustments.get(model_key, {}).get(dimension, prior)
        production = evidence.production_adjustments.get(model_key, {}).get(dimension, prior)
        sample_count = evidence.production_sample_counts.get(model_key, 0)
        if sample_count < 20:
            weights = (0.75, 0.15, 0.10)
        elif sample_count < 50:
            weights = (0.55, 0.25, 0.20)
        else:
            weights = (0.40, 0.30, 0.30)
        return max(0.0, min(1.0, weights[0] * prior + weights[1] * benchmark + weights[2] * production))

    def _eligible(
        self, profile: ModelCapabilityProfile, requirements: ShotRequirements
    ) -> list[tuple[str, str]]:
        """Hard constraints, evaluated before any score exists.

        Returns ``(reason_code, detail)`` pairs; empty means eligible. Modality
        and operation come first because they are the only ones whose failure
        means the model cannot do this job *at all* rather than not do it well.
        """

        failures: list[tuple[str, str]] = []
        if profile.modality != self.modality:
            failures.append(("MODALITY_MISMATCH", f"modality {profile.modality} is not {self.modality}"))
        if self.required_operation not in profile.supported_operations:
            failures.append(
                (
                    f"{self.required_operation.upper()}_UNSUPPORTED",
                    f"supported operations are {sorted(profile.supported_operations)}",
                )
            )
        if profile.status == "disabled":
            failures.append(("MODEL_DISABLED", "model disabled"))
        task_type = router_task_type(requirements)
        task_capability = _TASK_CAPABILITY[task_type]
        if not getattr(profile, task_capability):
            failures.append(
                ("TASK_TYPE_UNSUPPORTED", f"task type {task_type.value} requires {task_capability}")
            )
        if profile.max_duration is not None and requirements.duration > profile.max_duration:
            failures.append(("DURATION_UNSUPPORTED", f"duration exceeds {profile.max_duration:g}s"))
        if profile.min_duration is not None and requirements.duration < profile.min_duration:
            failures.append(("DURATION_UNSUPPORTED", f"duration is below {profile.min_duration:g}s"))
        failures.extend(self._mode_failures(profile, requirements, task_type))
        if requirements.max_cost_per_second is not None:
            per_second = profile.cost.get("estimated_per_second")
            if per_second is None:
                failures.append(
                    (
                        "COST_UNKNOWN",
                        "no estimated_per_second declared; a cost ceiling cannot be honored",
                    )
                )
            elif per_second > requirements.max_cost_per_second:
                failures.append(
                    (
                        "COST_LIMIT_EXCEEDED",
                        f"estimated {per_second:g} USD/s exceeds the "
                        f"{requirements.max_cost_per_second:g} USD/s ceiling",
                    )
                )
        if profile.supported_resolutions and requirements.resolution not in profile.supported_resolutions:
            failures.append(("RESOLUTION_UNSUPPORTED", f"resolution {requirements.resolution} unsupported"))
        if (
            profile.supported_aspect_ratios
            and requirements.aspect_ratio not in profile.supported_aspect_ratios
        ):
            failures.append(
                ("ASPECT_RATIO_UNSUPPORTED", f"aspect ratio {requirements.aspect_ratio} unsupported")
            )
        if requirements.reference_image_count > profile.max_reference_images:
            failures.append(
                (
                    "REFERENCE_COUNT_EXCEEDED",
                    f"reference image count exceeds {profile.max_reference_images}",
                )
            )
        if not provider_can_handle(profile.provider_trust_level, requirements.asset_criticality):
            failures.append(
                (
                    "PROVIDER_TRUST_INSUFFICIENT",
                    f"provider trust {profile.provider_trust_level.value} is below "
                    f"{requirements.asset_criticality.value} criticality",
                )
            )
        if requirements.asset_criticality not in profile.criticality_allowed:
            failures.append(
                (
                    "CRITICALITY_NOT_ALLOWED",
                    f"asset criticality {requirements.asset_criticality.value} not explicitly allowed",
                )
            )
        for requirement, capability in self.hard_capabilities.items():
            if getattr(requirements, requirement) and not getattr(profile, capability):
                failures.append(("CAPABILITY_REQUIRED", f"{capability} required"))
        return failures

    @staticmethod
    def _mode_failures(
        profile: ModelCapabilityProfile,
        requirements: ShotRequirements,
        task_type: TaskType,
    ) -> list[tuple[str, str]]:
        """Per-mode bounds, read from the profile's declared modes.

        A provider's real envelope depends on the mode a request resolves to,
        not on the profile as a whole. Wan 2.7 allows 15s normally but 10s when
        a reference video rides along (``modes.r2v.max_duration_with_reference_video``);
        its I2V mode accepts a last frame and its R2V mode does not, so a
        profile-wide ``supports_end_frame`` says nothing about whether *this*
        request's mode carries one; and I2V publishes a closed list of valid
        material sets. All three used to be enforced by the adapter, after
        routing, so a shot the champion table pinned onto Wan reached the
        adapter only to be refused on every attempt (OPEN_ISSUES §2.27, §2.28).
        A profile that declares ``provider_metadata.modes`` gets those facts
        applied here, at routing; one that declares nothing keeps the
        profile-level flags and bounds.

        A reference-video request derives ``V2V``, but providers that consume a
        reference video do so through their reference mode — Wan's is ``r2v`` —
        so V2V falls back to the ``r2v`` declaration when no ``v2v`` exists.
        """

        modes = profile.provider_metadata.get("modes")
        if not isinstance(modes, dict):
            return []
        mode_keys = ("v2v", "r2v") if task_type is TaskType.V2V else (task_type.value.lower(),)
        declaration = next(
            (modes[key] for key in mode_keys if isinstance(modes.get(key), dict)), None
        )
        if declaration is None:
            return []
        failures: list[tuple[str, str]] = []
        required_roles = {
            role for requirement, role in _REQUIREMENT_ROLES if getattr(requirements, requirement)
        }
        accepts = declaration.get("accepts")
        if isinstance(accepts, list):
            missing = sorted(required_roles - {str(item) for item in accepts})
            if missing:
                failures.append(
                    (
                        "MODE_ROLE_UNSUPPORTED",
                        f"{task_type.value} mode does not accept {', '.join(missing)}",
                    )
                )
        combinations = declaration.get("material_combinations")
        if isinstance(combinations, list) and required_roles:
            declared = [
                frozenset(str(role) for role in combination)
                for combination in combinations
                if isinstance(combination, list)
            ]
            if declared and frozenset(required_roles) not in declared:
                failures.append(
                    (
                        "MODE_COMBINATION_UNSUPPORTED",
                        f"{task_type.value} mode has no material combination for "
                        f"{{{', '.join(sorted(required_roles))}}}",
                    )
                )
        max_duration = declaration.get("max_duration")
        with_reference_video = declaration.get("max_duration_with_reference_video")
        if requirements.requires_reference_video and isinstance(with_reference_video, (int, float)):
            max_duration = with_reference_video
        if isinstance(max_duration, (int, float)) and requirements.duration > float(max_duration):
            failures.append(
                (
                    "DURATION_UNSUPPORTED",
                    f"{task_type.value} mode caps duration at {float(max_duration):g}s",
                )
            )
        min_duration = declaration.get("min_duration")
        if isinstance(min_duration, (int, float)) and requirements.duration < float(min_duration):
            failures.append(
                (
                    "DURATION_UNSUPPORTED",
                    f"{task_type.value} mode requires at least {float(min_duration):g}s",
                )
            )
        return failures

    def rank(
        self,
        requirements: ShotRequirements,
        *,
        excluded_models: set[str] | None = None,
        evidence: RoutingEvidence | None = None,
    ) -> RouterDecision:
        excluded_models = excluded_models or set()
        evidence = evidence or self._baseline_evidence
        rejected: list[RejectedModel] = []
        weights = dict(self.profile_weights[requirements.profile])
        for requirement, dimension in self.requirement_dimensions.items():
            if getattr(requirements, requirement):
                weights[dimension] = weights.get(dimension, 0.0) + 0.10
        if requirements.visual_quality_priority:
            weights["visual_quality"] = weights.get("visual_quality", 0.0) + (
                0.12 * requirements.visual_quality_priority
            )
        if requirements.product_fidelity_priority:
            weights["product_fidelity"] = weights.get("product_fidelity", 0.0) + (
                0.16 * requirements.product_fidelity_priority
            )
        total_weight = sum(weights.values()) or 1.0
        weights = {dimension: weight / total_weight for dimension, weight in weights.items()}

        candidates: list[ModelCandidate] = []
        key_by_logical: dict[str, str] = {}
        profiles = (
            self.registry.routable(require_live=self.require_live_lifecycle)
            if hasattr(self.registry, "routable")
            else self.registry.all()
        )
        for profile in profiles:
            if profile.key in excluded_models:
                rejected.append(
                    RejectedModel(
                        provider=profile.provider,
                        model=profile.model_id,
                        modality=profile.modality,
                        reason_codes=["EXCLUDED_BY_CALLER"],
                        details=["provider unconfigured or outside the request's allowed providers"],
                    )
                )
                continue
            failures = self._eligible(profile, requirements)
            if failures:
                rejected.append(
                    RejectedModel(
                        provider=profile.provider,
                        model=profile.model_id,
                        modality=profile.modality,
                        reason_codes=list(dict.fromkeys(code for code, _ in failures)),
                        details=[detail for _, detail in failures],
                    )
                )
                continue
            components: dict[str, float] = {}
            reasons: list[str] = []
            penalties: list[str] = []
            fit = 0.0
            for dimension, weight in weights.items():
                prior = profile.capability_prior.get(dimension, 0.0)
                effective = self._effective_capability(evidence, profile.key, dimension, prior)
                contribution = weight * effective
                components[dimension] = round(contribution, 5)
                fit += contribution
                if weight >= 0.12 and effective >= 0.82:
                    reasons.append(f"strong {dimension.replace('_', ' ')} fit")
            if requirements.preferred_provider == profile.provider:
                fit += 0.025
                components["preferred_provider_tiebreaker"] = 0.025
                reasons.append("matches approved project provider preference")

            failure_penalty = 0.0
            gaze_sensitive = (
                requirements.requires_end_frame_profile
                or requirements.requires_rear_view_ending
                or requirements.forbid_camera_gaze
            )
            if gaze_sensitive:
                gaze_prior = profile.failure_priors.get("end_frame_direct_gaze", 0.0)
                if gaze_prior:
                    penalty = gaze_prior * 0.35
                    failure_penalty += penalty
                    penalties.append(f"end-frame direct-gaze prior -{penalty:.3f}")
            if profile.confidence_level == "experimental":
                experimental = profile.failure_priors.get("experimental_version", 0.25) * 0.20
                failure_penalty += experimental
                penalties.append(f"experimental confidence -{experimental:.3f}")

            cost_penalty = requirements.cost_priority * profile.cost.get("normalized", 0.5) * 0.16
            latency_penalty = requirements.latency_priority * profile.latency.get("normalized", 0.5) * 0.12
            components["failure_penalty"] = round(failure_penalty, 5)
            components["cost_penalty"] = round(cost_penalty, 5)
            components["latency_penalty"] = round(latency_penalty, 5)
            if cost_penalty:
                penalties.append(f"cost priority -{cost_penalty:.3f}")
            if latency_penalty:
                penalties.append(f"latency priority -{latency_penalty:.3f}")
            score = max(0.0, min(1.0, fit - failure_penalty - cost_penalty - latency_penalty))
            key_by_logical[profile.logical_name] = profile.key
            candidates.append(
                ModelCandidate(
                    provider=profile.provider,
                    model=profile.model_id,
                    version=profile.version,
                    adapter=profile.adapter,
                    score=round(score, 5),
                    reasons=reasons or ["general capability match"],
                    penalties=penalties,
                    components=components,
                    confidence_level=profile.confidence_level,
                )
            )
        candidates.sort(key=lambda candidate: (-candidate.score, candidate.provider, candidate.model))
        rejected.sort(key=lambda item: (item.provider, item.model))
        if not candidates:
            raise LookupError(
                "no active model satisfies the shot requirements; rejected: "
                + "; ".join(
                    f"{item.provider}:{item.model}={','.join(item.reason_codes)}" for item in rejected
                )
            )
        scenario = router_scenario(requirements)
        selection_basis = "OPEN_SCORING"
        champion_audit: list[str] = []
        if self.scene_champions is not None:
            entry = self.scene_champions.champions_for(scenario)
            if entry is None:
                selection_basis = "OPEN_SCORING_NO_CHAMPION_SCENE"
            else:
                champion_keys: list[str] = []
                for binding in entry.champions:
                    key = key_by_logical.get(binding.logical_name)
                    if key is None:
                        champion_audit.append(
                            f"{binding.logical_name}: excluded by the hard filter or not routable"
                        )
                    else:
                        champion_keys.append(key)
                if not champion_keys:
                    selection_basis = "OPEN_SCORING_NO_ELIGIBLE_CHAMPION"
                    champion_audit.append(
                        "no champion survived the hard filter; open scoring decides"
                    )
                else:
                    selection_basis = "CHAMPION_TABLE"
                    candidates = self._champion_ordered(
                        candidates, champion_keys, scenario.value, evidence, champion_audit
                    )
        selected = candidates[0]
        return RouterDecision(
            recommended=selected.model,
            provider=selected.provider,
            candidates=candidates,
            rejected=rejected,
            router_version=self.version,
            profile=requirements.profile,
            scenario=scenario.value,
            selection_basis=selection_basis,
            champion_audit=champion_audit,
        )

    def _champion_ordered(
        self,
        candidates: list[ModelCandidate],
        champion_keys: list[str],
        scenario: str,
        evidence: RoutingEvidence,
        audit: list[str],
    ) -> list[ModelCandidate]:
        """Champions in table order, evidence-demoted, then everyone else.

        The manual order is the policy; production evidence corrects it only
        when it is sufficient on *both* sides of a comparison and decisive by
        more than the configured margin. Sufficiency is read from
        ``scene_sample_counts`` — observations of *this* scene — never from the
        pooled per-model counts, which mix every scene and task a model ever
        served: a physics champion must not be demoted on dialogue failures.
        Non-champions keep their score order behind the champions — they are
        the deep fallback when every champion fails at execution time, not
        competitors for the pick.
        """

        assert self.scene_champions is not None
        margin = self.scene_champions.demotion_margin
        min_samples = self.scene_champions.min_demotion_samples
        by_key = {f"{candidate.provider}:{candidate.model}": candidate for candidate in candidates}
        champions = [by_key[key] for key in champion_keys]
        swapped = True
        while swapped:
            swapped = False
            for index in range(len(champions) - 1):
                upper, lower = champions[index], champions[index + 1]
                upper_key = f"{upper.provider}:{upper.model}"
                lower_key = f"{lower.provider}:{lower.model}"
                upper_samples = evidence.scene_sample_counts.get(upper_key, 0)
                lower_samples = evidence.scene_sample_counts.get(lower_key, 0)
                if (
                    upper_samples >= min_samples
                    and lower_samples >= min_samples
                    and lower.score - upper.score > margin
                ):
                    champions[index], champions[index + 1] = lower, upper
                    audit.append(
                        f"demoted {upper_key} below {lower_key}: blended score "
                        f"{upper.score:.3f} vs {lower.score:.3f} on "
                        f"{upper_samples}/{lower_samples} observations"
                    )
                    swapped = True
        champion_key_set = set(champion_keys)
        ordered = [
            champion.model_copy(
                update={
                    "champion_rank": rank,
                    "reasons": [f"scene champion #{rank} for '{scenario}'", *champion.reasons],
                }
            )
            for rank, champion in enumerate(champions, start=1)
        ]
        ordered.extend(
            candidate
            for candidate in candidates
            if f"{candidate.provider}:{candidate.model}" not in champion_key_set
        )
        return ordered
