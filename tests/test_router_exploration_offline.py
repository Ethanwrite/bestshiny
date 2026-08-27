"""Exploration exists as a design and a simulator, and has no way to reach production."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from router_evidence_core import (
    CandidateModel,
    ConditionBucket,
    ExplorationConstraints,
    ExplorationPolicy,
    HierarchicalPosteriorEngine,
    OutcomeName,
    PosteriorLookup,
    ProductionObservation,
    ReferenceMode,
    Scenario,
    TaskType,
)

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2026, 7, 1, tzinfo=UTC)
CANDIDATE = CandidateModel(provider="wan", model_id="wan-2.7", exact_version="wan-2.7")
CONDITIONS = ConditionBucket(
    duration_bucket="2-5s", resolution="720P", reference_mode=ReferenceMode.NONE
)


def _lookup(count: int = 60) -> PosteriorLookup:
    observations = [
        ProductionObservation(
            observation_id=f"obs-{index:05d}",
            occurred_at=BASE + timedelta(minutes=index),
            provider=CANDIDATE.provider,
            model_id=CANDIDATE.model_id,
            exact_version=CANDIDATE.exact_version,
            task_type=TaskType.T2V,
            scenario=Scenario.MOTION,
            asset_criticality="STANDARD",
            duration_seconds=5.0,
            resolution="720P",
            generation_success=True,
            accepted_output=index % 4 != 0,
        )
        for index in range(count)
    ]
    run = HierarchicalPosteriorEngine().compute(
        observations, run_id="x", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    return PosteriorLookup(run.records)


def _permissive() -> ExplorationConstraints:
    return ExplorationConstraints(
        enabled=True,
        budget_credits=Decimal("500"),
        max_generation_cost_credits=Decimal("50"),
        min_observations=5,
        max_failure_rate=0.15,
        eligible_exact_versions=frozenset({"wan-2.7"}),
    )


def _evaluate(policy: ExplorationPolicy, **overrides: object):  # type: ignore[no-untyped-def]
    arguments: dict[str, object] = {
        "task_type": TaskType.T2V,
        "scenario": Scenario.MOTION,
        "asset_criticality": "STANDARD",
        "estimated_cost_credits": Decimal("44"),
        "conditions": CONDITIONS,
    }
    arguments.update(overrides)
    return policy.evaluate(CANDIDATE, **arguments)  # type: ignore[arg-type]


def test_no_service_or_app_imports_the_exploration_module() -> None:
    """The strongest guarantee available: there is no call site to switch on."""

    importers: list[str] = []
    for directory in ("services", "apps", "agents", "providers"):
        for path in (ROOT / directory).rglob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("exploration"):
                    importers.append(str(path.relative_to(ROOT)))
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.endswith("exploration"):
                            importers.append(str(path.relative_to(ROOT)))
    assert importers == []


def test_the_names_it_exports_appear_in_no_service_or_app() -> None:
    forbidden = ("ExplorationPolicy", "ExplorationConstraints", "ExplorationSimulation")
    hits: list[str] = []
    for directory in ("services", "apps", "agents", "providers", "core"):
        for path in (ROOT / directory).rglob("*.py"):
            if path.name in {"exploration.py", "__init__.py"}:
                continue
            text = path.read_text("utf-8")
            hits.extend(f"{path.relative_to(ROOT)}:{name}" for name in forbidden if name in text)
    assert hits == []


def test_the_default_constraints_refuse_everything() -> None:
    policy = ExplorationPolicy(_lookup())
    verdict = _evaluate(policy)
    assert verdict.allowed is False
    assert "EXPLORATION_DISABLED" in verdict.reasons


def test_a_canonical_shot_is_never_explored_however_much_budget_there_is() -> None:
    policy = ExplorationPolicy(_lookup(), _permissive())
    for criticality in ("CANONICAL", "HERO", "IMPORTANT"):
        verdict = _evaluate(policy, asset_criticality=criticality)
        assert verdict.allowed is False
        assert any(reason.startswith(f"CRITICALITY_{criticality}") for reason in verdict.reasons)


def test_eligibility_is_by_exact_version_not_by_model_id() -> None:
    """A silent snapshot change must not inherit permission."""

    policy = ExplorationPolicy(_lookup(), _permissive())
    other = CandidateModel(provider="wan", model_id="wan-2.7", exact_version="wan-2.7-preview")
    verdict = policy.evaluate(
        other,
        task_type=TaskType.T2V,
        scenario=Scenario.MOTION,
        asset_criticality="STANDARD",
        estimated_cost_credits=Decimal("44"),
        conditions=CONDITIONS,
    )
    assert "VERSION_NOT_ELIGIBLE" in verdict.reasons


def test_the_cost_cap_and_the_budget_are_separate_limits() -> None:
    policy = ExplorationPolicy(
        _lookup(),
        ExplorationConstraints(
            enabled=True,
            budget_credits=Decimal("500"),
            max_generation_cost_credits=Decimal("10"),
            eligible_exact_versions=frozenset({"wan-2.7"}),
        ),
    )
    assert "GENERATION_COST_ABOVE_CAP" in _evaluate(policy).reasons

    tight = ExplorationPolicy(
        _lookup(),
        ExplorationConstraints(
            enabled=True,
            budget_credits=Decimal("10"),
            max_generation_cost_credits=Decimal("100"),
            eligible_exact_versions=frozenset({"wan-2.7"}),
        ),
    )
    assert "BUDGET_EXHAUSTED" in _evaluate(tight).reasons


def test_a_model_with_no_evidence_is_not_a_promising_arm() -> None:
    policy = ExplorationPolicy(PosteriorLookup([]), _permissive())
    assert "NO_EVIDENCE_AT_ALL" in _evaluate(policy).reasons


def test_a_model_below_the_minimum_evidence_is_refused_with_its_count() -> None:
    policy = ExplorationPolicy(
        _lookup(count=6),
        ExplorationConstraints(
            enabled=True,
            budget_credits=Decimal("500"),
            max_generation_cost_credits=Decimal("50"),
            min_observations=50,
            eligible_exact_versions=frozenset({"wan-2.7"}),
        ),
    )
    verdict = _evaluate(policy)
    assert any(reason.startswith("BELOW_MIN_EVIDENCE_") for reason in verdict.reasons)


def test_a_failure_rate_above_the_ceiling_excludes_a_good_looking_model() -> None:
    policy = ExplorationPolicy(_lookup(), _permissive(), failure_rates={"wan:wan-2.7": 0.4})
    verdict = _evaluate(policy)
    assert any(reason.startswith("FAILURE_RATE_") for reason in verdict.reasons)


def test_every_failing_constraint_is_reported_not_just_the_first() -> None:
    policy = ExplorationPolicy(PosteriorLookup([]), ExplorationConstraints(), failure_rates={})
    verdict = _evaluate(policy, asset_criticality="CANONICAL", estimated_cost_credits=Decimal("999"))
    assert len(verdict.reasons) >= 4


def test_a_fully_satisfied_candidate_is_allowed_and_carries_the_upper_bound() -> None:
    policy = ExplorationPolicy(_lookup(), _permissive())
    verdict = _evaluate(policy)
    assert verdict.allowed is True
    assert verdict.optimistic_score is not None
    assert verdict.posterior_mean is not None
    # Optimism is the mirror of the LCB: the upper edge, not the lower one.
    assert verdict.optimistic_score > verdict.posterior_mean


def test_a_simulation_spends_only_simulated_budget_and_says_it_was_offline() -> None:
    policy = ExplorationPolicy(_lookup(), _permissive())
    requests = [
        (CANDIDATE, TaskType.T2V, Scenario.MOTION, "STANDARD", Decimal("44"), CONDITIONS)
        for _ in range(20)
    ]
    simulation = policy.simulate(requests)
    assert simulation.online is False
    assert simulation.considered == 20
    # 500 credits at 44 each: eleven fit, the rest hit the budget.
    assert simulation.allowed == 11
    assert simulation.budget_spent == Decimal("484")
    assert simulation.refusals_by_reason["BUDGET_EXHAUSTED"] == 9


def test_two_simulations_on_one_policy_do_not_share_a_budget() -> None:
    """A simulator that spends real state is not reproducible."""

    policy = ExplorationPolicy(_lookup(), _permissive())
    requests = [
        (CANDIDATE, TaskType.T2V, Scenario.MOTION, "STANDARD", Decimal("44"), CONDITIONS)
        for _ in range(20)
    ]
    first = policy.simulate(requests)
    second = policy.simulate(requests)
    assert first.allowed == second.allowed == 11
    assert first.budget_spent == second.budget_spent == Decimal("484")
    # And the policy is left as it was found.
    assert policy.budget_remaining == Decimal("500")
