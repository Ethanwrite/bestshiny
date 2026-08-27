"""Exploration: the architecture, the constraints, and no way to switch it on.

Exploration is how a router discovers that a model it rarely picks is actually
good. It is also how a router spends a user's money on a shot they cared about
to satisfy its own curiosity, which is why this module ships as a design and a
simulator rather than as a behaviour.

There is no feature flag here on purpose. A flag is a thing someone can turn
on; the absence of any call site is a thing they cannot. Nothing in
``services/`` or ``apps/`` imports this module, and
``test_router_exploration_offline.py`` asserts that it stays that way. Bringing
it online is a deliberate future change with its own review, not a
configuration edit.

**The six constraints**, all of which must pass before a candidate may be
explored at all:

* *budget* — a spend ceiling for the window, in credits, that exploration may
  not exceed even by one clip;
* *criticality* — a ceiling on how important the shot may be; canonical and
  hero shots are never experiments;
* *cost* — a per-generation cost cap, so exploration cannot pick the most
  expensive model in the registry as its first experiment;
* *minimum evidence* — a model with no evidence at all is not a promising arm,
  it is an unknown, and exploring it is indistinguishable from routing at
  random;
* *failure ceiling* — a model whose observed provider-failure rate is above the
  ceiling is excluded regardless of how good its quality posterior looks;
* *eligibility* — an explicit allowlist of exact versions. Not model ids:
  versions, because a silent snapshot change must not inherit permission.

The bonus itself is the standard optimistic one — the *upper* quantile, the
mirror of what ``lcb.py`` does — so the two can be compared honestly in
simulation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .keys import ConditionBucket, Scenario, TaskType
from .lcb import CandidateModel, PosteriorLookup
from .observations import OUTCOME_SCALES, OutcomeName
from .posterior import PosteriorRecord

#: Criticalities that may never be used for exploration, whatever the budget
#: says. Mirrors the platform's own ordering; kept as strings so this module
#: does not depend on the provider SDK.
NEVER_EXPLORE_CRITICALITY: frozenset[str] = frozenset({"CANONICAL", "HERO", "IMPORTANT"})


@dataclass(frozen=True)
class ExplorationConstraints:
    """Every limit exploration must satisfy, in one object that can be reviewed.

    ``enabled`` exists so a simulation can be run at all. It is not read by any
    production code path, because no production code path imports this module.
    """

    enabled: bool = False
    budget_credits: Decimal = Decimal("0")
    max_generation_cost_credits: Decimal = Decimal("0")
    max_asset_criticality: str = "STANDARD"
    min_observations: int = 5
    max_failure_rate: float = 0.15
    eligible_exact_versions: frozenset[str] = frozenset()
    #: How much optimism to allow, as a quantile. 0.90 mirrors the LCB's 0.10.
    optimism_quantile: float = 0.90
    #: Share of eligible requests that may be explored at all.
    exploration_rate: float = 0.05


@dataclass
class ExplorationVerdict:
    candidate: CandidateModel
    allowed: bool
    reasons: tuple[str, ...]
    optimistic_score: float | None = None
    posterior_mean: float | None = None
    observation_count: int = 0
    estimated_cost_credits: Decimal = Decimal("0")


@dataclass
class ExplorationSimulation:
    """What an offline run of the policy would have done. Never what it did."""

    considered: int = 0
    allowed: int = 0
    refused: int = 0
    refusals_by_reason: dict[str, int] = field(default_factory=dict)
    budget_spent: Decimal = Decimal("0")
    verdicts: list[ExplorationVerdict] = field(default_factory=list)
    online: bool = False


class ExplorationPolicy:
    """Decide, offline, whether a candidate could be explored.

    Every method is a pure function of its arguments and the constraints. There
    is no state that could drift between a simulation and a hypothetical
    deployment, and no way to reach a provider from here.
    """

    version = "router-exploration-v1-offline"

    def __init__(
        self,
        lookup: PosteriorLookup,
        constraints: ExplorationConstraints | None = None,
        *,
        failure_rates: dict[str, float] | None = None,
    ):
        self.lookup = lookup
        self.constraints = constraints or ExplorationConstraints()
        self.failure_rates = dict(failure_rates or {})
        self._spent = Decimal("0")

    @property
    def budget_remaining(self) -> Decimal:
        return max(Decimal("0"), self.constraints.budget_credits - self._spent)

    def _record_for(
        self,
        candidate: CandidateModel,
        task_type: TaskType,
        scenario: Scenario,
        conditions: ConditionBucket | None,
        outcome: OutcomeName = OutcomeName.ACCEPTED_OUTPUT,
    ) -> PosteriorRecord | None:
        token = "|".join(
            (
                candidate.provider,
                candidate.model_id,
                candidate.exact_version,
                task_type.value,
                scenario.value,
                OUTCOME_SCALES[outcome].scale_id,
            )
        )
        if conditions is not None:
            narrow = self.lookup.condition(token, conditions, outcome)
            if narrow is not None:
                return narrow
        return self.lookup.scenario(token, outcome)

    def evaluate(
        self,
        candidate: CandidateModel,
        *,
        task_type: TaskType,
        scenario: Scenario,
        asset_criticality: str,
        estimated_cost_credits: Decimal,
        conditions: ConditionBucket | None = None,
    ) -> ExplorationVerdict:
        """Check all six constraints and report every failure, not just the first.

        Reporting all of them matters for the simulation: a candidate that is
        refused for four reasons will not become explorable by fixing one, and
        a report that names only the first invites exactly that mistake.
        """

        reasons: list[str] = []
        if not self.constraints.enabled:
            reasons.append("EXPLORATION_DISABLED")
        if asset_criticality.upper() in NEVER_EXPLORE_CRITICALITY:
            reasons.append(f"CRITICALITY_{asset_criticality.upper()}_NEVER_EXPLORED")
        if candidate.exact_version not in self.constraints.eligible_exact_versions:
            reasons.append("VERSION_NOT_ELIGIBLE")
        if estimated_cost_credits > self.constraints.max_generation_cost_credits:
            reasons.append("GENERATION_COST_ABOVE_CAP")
        if estimated_cost_credits > self.budget_remaining:
            reasons.append("BUDGET_EXHAUSTED")

        failure_rate = self.failure_rates.get(f"{candidate.provider}:{candidate.model_id}")
        if failure_rate is not None and failure_rate > self.constraints.max_failure_rate:
            reasons.append(f"FAILURE_RATE_{failure_rate:.3f}_ABOVE_CEILING")

        record = self._record_for(candidate, task_type, scenario, conditions)
        if record is None:
            reasons.append("NO_EVIDENCE_AT_ALL")
        elif record.observation_count < self.constraints.min_observations:
            reasons.append(f"BELOW_MIN_EVIDENCE_{record.observation_count}")

        optimistic = record.posterior_upper_quantile if record is not None else None
        return ExplorationVerdict(
            candidate=candidate,
            allowed=not reasons,
            reasons=tuple(reasons),
            optimistic_score=optimistic,
            posterior_mean=record.posterior_mean if record is not None else None,
            observation_count=record.observation_count if record is not None else 0,
            estimated_cost_credits=estimated_cost_credits,
        )

    def simulate(
        self,
        requests: list[
            tuple[CandidateModel, TaskType, Scenario, str, Decimal, ConditionBucket | None]
        ],
    ) -> ExplorationSimulation:
        """Run the policy over a list of hypothetical requests.

        ``online`` is hard-coded ``False`` in the result. It is a field rather
        than an omission so that any report generated from a simulation says,
        in its own data, that it did not happen.
        """

        simulation = ExplorationSimulation(online=False)
        for candidate, task_type, scenario, criticality, cost, conditions in requests:
            simulation.considered += 1
            verdict = self.evaluate(
                candidate,
                task_type=task_type,
                scenario=scenario,
                asset_criticality=criticality,
                estimated_cost_credits=cost,
                conditions=conditions,
            )
            simulation.verdicts.append(verdict)
            if verdict.allowed:
                simulation.allowed += 1
                self._spent += cost
                simulation.budget_spent += cost
            else:
                simulation.refused += 1
                for reason in verdict.reasons:
                    simulation.refusals_by_reason[reason] = (
                        simulation.refusals_by_reason.get(reason, 0) + 1
                    )
        return simulation


__all__ = [
    "NEVER_EXPLORE_CRITICALITY",
    "ExplorationConstraints",
    "ExplorationPolicy",
    "ExplorationSimulation",
    "ExplorationVerdict",
]
