"""Historical replay — including the cases where it has to fail."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from router_evidence_core import (
    HierarchicalPosteriorEngine,
    OutcomeName,
    ProductionObservation,
    ReferenceMode,
    ReplayHarness,
    Scenario,
    TaskType,
    fixed_order_policy,
)
from router_evidence_core.replay import MIN_COVERAGE_CELLS

BASE = datetime(2026, 6, 1, tzinfo=UTC)
GOOD = ("wan", "wan-2.7", "wan-2.7")
POOR = ("openrouter", "google/veo-3.1-fast", "veo-3.1-fast")


def _history(
    *,
    scenarios: tuple[Scenario, ...] = (Scenario.MOTION,),
    total: int = 800,
    good_rate: float = 0.82,
    poor_rate: float = 0.55,
    good_cost: float = 40.0,
    poor_cost: float = 40.0,
    good_failure: float = 0.02,
    poor_failure: float = 0.02,
    seed: int = 11,
) -> list[ProductionObservation]:
    rng = random.Random(seed)
    observations: list[ProductionObservation] = []
    for index in range(total):
        provider, model_id, version = GOOD if index % 2 == 0 else POOR
        rate = good_rate if index % 2 == 0 else poor_rate
        cost = good_cost if index % 2 == 0 else poor_cost
        failure_rate = good_failure if index % 2 == 0 else poor_failure
        scenario = scenarios[index % len(scenarios)]
        success = rng.random() >= failure_rate
        observations.append(
            ProductionObservation(
                observation_id=f"obs-{index:05d}",
                occurred_at=BASE + timedelta(minutes=index),
                provider=provider,
                model_id=model_id,
                exact_version=version,
                task_type=TaskType.T2V,
                scenario=scenario,
                asset_criticality="STANDARD",
                reference_mode=ReferenceMode.NONE,
                duration_seconds=5.0,
                resolution="720P",
                generation_success=success,
                provider_failure=None if success else "PROVIDER_TIMEOUT",
                latency_ms=rng.randint(20_000, 80_000),
                cost_credits=cost,
                accepted_output=(rng.random() < rate) if success else None,
            )
        )
    return observations


def test_the_split_is_chronological_and_never_random() -> None:
    observations = _history(total=100)
    fit, evaluate = ReplayHarness.split(observations, fit_fraction=0.6)
    assert len(fit) == 60 and len(evaluate) == 40
    assert max(item.occurred_at for item in fit) <= min(item.occurred_at for item in evaluate)
    again = ReplayHarness.split(list(reversed(observations)), fit_fraction=0.6)
    assert [item.observation_id for item in again[0]] == [item.observation_id for item in fit]


def test_the_posterior_policy_finds_the_better_arm() -> None:
    result = ReplayHarness().run(
        _history(),
        run_id="r",
        baseline_policy=fixed_order_policy([f"{POOR[0]}:{POOR[1]}"]),
    )
    assert result.posterior.chosen_models == {f"{GOOD[0]}:{GOOD[1]}": result.posterior.scored_contexts}
    assert result.posterior.mean_regret == pytest.approx(0.0, abs=1e-9)
    assert result.baseline.mean_regret > result.posterior.mean_regret


def test_a_more_expensive_win_does_not_pass_the_gate() -> None:
    """Buying quality with money is a product decision, so replay refuses to make it."""

    result = ReplayHarness().run(
        _history(good_cost=80.0, poor_cost=40.0),
        run_id="r",
        baseline_policy=fixed_order_policy([f"{POOR[0]}:{POOR[1]}"]),
    )
    assert result.posterior_is_not_worse is False
    assert any(reason.startswith("COST_ABOVE_TOLERANCE") for reason in result.failure_reasons())


def test_a_quality_win_bought_with_reliability_does_not_pass_either() -> None:
    result = ReplayHarness().run(
        _history(good_failure=0.25, poor_failure=0.01),
        run_id="r",
        baseline_policy=fixed_order_policy([f"{POOR[0]}:{POOR[1]}"]),
    )
    assert any(reason.startswith("FAILURE_RATE_WORSE") for reason in result.failure_reasons())


def test_coverage_below_the_minimum_cell_count_is_undetermined_not_calibrated() -> None:
    result = ReplayHarness().run(
        _history(total=200), run_id="r", baseline_policy=fixed_order_policy([f"{POOR[0]}:{POOR[1]}"])
    )
    assert result.coverage.cells_checked < MIN_COVERAGE_CELLS
    assert result.coverage.determinable is False
    assert result.coverage.calibrated is False
    assert result.passed is False
    assert any(reason.startswith("COVERAGE_UNDETERMINED") for reason in result.failure_reasons())


def test_a_context_with_one_arm_is_unscored_rather_than_scored_perfect() -> None:
    single = [
        item
        for item in _history(total=200)
        if (item.provider, item.model_id) == (GOOD[0], GOOD[1])
    ]
    result = ReplayHarness().run(
        single, run_id="r", baseline_policy=fixed_order_policy([f"{GOOD[0]}:{GOOD[1]}"])
    )
    assert result.unscored_contexts == result.contexts
    assert result.baseline.mean_regret is None
    assert result.passed is False


def test_falling_back_to_the_baseline_is_counted_not_hidden() -> None:
    """With too little history the posterior policy *is* the baseline, and says so."""

    result = ReplayHarness().run(
        _history(total=60, scenarios=(Scenario.MOTION, Scenario.PHYSICS, Scenario.IDENTITY)),
        run_id="r",
        baseline_policy=fixed_order_policy([f"{POOR[0]}:{POOR[1]}"]),
    )
    assert result.posterior.fell_back > 0
    assert any("fell back" in note for note in result.notes)


def test_scenarios_are_replayed_separately() -> None:
    observations = _history(
        total=1200, scenarios=(Scenario.MOTION, Scenario.PHYSICS, Scenario.IDENTITY)
    )
    result = ReplayHarness().run(
        observations, run_id="r", baseline_policy=fixed_order_policy([f"{POOR[0]}:{POOR[1]}"])
    )
    assert result.contexts >= 3


def test_the_replay_is_deterministic() -> None:
    observations = _history()
    first = ReplayHarness().run(
        observations, run_id="r", baseline_policy=fixed_order_policy([f"{POOR[0]}:{POOR[1]}"])
    )
    second = ReplayHarness().run(
        list(reversed(observations)),
        run_id="r",
        baseline_policy=fixed_order_policy([f"{POOR[0]}:{POOR[1]}"]),
    )
    assert first.posterior.mean_regret == second.posterior.mean_regret
    assert first.coverage.cells_checked == second.coverage.cells_checked


def test_a_well_calibrated_posterior_covers_close_to_its_nominal_rate() -> None:
    """Many small cells, generated from a known rate, should mostly be covered."""

    rng = random.Random(5)
    observations: list[ProductionObservation] = []
    scenarios = [scenario for scenario in Scenario if scenario is not Scenario.ANY][:10]
    # Interleaved, not blocked by scenario: a chronological split of blocked
    # data puts whole scenarios on one side of the cut, and then no cell has
    # both a fitted interval and a realised rate to check it against.
    for index in range(1800):
        scenario = scenarios[index % len(scenarios)]
        provider, model_id, version = GOOD if (index // len(scenarios)) % 2 == 0 else POOR
        observations.append(
            ProductionObservation(
                observation_id=f"obs-{index:06d}",
                occurred_at=BASE + timedelta(minutes=index),
                provider=provider,
                model_id=model_id,
                exact_version=version,
                task_type=TaskType.T2V,
                scenario=scenario,
                asset_criticality="STANDARD",
                duration_seconds=5.0,
                resolution="720P",
                generation_success=True,
                cost_credits=40.0,
                accepted_output=rng.random() < (0.78 if provider == GOOD[0] else 0.55),
            )
        )
    result = ReplayHarness().run(
        observations, run_id="r", baseline_policy=fixed_order_policy([f"{POOR[0]}:{POOR[1]}"])
    )
    assert result.coverage.determinable is True
    # Nominal is 0.80; the predictive check should land near it rather than
    # well below, which is what a posterior-only check produces.
    assert result.coverage.observed is not None
    assert abs(result.coverage.observed - result.coverage.nominal) <= 0.15


def test_the_harness_fits_only_on_the_past() -> None:
    """A leak would let the policy be judged on data it was given."""

    observations = _history(total=400)
    harness = ReplayHarness()
    fit, evaluate = harness.split(observations, fit_fraction=0.6)
    run = HierarchicalPosteriorEngine().compute(
        fit, run_id="x", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    assert run.observation_count == len(fit)
    assert run.observation_count < len(observations)
    assert len(evaluate) == len(observations) - len(fit)


def test_an_impossible_fit_fraction_is_refused() -> None:
    with pytest.raises(ValueError, match="strictly between"):
        ReplayHarness.split(_history(total=10), fit_fraction=1.0)
