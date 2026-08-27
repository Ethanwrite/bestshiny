"""The conservative LCB: off by default, silent when thin, and never a refactor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from model_registry_core import ModelCapabilityProfile, ShotRequirements
from router_evidence_core import (
    CandidateModel,
    ConditionBucket,
    ConservativeLcbBuilder,
    HierarchicalPosteriorEngine,
    LcbSettings,
    OutcomeName,
    PosteriorLookup,
    ProductionObservation,
    ReferenceMode,
    Scenario,
    TaskType,
    merge_with_baseline,
    router_reference_mode,
    router_scenario,
    router_task_type,
)
from router_evidence_core.observations import OUTCOME_TO_ROUTER_DIMENSION

BASE = datetime(2026, 7, 1, tzinfo=UTC)
WAN = CandidateModel(provider="wan", model_id="wan-2.7", exact_version="wan-2.7")
VEO = CandidateModel(
    provider="openrouter", model_id="google/veo-3.1-fast", exact_version="veo-3.1-fast"
)


def _observations(
    candidate: CandidateModel, count: int, *, accepted_rate: float, identity: float | None = None
) -> list[ProductionObservation]:
    accepted = [index < int(count * accepted_rate) for index in range(count)]
    return [
        ProductionObservation(
            observation_id=f"{candidate.model_id}-{index:05d}",
            occurred_at=BASE + timedelta(minutes=index),
            provider=candidate.provider,
            model_id=candidate.model_id,
            exact_version=candidate.exact_version,
            task_type=TaskType.T2V,
            scenario=Scenario.MOTION,
            asset_criticality="STANDARD",
            reference_mode=ReferenceMode.NONE,
            duration_seconds=5.0,
            resolution="720P",
            generation_success=True,
            accepted_output=value,
            qc_identity_score=identity,
        )
        for index, value in enumerate(accepted)
    ]


def _lookup(observations: list[ProductionObservation]) -> PosteriorLookup:
    run = HierarchicalPosteriorEngine().compute(
        observations,
        run_id="lcb",
        outcomes=[OutcomeName.ACCEPTED_OUTPUT, OutcomeName.QC_IDENTITY],
    )
    return PosteriorLookup(run.records)


CONDITIONS = ConditionBucket(
    duration_bucket="2-5s", resolution="720P", reference_mode=ReferenceMode.NONE
)


def test_the_default_is_off_and_the_default_is_a_no_op() -> None:
    lookup = _lookup(_observations(WAN, 200, accepted_rate=0.8))
    result = ConservativeLcbBuilder(lookup).build(
        [WAN], task_type=TaskType.T2V, scenario=Scenario.MOTION, conditions=CONDITIONS
    )
    assert result.enabled is False
    assert result.is_noop is True
    assert result.fallback_reason == "FEATURE_FLAG_OFF"


def test_with_no_posterior_at_all_it_falls_back_and_says_so() -> None:
    result = ConservativeLcbBuilder(PosteriorLookup([]), LcbSettings(enabled=True)).build(
        [WAN], task_type=TaskType.T2V, scenario=Scenario.MOTION
    )
    assert result.is_noop is True
    assert result.fallback_reason == "NO_POSTERIOR_DATA"


def test_it_offers_the_lower_bound_not_the_mean() -> None:
    observations = _observations(WAN, 200, accepted_rate=0.8)
    run = HierarchicalPosteriorEngine().compute(
        observations, run_id="lcb", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    lookup = PosteriorLookup(run.records)
    result = ConservativeLcbBuilder(lookup, LcbSettings(enabled=True)).build(
        [WAN], task_type=TaskType.T2V, scenario=Scenario.MOTION, conditions=CONDITIONS
    )
    # The condition cell, not the scenario cell: every observation here shares
    # one duration bucket, resolution and reference mode, so the narrower cell
    # exists and is preferred.
    record = next(item for item in run.leaf_records())
    offered = result.adjustments["wan:wan-2.7"]["visual_quality"]
    assert offered == pytest.approx(record.posterior_lower_quantile, abs=1e-9)
    assert offered < record.posterior_mean


def test_a_thin_cell_is_omitted_so_the_router_keeps_its_hand_authored_prior() -> None:
    result = ConservativeLcbBuilder(
        _lookup(_observations(WAN, 8, accepted_rate=0.9)), LcbSettings(enabled=True)
    ).build([WAN], task_type=TaskType.T2V, scenario=Scenario.MOTION, conditions=CONDITIONS)
    assert result.is_noop is True
    assert result.fallback_reason == "NO_SUFFICIENT_CELL"
    assert any("INSUFFICIENT_OBSERVATIONS" in decision.reason for decision in result.decisions)


def test_a_candidate_whose_snapshot_is_unknown_is_never_adjusted() -> None:
    """A version the registry cannot name must not borrow another version's posterior."""

    nameless = CandidateModel(provider="wan", model_id="wan-2.7", exact_version="")
    result = ConservativeLcbBuilder(
        _lookup(_observations(WAN, 200, accepted_rate=0.8)), LcbSettings(enabled=True)
    ).build([nameless], task_type=TaskType.T2V, scenario=Scenario.MOTION)
    assert result.is_noop is True
    assert result.decisions[0].reason == "NO_EXACT_VERSION_FOR_CANDIDATE"


def test_the_wrong_scenario_finds_nothing() -> None:
    result = ConservativeLcbBuilder(
        _lookup(_observations(WAN, 200, accepted_rate=0.8)), LcbSettings(enabled=True)
    ).build([WAN], task_type=TaskType.T2V, scenario=Scenario.DIALOGUE_LIPSYNC)
    assert result.is_noop is True
    assert all(decision.reason.endswith("NO_POSTERIOR_FOR_KEY") for decision in result.decisions)


def test_two_outcomes_on_one_dimension_take_the_more_pessimistic() -> None:
    """Averaging two metrics into one dimension is the mixing this forbids."""

    observations = _observations(WAN, 200, accepted_rate=0.95, identity=0.60)
    result = ConservativeLcbBuilder(_lookup(observations), LcbSettings(enabled=True)).build(
        [WAN], task_type=TaskType.T2V, scenario=Scenario.MOTION, conditions=CONDITIONS
    )
    dimensions = result.adjustments["wan:wan-2.7"]
    assert dimensions["character_consistency"] < 0.7


def test_every_mapped_dimension_is_a_real_router_dimension() -> None:
    """Guards against a mapping to a dimension the router silently ignores."""

    profile = ModelCapabilityProfile(
        model_definition_id="d",
        logical_name="l",
        model_id="m",
        provider="p",
        modality="video",
        version="v",
    )
    known = set(profile.capability_prior)
    assert set(OUTCOME_TO_ROUTER_DIMENSION.values()) <= known
    assert "prompt_adherence" not in known


def test_the_overlay_keeps_evidence_the_lcb_says_nothing_about() -> None:
    baseline = {"openrouter:google/veo-3.1-fast": {"visual_quality": 0.71}}
    result = ConservativeLcbBuilder(
        _lookup(_observations(WAN, 200, accepted_rate=0.8)), LcbSettings(enabled=True)
    ).build([WAN], task_type=TaskType.T2V, scenario=Scenario.MOTION, conditions=CONDITIONS)
    merged = merge_with_baseline(baseline, result)
    assert merged["openrouter:google/veo-3.1-fast"]["visual_quality"] == 0.71
    assert "wan:wan-2.7" in merged


def test_the_condition_cell_wins_over_the_scenario_cell() -> None:
    at_720 = _observations(WAN, 120, accepted_rate=0.95)
    at_1080 = [
        item.model_copy(update={"resolution": "1080P", "observation_id": f"hd-{index}"})
        for index, item in enumerate(_observations(WAN, 120, accepted_rate=0.30))
    ]
    lookup = _lookup(at_720 + at_1080)
    builder = ConservativeLcbBuilder(lookup, LcbSettings(enabled=True))
    hd = builder.build(
        [WAN],
        task_type=TaskType.T2V,
        scenario=Scenario.MOTION,
        conditions=ConditionBucket(
            duration_bucket="2-5s", resolution="1080P", reference_mode=ReferenceMode.NONE
        ),
    )
    sd = builder.build(
        [WAN], task_type=TaskType.T2V, scenario=Scenario.MOTION, conditions=CONDITIONS
    )
    assert hd.adjustments["wan:wan-2.7"]["visual_quality"] < sd.adjustments["wan:wan-2.7"]["visual_quality"]


def test_a_shot_is_read_as_exactly_one_task_scene_and_reference_mode() -> None:
    requirements = ShotRequirements(
        profile="dialogue",
        requires_dialogue=True,
        requires_chinese_dialogue=True,
        requires_start_frame=True,
        requires_end_frame=True,
    )
    assert router_task_type(requirements) is TaskType.I2V
    assert router_scenario(requirements) is Scenario.CHINESE_TEXT
    assert router_reference_mode(requirements) is ReferenceMode.FIRST_LAST_FRAME


def test_a_plain_text_shot_reads_as_generic_text_to_video() -> None:
    requirements = ShotRequirements()
    assert router_task_type(requirements) is TaskType.T2V
    assert router_scenario(requirements) is Scenario.GENERIC
    assert router_reference_mode(requirements) is ReferenceMode.NONE


def test_a_reference_video_shot_is_video_to_video_whatever_else_it_carries() -> None:
    requirements = ShotRequirements(
        requires_reference_video=True, requires_reference_images=True, requires_start_frame=True
    )
    assert router_task_type(requirements) is TaskType.V2V
    assert router_reference_mode(requirements) is ReferenceMode.REFERENCE_VIDEO
