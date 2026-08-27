"""The hierarchical posterior: what it pools, what it refuses to pool, and what it admits."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from router_evidence_core import (
    ConditionBucket,
    EvidenceLayer,
    ExternalPriorContribution,
    HierarchicalPosteriorEngine,
    OutcomeName,
    PosteriorLevel,
    ProductionObservation,
    ReferenceMode,
    Scenario,
    TaskType,
    audit_contamination,
    quarantine_unversioned,
)
from router_evidence_core.observations import OUTCOME_SCALES
from router_evidence_core.posterior import summarize_cost_and_latency

BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _observation(
    index: int,
    *,
    provider: str = "wan",
    model_id: str = "wan-2.7",
    version: str = "wan-2.7",
    task: TaskType = TaskType.T2V,
    scenario: Scenario = Scenario.MOTION,
    accepted: bool | None = True,
    success: bool = True,
    resolution: str = "720P",
    duration: float = 5.0,
    reference_mode: ReferenceMode = ReferenceMode.NONE,
    alias: bool = False,
    cost: float | None = 44.0,
    latency: int | None = 42_000,
    identity: float | None = None,
) -> ProductionObservation:
    return ProductionObservation(
        observation_id=f"obs-{provider}-{index:05d}",
        occurred_at=BASE + timedelta(minutes=index),
        provider=provider,
        model_id=model_id,
        exact_version=version,
        model_is_alias=alias,
        task_type=task,
        scenario=scenario,
        asset_criticality="STANDARD",
        reference_mode=reference_mode,
        duration_seconds=duration,
        resolution=resolution,
        generation_success=success,
        provider_failure=None if success else "PROVIDER_TIMEOUT",
        latency_ms=latency,
        cost_credits=cost,
        accepted_output=accepted if success else None,
        qc_identity_score=identity if success else None,
    )


def test_the_posterior_recovers_a_known_rate() -> None:
    accepted = [True] * 160 + [False] * 40
    observations = [_observation(i, accepted=value) for i, value in enumerate(accepted)]
    run = HierarchicalPosteriorEngine().compute(
        observations, run_id="r", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    scenario = next(record for record in run.scenario_records())
    assert scenario.posterior_mean == pytest.approx(0.80, abs=0.02)
    assert scenario.posterior_lower_quantile < scenario.posterior_mean < scenario.posterior_upper_quantile
    assert scenario.observation_count == 200


def test_every_level_of_the_hierarchy_is_produced() -> None:
    observations = [
        _observation(i, task=TaskType.T2V if i % 2 else TaskType.I2V, scenario=Scenario.MOTION)
        for i in range(40)
    ]
    run = HierarchicalPosteriorEngine().compute(
        observations, run_id="r", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    levels = {record.level for record in run.records}
    assert levels == {
        PosteriorLevel.VERSION,
        PosteriorLevel.TASK,
        PosteriorLevel.SCENARIO,
        PosteriorLevel.CONDITION,
    }


def test_two_versions_of_one_model_never_share_a_parent() -> None:
    """The mechanical guarantee against score inheritance across versions."""

    good = [_observation(i, version="wan-2.7", accepted=True) for i in range(60)]
    bad = [_observation(1000 + i, version="wan-3.0", accepted=False) for i in range(60)]
    run = HierarchicalPosteriorEngine().compute(
        good + bad, run_id="r", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    by_version = {
        record.key.exact_version: record
        for record in run.records
        if record.level is PosteriorLevel.VERSION
    }
    assert by_version["wan-2.7"].posterior_mean > 0.9
    assert by_version["wan-3.0"].posterior_mean < 0.1
    # Both shrank towards the fixed global prior, never towards each other.
    assert {record.parent_level for record in by_version.values()} == {PosteriorLevel.GLOBAL}
    assert len({record.parent_mean for record in by_version.values()}) == 1


def test_a_sparse_scenario_is_pulled_towards_its_own_task_not_another_model() -> None:
    dense = [_observation(i, scenario=Scenario.MOTION, accepted=True) for i in range(100)]
    sparse = [_observation(500 + i, scenario=Scenario.PHYSICS, accepted=False) for i in range(2)]
    other = [
        _observation(900 + i, provider="openrouter", model_id="google/veo-3.1", version="veo-3.1")
        for i in range(100)
    ]
    run = HierarchicalPosteriorEngine().compute(
        dense + sparse + other, run_id="r", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    physics = next(
        record
        for record in run.scenario_records()
        if record.key.scenario is Scenario.PHYSICS and record.key.exact_version == "wan-2.7"
    )
    # Two zeros against a task that is otherwise near 1.0: the mean lands
    # between them, and the interval is wide enough to say it does not know.
    assert 0.1 < physics.posterior_mean < 0.9
    # Eight effective observations' worth of mass — six borrowed from the task
    # and two of its own — so the interval spans well over a third of the range.
    assert physics.interval_width > 0.3
    assert physics.sufficient is False


def test_strict_isolation_removes_all_pooling() -> None:
    dense = [_observation(i, scenario=Scenario.MOTION, accepted=True) for i in range(100)]
    sparse = [_observation(500 + i, scenario=Scenario.PHYSICS, accepted=False) for i in range(2)]
    strict = HierarchicalPosteriorEngine(strict_isolation=True).compute(
        dense + sparse, run_id="r", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    physics = next(
        record for record in strict.scenario_records() if record.key.scenario is Scenario.PHYSICS
    )
    assert physics.posterior_mean < 0.3
    assert strict.strict_isolation is True


def test_conditions_split_the_leaf() -> None:
    observations = [
        _observation(i, resolution="720P" if i % 2 else "1080P", accepted=i % 2 == 0)
        for i in range(80)
    ]
    run = HierarchicalPosteriorEngine().compute(
        observations, run_id="r", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    leaves = run.leaf_records()
    resolutions = {record.condition.resolution for record in leaves if record.condition}
    assert resolutions == {"720P", "1080P"}


def test_an_unobserved_outcome_is_not_a_zero() -> None:
    """A shot nobody rated must never become a one-star."""

    observations = [_observation(i, accepted=None) for i in range(50)]
    run = HierarchicalPosteriorEngine().compute(
        observations, run_id="r", outcomes=[OutcomeName.ACCEPTED_OUTPUT, OutcomeName.GENERATION_SUCCESS]
    )
    accepted = [record for record in run.records if record.outcome is OutcomeName.ACCEPTED_OUTPUT]
    success = [record for record in run.records if record.outcome is OutcomeName.GENERATION_SUCCESS]
    assert accepted == []
    assert success and success[0].posterior_mean > 0.9


def test_a_failed_generation_cannot_carry_a_quality_score() -> None:
    with pytest.raises(ValueError, match="no artefact to judge"):
        ProductionObservation(
            observation_id="x",
            occurred_at=BASE,
            provider="wan",
            model_id="wan-2.7",
            exact_version="wan-2.7",
            task_type=TaskType.T2V,
            scenario=Scenario.MOTION,
            asset_criticality="STANDARD",
            generation_success=False,
            qc_identity_score=0.9,
        )


def test_an_alias_observation_is_quarantined_rather_than_attributed() -> None:
    observations = [_observation(i) for i in range(10)] + [_observation(99, alias=True)]
    usable, quarantined = quarantine_unversioned(observations)
    assert len(usable) == 10
    assert quarantined == [("obs-wan-00099", "MODEL_RECORDED_AS_ALIAS")]
    run = HierarchicalPosteriorEngine().compute(
        observations, run_id="r", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    assert run.quarantined == quarantined
    assert all(record.observation_count <= 10 for record in run.records)


def test_the_contamination_audit_passes_on_a_clean_run() -> None:
    observations = [
        _observation(i, task=TaskType.T2V if i % 2 else TaskType.I2V, scenario=Scenario.MOTION)
        for i in range(40)
    ]
    run = HierarchicalPosteriorEngine().compute(observations, run_id="r")
    assert audit_contamination(run) == []


def test_the_contamination_audit_notices_a_mislevelled_row() -> None:
    """The check that would catch a future refactor widening a group key."""

    observations = [_observation(i) for i in range(20)]
    run = HierarchicalPosteriorEngine().compute(
        observations, run_id="r", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    version_row = next(record for record in run.records if record.level is PosteriorLevel.VERSION)
    broken = version_row.key.model_copy(update={"task_type": TaskType.T2V})
    run.records.append(version_row.__class__(**{**version_row.__dict__, "key": broken}))
    findings = audit_contamination(run)
    assert any(item.kind == "LEVEL_KEY_MISMATCH" for item in findings)


def test_the_contamination_audit_rejects_a_non_production_scale() -> None:
    observations = [_observation(i) for i in range(20)]
    run = HierarchicalPosteriorEngine().compute(
        observations, run_id="r", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    row = run.scenario_records()[0]
    smuggled = row.key.model_copy(update={"metric_scale_id": "vbench2-total-0-1"})
    run.records.append(row.__class__(**{**row.__dict__, "key": smuggled}))
    findings = audit_contamination(run)
    assert any(item.kind == "NON_PRODUCTION_SCALE_IN_POSTERIOR" for item in findings)


def test_every_outcome_carries_its_own_production_scale() -> None:
    scale_ids = [scale.scale_id for scale in OUTCOME_SCALES.values()]
    assert len(scale_ids) == len(set(scale_ids))
    assert all(scale_id.startswith("prod.") for scale_id in scale_ids)


def test_a_bounded_continuous_outcome_reduces_to_the_same_code_path() -> None:
    observations = [_observation(i, identity=0.75) for i in range(60)]
    run = HierarchicalPosteriorEngine().compute(
        observations, run_id="r", outcomes=[OutcomeName.QC_IDENTITY]
    )
    record = run.scenario_records()[0]
    assert record.posterior_mean == pytest.approx(0.75, abs=0.02)
    assert record.observation_count == 60


def test_a_lower_is_better_outcome_is_inverted_onto_the_unit_axis() -> None:
    """``regenerated`` is bad news; a high rate must produce a low posterior."""

    observations = [
        _observation(i).model_copy(update={"regenerated": True}) for i in range(40)
    ]
    run = HierarchicalPosteriorEngine().compute(
        observations, run_id="r", outcomes=[OutcomeName.REGENERATED]
    )
    assert run.scenario_records()[0].posterior_mean < 0.1


def test_cost_and_latency_are_summarised_in_their_own_units() -> None:
    rng = random.Random(3)
    observations = [
        _observation(i, latency=rng.randint(10_000, 100_000), cost=44.0) for i in range(50)
    ]
    summaries = summarize_cost_and_latency(observations)
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.key.metric_scale_id == "prod.operational-units"
    assert summary.latency_ms_p90 is not None and summary.latency_ms_p90 >= summary.latency_ms_p50
    assert summary.cost_credits_total == pytest.approx(50 * 44.0)


def test_cost_and_latency_never_become_a_beta() -> None:
    assert OutcomeName.__members__.keys().isdisjoint({"LATENCY", "COST"})


def test_an_external_contribution_is_capped_and_attributed() -> None:
    """External priors enter as bounded pseudo-counts, and say who they were."""

    observations = [_observation(i, accepted=False) for i in range(30)]
    engine = HierarchicalPosteriorEngine(prior_version="benchmark-prior-v1")
    key_token = "|".join(
        ("wan", "wan-2.7", "wan-2.7", "T2V", "motion", OUTCOME_SCALES[OutcomeName.ACCEPTED_OUTPUT].scale_id)
    )
    contribution = ExternalPriorContribution(
        layer=EvidenceLayer.BENCHMARK,
        alpha=7.2,
        beta=0.8,
        record_count=4,
        source_version="benchmark-prior-v1",
    )
    run = engine.compute(
        observations,
        run_id="r",
        outcomes=[OutcomeName.ACCEPTED_OUTPUT],
        external_priors={(key_token, OutcomeName.ACCEPTED_OUTPUT): (contribution,)},
    )
    scenario = next(record for record in run.scenario_records())
    assert "benchmark_prior" in scenario.prior_sources
    assert scenario.prior_version == "benchmark-prior-v1"
    # Thirty zeros against eight pseudo-observations: production wins.
    assert scenario.posterior_mean < 0.3


def test_sufficiency_is_about_data_not_a_narrow_interval() -> None:
    thin = [_observation(i) for i in range(5)]
    run = HierarchicalPosteriorEngine().compute(
        thin, run_id="r", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    assert all(record.sufficient is False for record in run.scenario_records())


def test_the_condition_bucket_boundaries_are_where_they_claim() -> None:
    assert ConditionBucket.bucket_duration(None) == "n/a"
    assert ConditionBucket.bucket_duration(2.0) == "<=2s"
    assert ConditionBucket.bucket_duration(2.1) == "2-5s"
    assert ConditionBucket.bucket_duration(8.0) == "5-8s"
    assert ConditionBucket.bucket_duration(13.0) == ">12s"


def test_an_aggregate_sentinel_cannot_be_an_observation() -> None:
    with pytest.raises(ValueError, match="ANY aggregate slot"):
        ProductionObservation(
            observation_id="x",
            occurred_at=BASE,
            provider="wan",
            model_id="wan-2.7",
            exact_version="wan-2.7",
            task_type=TaskType.ANY,
            scenario=Scenario.MOTION,
            asset_criticality="STANDARD",
            generation_success=True,
        )
