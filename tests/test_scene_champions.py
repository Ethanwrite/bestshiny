"""Scene-champion routing: the v3 selection policy.

The router hard-filters deterministically, then selects within the
hand-authored champion table for the derived scenario. Open scoring survives
only as the fallback when no champion is defined for the scene or none
survives the filter, and production evidence reorders champions only when it
is sufficient on both sides and decisive beyond the configured margin.
"""

from __future__ import annotations

import pytest
from model_registry_core import (
    ModelCapabilityProfile,
    RoutingEvidence,
    SceneChampionTable,
    ShotRequirements,
    VideoModelRouter,
)


def _profile(
    logical_name: str,
    provider: str,
    model_id: str,
    *,
    priors: float = 0.5,
    max_duration: float = 15,
    min_duration: float = 1,
    resolutions: list[str] | None = None,
    max_reference_images: int = 4,
    estimated_per_second: float | None = 0.10,
    provider_metadata: dict[str, object] | None = None,
    **overrides: object,
) -> ModelCapabilityProfile:
    metadata: dict[str, object] = dict(provider_metadata or {})
    if estimated_per_second is not None:
        metadata.setdefault("cost", {"normalized": 0.5, "estimated_per_second": estimated_per_second})
    fields: dict[str, object] = {
        "model_definition_id": f"def-{logical_name}",
        "logical_name": logical_name,
        "model_id": model_id,
        "provider": provider,
        "modality": "video",
        "version": f"{logical_name}-test-v1",
        "supported_operations": ["video_generation"],
        "supports_t2v": True,
        "supports_i2v": True,
        "supports_reference_image": True,
        "supports_multi_reference": True,
        "supports_start_frame": True,
        "supports_end_frame": True,
        "max_duration": max_duration,
        "min_duration": min_duration,
        "supported_resolutions": resolutions or ["720p", "1080p"],
        "supported_aspect_ratios": ["9:16", "16:9"],
        "max_reference_images": max_reference_images,
        "physics_prior": priors,
        "identity_prior": priors,
        "camera_prior": priors,
        "render_prior": priors,
        "action_prior": priors,
        "dialogue_prior": priors,
        "text_render_prior": priors,
        "provider_metadata": metadata,
    }
    fields.update(overrides)
    return ModelCapabilityProfile(**fields)


class _Registry:
    """A registry stub exposing only ``all()`` so the router uses it as-is."""

    def __init__(self, profiles: list[ModelCapabilityProfile]):
        self._profiles = profiles

    def all(self, include_disabled: bool = False) -> list[ModelCapabilityProfile]:
        return list(self._profiles)


def _table(**scenes: list[str]) -> SceneChampionTable:
    return SceneChampionTable.model_validate(
        {
            "version": "test-champions",
            "demotion_margin": 0.05,
            "min_demotion_samples": 20,
            "scenes": {
                scene: {
                    "champions": [
                        {"logical_name": name, "rationale": "test judgement"} for name in names
                    ]
                }
                for scene, names in scenes.items()
            },
        }
    )


def _router(
    profiles: list[ModelCapabilityProfile],
    table: SceneChampionTable | None,
) -> VideoModelRouter:
    return VideoModelRouter(_Registry(profiles), scene_champions=table)


def test_champion_primary_wins_even_when_open_scoring_prefers_the_fallback() -> None:
    weak_primary = _profile("primary-model", "prov-a", "model-a", priors=0.5)
    strong_fallback = _profile("fallback-model", "prov-b", "model-b", priors=0.9)
    router = _router([weak_primary, strong_fallback], _table(generic=["primary-model", "fallback-model"]))

    decision = router.rank(ShotRequirements())

    assert decision.scenario == "generic"
    assert decision.selection_basis == "CHAMPION_TABLE"
    assert decision.recommended == "model-a"
    assert decision.candidates[0].champion_rank == 1
    assert decision.candidates[1].model == "model-b"
    assert decision.candidates[1].champion_rank == 2
    assert decision.candidates[1].score > decision.candidates[0].score, (
        "the fixture must make open scoring prefer the fallback, or this test proves nothing"
    )


def test_the_hard_filter_vetoes_a_champion_and_the_fallback_takes_the_scene() -> None:
    short_primary = _profile("primary-model", "prov-a", "model-a", max_duration=8)
    long_fallback = _profile("fallback-model", "prov-b", "model-b", max_duration=15)
    router = _router([short_primary, long_fallback], _table(generic=["primary-model", "fallback-model"]))

    decision = router.rank(ShotRequirements(duration=12))

    assert decision.selection_basis == "CHAMPION_TABLE"
    assert decision.recommended == "model-b"
    assert any("primary-model" in entry for entry in decision.champion_audit)
    rejected = {f"{item.provider}:{item.model}": item for item in decision.rejected}
    assert "DURATION_UNSUPPORTED" in rejected["prov-a:model-a"].reason_codes


def test_a_scene_without_champions_falls_back_to_open_scoring() -> None:
    weak = _profile("weak-model", "prov-a", "model-a", priors=0.5)
    strong = _profile("strong-model", "prov-b", "model-b", priors=0.9)
    router = _router([weak, strong], _table(motion=["weak-model"]))

    decision = router.rank(ShotRequirements())  # generic scene; table only has motion

    assert decision.selection_basis == "OPEN_SCORING_NO_CHAMPION_SCENE"
    assert decision.recommended == "model-b"
    assert all(candidate.champion_rank is None for candidate in decision.candidates)


def test_a_router_without_a_table_is_pure_open_scoring() -> None:
    weak = _profile("weak-model", "prov-a", "model-a", priors=0.5)
    strong = _profile("strong-model", "prov-b", "model-b", priors=0.9)
    router = _router([weak, strong], None)

    decision = router.rank(ShotRequirements())

    assert decision.selection_basis == "OPEN_SCORING"
    assert decision.recommended == "model-b"


def test_no_surviving_champion_falls_back_to_open_scoring_and_says_so() -> None:
    short_champion = _profile("champion-model", "prov-a", "model-a", max_duration=8)
    other = _profile("other-model", "prov-b", "model-b", max_duration=15)
    router = _router([short_champion, other], _table(generic=["champion-model"]))

    decision = router.rank(ShotRequirements(duration=12))

    assert decision.selection_basis == "OPEN_SCORING_NO_ELIGIBLE_CHAMPION"
    assert decision.recommended == "model-b"
    assert any("no champion survived" in entry for entry in decision.champion_audit)


def _demotion_fixture() -> tuple[VideoModelRouter, dict[str, dict[str, float]]]:
    primary = _profile("primary-model", "prov-a", "model-a")
    fallback = _profile("fallback-model", "prov-b", "model-b")
    router = _router([primary, fallback], _table(generic=["primary-model", "fallback-model"]))
    dimensions = [
        "visual_quality",
        "character_consistency",
        "scene_consistency",
        "physical_plausibility",
        "camera_control",
        "complex_motion",
        "dialogue",
        "long_form",
        "product_fidelity",
    ]
    adjustments = {
        "prov-a:model-a": {dimension: 0.05 for dimension in dimensions},
        "prov-b:model-b": {dimension: 0.95 for dimension in dimensions},
    }
    return router, adjustments


def test_thin_production_evidence_cannot_demote_the_primary() -> None:
    """Below the sample floor the manual order holds, whatever the scores say.

    This is the cold-start guarantee: a few dozen early observations adjust
    the blended scores, but they cannot reorder the champions.
    """

    router, adjustments = _demotion_fixture()
    evidence = RoutingEvidence(
        production_adjustments=adjustments,
        production_sample_counts={"prov-a:model-a": 19, "prov-b:model-b": 19},
        scene_sample_counts={"prov-a:model-a": 19, "prov-b:model-b": 19},
    )

    decision = router.rank(ShotRequirements(), evidence=evidence)

    assert decision.selection_basis == "CHAMPION_TABLE"
    assert decision.recommended == "model-a"
    assert not any("demoted" in entry for entry in decision.champion_audit)
    scores = {candidate.model: candidate.score for candidate in decision.candidates}
    assert scores["model-b"] - scores["model-a"] > 0.05, (
        "the fixture must produce a decisive score gap, or the sample floor is untested"
    )


def test_sufficient_decisive_scene_evidence_demotes_the_primary() -> None:
    router, adjustments = _demotion_fixture()
    evidence = RoutingEvidence(
        production_adjustments=adjustments,
        production_sample_counts={"prov-a:model-a": 25, "prov-b:model-b": 25},
        scene_sample_counts={"prov-a:model-a": 25, "prov-b:model-b": 25},
    )

    decision = router.rank(ShotRequirements(), evidence=evidence)

    assert decision.selection_basis == "CHAMPION_TABLE"
    assert decision.recommended == "model-b"
    assert decision.candidates[0].champion_rank == 1
    assert decision.candidates[1].model == "model-a"
    assert any("demoted prov-a:model-a below prov-b:model-b" in entry for entry in decision.champion_audit)


def test_pooled_per_model_counts_never_qualify_a_demotion() -> None:
    """The adaptive-router evidence pools every scene a model ever served into
    one per-model count. Plenty of pooled observations plus a decisive score
    gap must still leave the scene order alone: a physics champion is not
    demoted on dialogue failures. Only scene-scoped counts (the LCB cell for
    this request's task and scenario) can qualify a demotion."""

    router, adjustments = _demotion_fixture()
    evidence = RoutingEvidence(
        production_adjustments=adjustments,
        production_sample_counts={"prov-a:model-a": 500, "prov-b:model-b": 500},
    )

    decision = router.rank(ShotRequirements(), evidence=evidence)

    assert decision.selection_basis == "CHAMPION_TABLE"
    assert decision.recommended == "model-a"
    assert not any("demoted" in entry for entry in decision.champion_audit)
    scores = {candidate.model: candidate.score for candidate in decision.candidates}
    assert scores["model-b"] - scores["model-a"] > 0.05, (
        "the pooled counts must move the blend decisively, or the gate is untested"
    )


def test_one_sided_sample_sufficiency_cannot_demote() -> None:
    """Both sides must be measured; a well-sampled challenger cannot displace
    an unmeasured champion on scores alone."""

    router, adjustments = _demotion_fixture()
    evidence = RoutingEvidence(
        production_adjustments=adjustments,
        production_sample_counts={"prov-a:model-a": 500, "prov-b:model-b": 500},
        scene_sample_counts={"prov-a:model-a": 3, "prov-b:model-b": 500},
    )

    decision = router.rank(ShotRequirements(), evidence=evidence)

    assert decision.recommended == "model-a"


def test_plain_text_to_video_requires_the_t2v_capability() -> None:
    no_t2v = _profile("frames-only", "prov-a", "model-a", supports_t2v=False)
    full = _profile("full-model", "prov-b", "model-b")
    router = _router([no_t2v, full], None)

    decision = router.rank(ShotRequirements())

    assert decision.recommended == "model-b"
    rejected = {f"{item.provider}:{item.model}": item for item in decision.rejected}
    assert "TASK_TYPE_UNSUPPORTED" in rejected["prov-a:model-a"].reason_codes


def test_a_cost_ceiling_excludes_expensive_and_unpriced_models() -> None:
    cheap = _profile("cheap-model", "prov-a", "model-a", estimated_per_second=0.07)
    expensive = _profile("expensive-model", "prov-b", "model-b", estimated_per_second=0.14)
    unpriced = _profile("unpriced-model", "prov-c", "model-c", estimated_per_second=None)
    router = _router([cheap, expensive, unpriced], None)

    decision = router.rank(ShotRequirements(max_cost_per_second=0.10))

    assert decision.recommended == "model-a"
    rejected = {f"{item.provider}:{item.model}": item for item in decision.rejected}
    assert "COST_LIMIT_EXCEEDED" in rejected["prov-b:model-b"].reason_codes
    assert "COST_UNKNOWN" in rejected["prov-c:model-c"].reason_codes


def test_without_a_ceiling_expensive_and_unpriced_models_stay_routable() -> None:
    expensive = _profile("expensive-model", "prov-b", "model-b", estimated_per_second=0.40)
    unpriced = _profile("unpriced-model", "prov-c", "model-c", estimated_per_second=None)
    router = _router([expensive, unpriced], None)

    decision = router.rank(ShotRequirements())

    assert len(decision.candidates) == 2


def test_mode_declared_duration_ceiling_is_enforced_at_routing() -> None:
    """OPEN_ISSUES 2.27: a reference-video shot over the mode's own ceiling is
    excluded at routing, before anything can be billed, not at the adapter."""

    modes = {
        "modes": {
            "r2v": {
                "min_duration": 2,
                "max_duration": 15,
                "max_duration_with_reference_video": 10,
            }
        }
    }
    bounded = _profile(
        "bounded-model", "prov-a", "model-a", supports_v2v=True, provider_metadata=modes
    )
    router = _router([bounded], None)

    over = ShotRequirements(duration=12, requires_reference_video=True)
    with pytest.raises(LookupError, match="DURATION_UNSUPPORTED"):
        router.rank(over)

    within = router.rank(ShotRequirements(duration=8, requires_reference_video=True))
    assert within.recommended == "model-a"

    plain_r2v = router.rank(
        ShotRequirements(duration=12, requires_reference_images=True, reference_image_count=2)
    )
    assert plain_r2v.recommended == "model-a", (
        "without a reference video the ordinary 15s mode ceiling applies"
    )


def test_mode_declared_roles_veto_a_mode_that_cannot_carry_them() -> None:
    """A profile-wide ``supports_end_frame`` is earned by *some* mode. When the
    mode this request resolves to declares what it accepts and a required role
    is not among them, the model is excluded here — not refused by the adapter
    on every attempt after the champion table pinned it."""

    modes = {
        "modes": {
            "i2v": {"accepts": ["first_frame", "last_frame"]},
            "r2v": {"accepts": ["first_frame", "reference_image", "reference_video"]},
        }
    }
    declared = _profile("declared-model", "prov-a", "model-a", provider_metadata=modes)
    undeclared = _profile("plain-model", "prov-b", "model-b")
    router = _router([declared, undeclared], _table(first_last_frame=["declared-model", "plain-model"]))

    # References plus an end frame resolve to R2V, whose declaration carries
    # no last_frame: the declared model is vetoed, the fallback takes the scene.
    decision = router.rank(
        ShotRequirements(requires_reference_images=True, requires_end_frame=True, reference_image_count=2)
    )
    assert decision.scenario == "first_last_frame"
    assert decision.recommended == "model-b"
    rejected = {f"{item.provider}:{item.model}": item for item in decision.rejected}
    assert "MODE_ROLE_UNSUPPORTED" in rejected["prov-a:model-a"].reason_codes
    assert any("last_frame" in detail for detail in rejected["prov-a:model-a"].details)

    # Start plus end frame resolve to I2V, which does carry a last frame.
    framed = router.rank(ShotRequirements(requires_start_frame=True, requires_end_frame=True))
    assert framed.recommended == "model-a"


def test_mode_material_combinations_are_enforced_at_routing() -> None:
    """OPEN_ISSUES 2.28: a mode's closed list of valid material sets is read
    at routing, so a set the provider publishes as invalid never reaches the
    adapter."""

    modes = {
        "modes": {
            "i2v": {
                "accepts": ["first_frame", "last_frame"],
                "material_combinations": [["first_frame"]],
            }
        }
    }
    strict = _profile("strict-model", "prov-a", "model-a", provider_metadata=modes)
    router = _router([strict], None)

    only_first = router.rank(ShotRequirements(requires_start_frame=True))
    assert only_first.recommended == "model-a"

    with pytest.raises(LookupError, match="MODE_COMBINATION_UNSUPPORTED"):
        router.rank(ShotRequirements(requires_start_frame=True, requires_end_frame=True))


def test_container_router_reference_plus_end_frame_never_pins_wan(container) -> None:
    """The regression the champion table would otherwise introduce: reference
    images plus an end frame is a routine production shot, it reads as the
    first_last_frame scene, and Wan 2.7 — that scene's primary — resolves it
    to an R2V mode that accepts no last frame. Open scoring never picked Wan
    (all-0.5 priors); the table must not either."""

    decision = container.video_router.rank(
        ShotRequirements(
            requires_reference_images=True,
            requires_end_frame=True,
            reference_image_count=2,
            duration=8,
        )
    )

    assert decision.scenario == "first_last_frame"
    assert decision.selection_basis == "CHAMPION_TABLE"
    assert decision.recommended == "kwaivgi/kling-v3.0-pro"
    assert any("wan-2.7-official" in entry for entry in decision.champion_audit)
    rejected = {f"{item.provider}:{item.model}": item for item in decision.rejected}
    assert "MODE_ROLE_UNSUPPORTED" in rejected["wan:wan-2.7"].reason_codes


def test_container_router_selects_the_configured_generic_champion(container) -> None:
    decision = container.video_router.rank(ShotRequirements())

    assert decision.scenario == "generic"
    assert decision.selection_basis == "CHAMPION_TABLE"
    assert decision.provider == "seedance"
    assert decision.recommended == "doubao-seedance-2-5-260628"


def test_container_router_routes_start_end_frame_scenes_to_wan(container) -> None:
    """The operator's start_end_frame example, end to end: Wan 2.7's priors are
    all initial 0.5, so open scoring would never pick it — the champion table
    is what routes the scene it is documented to be good at."""

    decision = container.video_router.rank(
        ShotRequirements(requires_start_frame=True, requires_end_frame=True, duration=8)
    )

    assert decision.scenario == "first_last_frame"
    assert decision.selection_basis == "CHAMPION_TABLE"
    assert decision.provider == "wan"
    assert decision.recommended == "wan-2.7"
    assert decision.candidates[0].champion_rank == 1


def test_container_router_reference_overflow_falls_to_the_wan_fallback(container) -> None:
    """Five references exceed Kling's four, so the reference_adherence primary
    is vetoed by the hard filter and the R2V-native fallback takes the scene."""

    decision = container.video_router.rank(
        ShotRequirements(
            requires_reference_images=True,
            requires_multi_reference=True,
            reference_image_count=5,
            duration=8,
        )
    )

    assert decision.scenario == "reference_adherence"
    assert decision.selection_basis == "CHAMPION_TABLE"
    assert decision.recommended == "wan-2.7"
    assert any("kling-3-pro-openrouter" in entry for entry in decision.champion_audit)
