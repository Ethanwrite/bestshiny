"""Historical replay: what the posterior router would have done, and what it cost.

Replay is the gate. Nothing in this work is allowed near production until a
replay over real history says the posterior policy would not have been worse
than the policy already running. This module is that check, and it is built to
be able to fail.

**How it works.** History is split in two by time. The posterior is fitted on
the earlier window only; the later window is the ground truth nobody was
allowed to see. For each context in the later window — a task, a scene, a set
of generation conditions — the two policies each name a model, and the realised
outcomes of the later window say how that model actually did.

**What it can and cannot know.** Only the model that actually ran has an
outcome for a given shot. Replay therefore compares policies on *context
buckets* rather than on individual shots: within one bucket, every model with
observations has an empirical mean, and a policy's score is the mean of the arm
it chose. A bucket where the chosen arm has no observations in the evaluation
window cannot be scored, and is reported as unscored rather than filled in with
a guess. ``unscored_contexts`` being large is a reason to distrust a replay
result, and the report prints it next to the headline.

This is the direct method, with the assumptions the direct method always has:
the observations in a bucket are treated as exchangeable, and a model that was
only ever chosen for the easy shots in a bucket will look better than it is.
The mitigation is the bucket definition — task, scene, duration, resolution and
reference mode — which is fine enough that "the easy shots" is a much smaller
category than it would be for the platform as a whole.

**Five reported quantities**, per the mandate: interval coverage, regret,
generation cost, failure rate and quality outcome. Each is reported for both
policies side by side, in its own units, and none of them is combined into a
single score.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .betamath import beta_quantile, moment_match
from .keys import ConditionBucket, Scenario, TaskType
from .lcb import CandidateModel, ConservativeLcbBuilder, LcbSettings, PosteriorLookup
from .observations import OUTCOME_SCALES, OutcomeName, ProductionObservation
from .posterior import HierarchicalPosteriorEngine, PosteriorRecord, PosteriorRun

#: A policy names a model key for a context, or ``None`` to abstain.
Policy = Callable[["ReplayContext", list[str]], str | None]


@dataclass(frozen=True)
class ReplayContext:
    task_type: TaskType
    scenario: Scenario
    conditions: ConditionBucket
    asset_criticality: str

    @property
    def token(self) -> str:
        return (
            f"{self.task_type.value}|{self.scenario.value}|"
            f"{self.conditions.token}|{self.asset_criticality}"
        )


@dataclass
class ArmOutcome:
    """The realised behaviour of one model in one context, in the eval window."""

    model_key: str
    exact_version: str
    observations: int = 0
    quality_mean: float | None = None
    failure_rate: float = 0.0
    cost_credits_mean: float = 0.0
    latency_ms_mean: float | None = None


@dataclass
class PolicyResult:
    name: str
    scored_contexts: int = 0
    abstentions: int = 0
    fell_back: int = 0
    total_regret: float = 0.0
    mean_regret: float | None = None
    quality_mean: float | None = None
    failure_rate: float | None = None
    cost_credits_mean: float | None = None
    chosen_models: dict[str, int] = field(default_factory=dict)

    def finalize(self, quality_values: list[float], failures: list[float], costs: list[float]) -> None:
        if self.scored_contexts:
            self.mean_regret = self.total_regret / self.scored_contexts
        if quality_values:
            self.quality_mean = sum(quality_values) / len(quality_values)
        if failures:
            self.failure_rate = sum(failures) / len(failures)
        if costs:
            self.cost_credits_mean = sum(costs) / len(costs)


#: Cells needed before a coverage figure means anything. Below this, three
#: cells landing outside their interval is ordinary luck rather than evidence
#: of overconfidence, and a gate that accepted such a number would be
#: measuring noise.
MIN_COVERAGE_CELLS = 8

#: How much more the posterior policy may spend per generation and still count
#: as "not worse". Buying quality with money is a product decision, not a
#: routing one, so replay refuses to make it silently.
DEFAULT_COST_TOLERANCE = 0.05

#: How far observed coverage may sit from nominal. Tighter would fail on
#: sampling noise alone at the cell counts this platform has; looser would
#: accept a posterior that is meaningfully overconfident.
COVERAGE_BAND = 0.10


@dataclass
class IntervalCoverage:
    """Did the fitted interval contain what actually happened?

    ``nominal`` is what the quantile levels promise — 0.80 for a 10th-to-90th
    interval. ``observed`` well below nominal means the posterior is
    overconfident and the LCB is not conservative enough; well above means it
    is so wide that it will never distinguish two models.
    """

    nominal: float
    observed: float | None = None
    cells_checked: int = 0
    cells_covered: int = 0
    cells_below: int = 0
    cells_above: int = 0

    @property
    def determinable(self) -> bool:
        return self.cells_checked >= MIN_COVERAGE_CELLS

    @property
    def calibrated(self) -> bool:
        """Whether coverage is close enough to nominal to act on.

        Undeterminable is not calibrated. A replay with four checkable cells
        has not shown the posterior to be well calibrated; it has shown that
        nobody can tell yet, and the conservative reading of "cannot tell" is
        "not yet".
        """

        if not self.determinable or self.observed is None:
            return False
        return abs(self.observed - self.nominal) <= COVERAGE_BAND


@dataclass
class ReplayResult:
    run_id: str
    fitted_through: datetime | None
    evaluated_from: datetime | None
    outcome: OutcomeName
    fit_observations: int
    eval_observations: int
    contexts: int
    unscored_contexts: int
    baseline: PolicyResult
    posterior: PolicyResult
    coverage: IntervalCoverage
    cost_tolerance: float = DEFAULT_COST_TOLERANCE
    notes: list[str] = field(default_factory=list)

    @property
    def posterior_is_not_worse(self) -> bool:
        """The gate condition, stated as the conservative "not worse", not "better".

        Three things must hold together, because a policy can buy quality with
        money or with reliability and the replay must not reward that:
        regret no higher than the baseline's, failure rate no higher, and mean
        cost no more than 5% above. A policy that improves quality while
        raising the failure rate has not passed.
        """

        if self.baseline.mean_regret is None or self.posterior.mean_regret is None:
            return False
        if self.posterior.mean_regret > self.baseline.mean_regret + 1e-9:
            return False
        base_failure = self.baseline.failure_rate or 0.0
        post_failure = self.posterior.failure_rate or 0.0
        if post_failure > base_failure + 1e-9:
            return False
        base_cost = self.baseline.cost_credits_mean or 0.0
        post_cost = self.posterior.cost_credits_mean or 0.0
        if base_cost and post_cost > base_cost * (1.0 + self.cost_tolerance):
            return False
        return True

    @property
    def passed(self) -> bool:
        """What the LCB feature flag is allowed to depend on.

        Coverage calibration is part of it. A policy that beats the baseline
        while its intervals are badly calibrated beat it by luck, and the next
        window is under no obligation to repeat that.
        """

        if not self.posterior_is_not_worse:
            return False
        if not self.coverage.calibrated:
            return False
        # More than half the contexts unscored means the comparison rested on
        # a minority of history, and the majority is not evidence of anything.
        return self.unscored_contexts * 2 <= self.contexts

    def failure_reasons(self) -> list[str]:
        """Why this replay did not pass, in the order they would be fixed.

        A boolean gate that cannot say why is a gate people route around.
        """

        reasons: list[str] = []
        if self.baseline.mean_regret is None or self.posterior.mean_regret is None:
            reasons.append("NO_SCORED_CONTEXTS")
        elif self.posterior.mean_regret > self.baseline.mean_regret + 1e-9:
            reasons.append(
                f"REGRET_WORSE baseline={self.baseline.mean_regret:.4f} "
                f"posterior={self.posterior.mean_regret:.4f}"
            )
        base_failure = self.baseline.failure_rate or 0.0
        post_failure = self.posterior.failure_rate or 0.0
        if post_failure > base_failure + 1e-9:
            reasons.append(f"FAILURE_RATE_WORSE {base_failure:.4f} -> {post_failure:.4f}")
        base_cost = self.baseline.cost_credits_mean or 0.0
        post_cost = self.posterior.cost_credits_mean or 0.0
        if base_cost and post_cost > base_cost * (1.0 + self.cost_tolerance):
            reasons.append(
                f"COST_ABOVE_TOLERANCE {base_cost:.2f} -> {post_cost:.2f} "
                f"(> +{self.cost_tolerance:.0%})"
            )
        if not self.coverage.determinable:
            reasons.append(
                f"COVERAGE_UNDETERMINED only {self.coverage.cells_checked} cells checked, "
                f"{MIN_COVERAGE_CELLS} needed"
            )
        elif not self.coverage.calibrated:
            reasons.append(
                f"COVERAGE_MISCALIBRATED observed={self.coverage.observed:.3f} "
                f"nominal={self.coverage.nominal:.3f}"
            )
        if self.unscored_contexts * 2 > self.contexts:
            reasons.append(f"TOO_MANY_UNSCORED {self.unscored_contexts}/{self.contexts}")
        return reasons


class ReplayHarness:
    """Split history, fit on the past, score on the future."""

    version = "router-replay-v1"

    def __init__(
        self,
        *,
        engine: HierarchicalPosteriorEngine | None = None,
        lcb_settings: LcbSettings | None = None,
        cost_tolerance: float = DEFAULT_COST_TOLERANCE,
    ):
        self.engine = engine or HierarchicalPosteriorEngine()
        self.cost_tolerance = cost_tolerance
        # Enabled here because a replay of a disabled policy would only ever
        # reproduce the baseline. The flag that matters is the one in
        # production, and it is separate from this.
        self.lcb_settings = lcb_settings or LcbSettings(enabled=True)

    @staticmethod
    def split(
        observations: Sequence[ProductionObservation], *, fit_fraction: float = 0.6
    ) -> tuple[list[ProductionObservation], list[ProductionObservation]]:
        """Chronological split. Never random.

        A random split leaks the future into the fit: models are added,
        prompts change, and a provider that degraded in week three would be
        judged partly on week four's data.
        """

        if not 0.0 < fit_fraction < 1.0:
            raise ValueError("fit_fraction must be strictly between 0 and 1")
        ordered = sorted(observations, key=lambda item: (item.occurred_at, item.observation_id))
        cut = int(len(ordered) * fit_fraction)
        return ordered[:cut], ordered[cut:]

    @staticmethod
    def _context(observation: ProductionObservation) -> ReplayContext:
        return ReplayContext(
            task_type=observation.task_type,
            scenario=observation.scenario,
            conditions=observation.conditions,
            asset_criticality=observation.asset_criticality,
        )

    @staticmethod
    def _arms(
        observations: Sequence[ProductionObservation], outcome: OutcomeName
    ) -> dict[str, ArmOutcome]:
        by_model: dict[str, list[ProductionObservation]] = defaultdict(list)
        for observation in observations:
            by_model[f"{observation.provider}:{observation.model_id}"].append(observation)
        arms: dict[str, ArmOutcome] = {}
        scale = OUTCOME_SCALES[outcome]
        for model_key, items in by_model.items():
            values = [
                scale.to_unit(value)
                for item in items
                if (value := item.outcome_value(outcome)) is not None
            ]
            failures = [1.0 if item.provider_failure else 0.0 for item in items]
            costs = [item.cost_credits for item in items if item.cost_credits is not None]
            latencies = [float(item.latency_ms) for item in items if item.latency_ms is not None]
            arms[model_key] = ArmOutcome(
                model_key=model_key,
                exact_version=items[0].exact_version,
                observations=len(items),
                quality_mean=(sum(values) / len(values)) if values else None,
                failure_rate=sum(failures) / len(failures),
                cost_credits_mean=(sum(costs) / len(costs)) if costs else 0.0,
                latency_ms_mean=(sum(latencies) / len(latencies)) if latencies else None,
            )
        return arms

    def run(
        self,
        observations: Sequence[ProductionObservation],
        *,
        run_id: str,
        baseline_policy: Policy,
        outcome: OutcomeName = OutcomeName.ACCEPTED_OUTPUT,
        fit_fraction: float = 0.6,
        versions_by_model_key: dict[str, str] | None = None,
    ) -> ReplayResult:
        fit, evaluate = self.split(observations, fit_fraction=fit_fraction)
        posterior_run = self.engine.compute(fit, run_id=f"{run_id}-fit", outcomes=[outcome])
        lookup = PosteriorLookup(posterior_run.records)
        builder = ConservativeLcbBuilder(lookup, self.lcb_settings)

        versions = dict(versions_by_model_key or {})
        for observation in fit:
            versions.setdefault(f"{observation.provider}:{observation.model_id}", observation.exact_version)

        grouped: dict[ReplayContext, list[ProductionObservation]] = defaultdict(list)
        for observation in evaluate:
            grouped[self._context(observation)].append(observation)

        baseline = PolicyResult(name="baseline")
        posterior = PolicyResult(name="posterior_lcb")
        baseline_quality: list[float] = []
        baseline_failures: list[float] = []
        baseline_costs: list[float] = []
        posterior_quality: list[float] = []
        posterior_failures: list[float] = []
        posterior_costs: list[float] = []
        unscored = 0
        notes: list[str] = []

        for context, items in sorted(grouped.items(), key=lambda entry: entry[0].token):
            arms = self._arms(items, outcome)
            scorable = {key: arm for key, arm in arms.items() if arm.quality_mean is not None}
            if len(scorable) < 2:
                # One arm is not a choice. Counted, not scored: reporting a
                # zero regret for a context with no alternative would make
                # every policy look perfect on a single-model platform.
                unscored += 1
                continue
            best = max(arm.quality_mean or 0.0 for arm in scorable.values())
            available = sorted(scorable)

            baseline_choice = baseline_policy(context, available)
            candidates = [
                CandidateModel(
                    provider=key.split(":", 1)[0],
                    model_id=key.split(":", 1)[1],
                    exact_version=versions.get(key, ""),
                )
                for key in available
            ]
            adjustments = builder.build(
                candidates,
                task_type=context.task_type,
                scenario=context.scenario,
                conditions=context.conditions,
            )
            posterior_choice = _argmax_lcb(adjustments, available)
            if posterior_choice is None:
                # The documented fallback: with no sufficient cell, the
                # posterior policy *is* the baseline policy. A replay that
                # hid this would overstate how often the new policy is used.
                posterior_choice = baseline_choice
                posterior.fell_back += 1

            for result, choice, quality, failures, costs in (
                (baseline, baseline_choice, baseline_quality, baseline_failures, baseline_costs),
                (posterior, posterior_choice, posterior_quality, posterior_failures, posterior_costs),
            ):
                if choice is None or choice not in scorable:
                    result.abstentions += 1
                    continue
                arm = scorable[choice]
                result.scored_contexts += 1
                result.total_regret += best - (arm.quality_mean or 0.0)
                result.chosen_models[choice] = result.chosen_models.get(choice, 0) + 1
                quality.append(arm.quality_mean or 0.0)
                failures.append(arm.failure_rate)
                costs.append(arm.cost_credits_mean)

        baseline.finalize(baseline_quality, baseline_failures, baseline_costs)
        posterior.finalize(posterior_quality, posterior_failures, posterior_costs)
        coverage = self._coverage(posterior_run, evaluate, outcome)
        if unscored:
            notes.append(
                f"{unscored} of {len(grouped)} contexts had fewer than two models with outcomes and "
                "could not be scored"
            )
        if posterior.fell_back:
            notes.append(
                f"the posterior policy fell back to the baseline in {posterior.fell_back} contexts "
                "for want of a sufficient cell"
            )

        return ReplayResult(
            run_id=run_id,
            fitted_through=fit[-1].occurred_at if fit else None,
            evaluated_from=evaluate[0].occurred_at if evaluate else None,
            outcome=outcome,
            fit_observations=len(fit),
            eval_observations=len(evaluate),
            contexts=len(grouped),
            unscored_contexts=unscored,
            baseline=baseline,
            posterior=posterior,
            coverage=coverage,
            cost_tolerance=self.cost_tolerance,
            notes=notes,
        )

    def _coverage(
        self,
        posterior_run: PosteriorRun,
        evaluation: Sequence[ProductionObservation],
        outcome: OutcomeName,
    ) -> IntervalCoverage:
        """Check each fitted interval against what the evaluation window realised.

        The interval checked is the **posterior predictive** one, not the
        posterior itself. The distinction matters and getting it wrong makes a
        well calibrated posterior look overconfident: the posterior interval
        describes uncertainty about the underlying rate, while the thing we can
        actually observe is a mean over a finite evaluation window, which
        carries its own sampling error on top. Comparing the second against the
        first is a category error — it asks the interval to contain something
        noisier than what it is an interval for.

        So the fitted Beta is widened by the evaluation window's own sampling
        variance and re-matched to a Beta before its quantiles are taken:

            predictive mean  = a / (a + b)
            predictive var   = Var(p) + E[p(1 - p)] / n_eval

        With a large evaluation window the second term vanishes and the
        predictive interval converges on the posterior interval, which is the
        behaviour you want.
        """

        scale = OUTCOME_SCALES[outcome]
        realised: dict[str, list[float]] = defaultdict(list)
        for observation in evaluation:
            value = observation.outcome_value(outcome)
            if value is None:
                continue
            realised[observation.key_for(outcome).token].append(scale.to_unit(value))

        coverage = IntervalCoverage(
            nominal=self.engine.upper_quantile - self.engine.lower_quantile,
        )
        for record in posterior_run.scenario_records():
            if record.outcome is not outcome:
                continue
            values = realised.get(record.key.token)
            # One realised observation is not a rate. Five is not many either,
            # but it is enough for the comparison to mean something.
            if not values or len(values) < 5:
                continue
            mean = sum(values) / len(values)
            lower, upper = self._predictive_interval(record, len(values))
            coverage.cells_checked += 1
            if mean < lower:
                coverage.cells_below += 1
            elif mean > upper:
                coverage.cells_above += 1
            else:
                coverage.cells_covered += 1
        if coverage.cells_checked:
            coverage.observed = coverage.cells_covered / coverage.cells_checked
        return coverage

    def _predictive_interval(self, record: PosteriorRecord, eval_count: int) -> tuple[float, float]:
        alpha, beta = record.alpha, record.beta
        total = alpha + beta
        rate_variance = (alpha * beta) / (total * total * (total + 1.0))
        binomial_variance = (alpha * beta) / (total * (total + 1.0)) / max(1, eval_count)
        mean = alpha / total
        maximum = mean * (1.0 - mean)
        variance = min(rate_variance + binomial_variance, maximum * 0.999999)
        if variance <= 0 or not 0.0 < mean < 1.0:  # pragma: no cover - degenerate cell
            return record.posterior_lower_quantile, record.posterior_upper_quantile
        predictive_alpha, predictive_beta = moment_match(mean, variance)
        return (
            beta_quantile(self.engine.lower_quantile, predictive_alpha, predictive_beta),
            beta_quantile(self.engine.upper_quantile, predictive_alpha, predictive_beta),
        )


def _argmax_lcb(adjustments, available: list[str]) -> str | None:  # type: ignore[no-untyped-def]
    """The model with the highest lower bound, or ``None`` if nothing applied.

    Ties break on the model key so a replay is reproducible; a tie between two
    lower bounds is a genuine tie and picking the alphabetically first is as
    defensible as any other rule, provided it is stable.
    """

    best_key: str | None = None
    best_score = float("-inf")
    for model_key in sorted(available):
        dimensions = adjustments.adjustments.get(model_key)
        if not dimensions:
            continue
        # The weakest dimension, not the average of them: averaging across
        # dimensions is averaging across metrics, and a model that is superb at
        # three things and unusable at a fourth should not be described as
        # good.
        score = min(dimensions.values())
        if score > best_score:
            best_score, best_key = score, model_key
    return best_key


def fixed_order_policy(order: Sequence[str]) -> Policy:
    """A baseline that always prefers the first available model in a fixed order.

    Stands in for "the router as it is today" in tests and simulations, where
    the real router's inputs are not available. The real replay is driven by
    the actual capability priors — see ``scripts/router_replay.py``.
    """

    def policy(_context: ReplayContext, available: list[str]) -> str | None:
        for model_key in order:
            if model_key in available:
                return model_key
        return available[0] if available else None

    return policy


__all__ = [
    "COVERAGE_BAND",
    "DEFAULT_COST_TOLERANCE",
    "MIN_COVERAGE_CELLS",
    "ArmOutcome",
    "IntervalCoverage",
    "Policy",
    "PolicyResult",
    "ReplayContext",
    "ReplayHarness",
    "ReplayResult",
    "fixed_order_policy",
]
