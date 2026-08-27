"""The offline hierarchical posterior.

One Beta posterior per (provider, model_id, exact_version, task, scenario,
condition bucket, outcome). Nothing is computed online and nothing here can
reach the live router: this module reads observations, returns numbers, and has
no dependency on the routing path at all.

**The hierarchy.** A model that has run four times on dialogue and two hundred
times overall should not be described by four observations, and should not be
described by the two hundred either. Partial pooling is the standard answer:
each level is estimated from its own data, then used as a *prior* for the level
below it, with a bounded strength. A scenario with plenty of data barely moves;
a scenario with three observations sits close to its task's behaviour and says
so through a wide interval.

    L0  GLOBAL      a fixed, weakly-informative prior — not learned
    L1  VERSION     this exact snapshot, all tasks and scenes
    L2  TASK        this snapshot on this task type
    L3  SCENARIO    this snapshot, this task, this scene
    L4  CONDITION   ...at this duration bucket, resolution and reference mode

**Where the walls are.** L1 is the highest level that touches a model, and
there is no level above it that mixes models. That is the mechanical guarantee
against score inheritance across versions: Veo 3.1's data cannot shrink Veo
3.1 Fast's posterior, because there is no shared parent for them to meet in.
L0 is a *constant*, not an estimate over other models, precisely so that it
cannot become a back channel between versions.

Scales never meet either. Each outcome carries its own ``metric_scale_id`` and
a posterior is only ever built from values sharing it — checked, not assumed,
in :meth:`HierarchicalPosteriorEngine._collect`.

**Pooling is not contamination, and is switchable.** Shrinking a sparse
scenario towards its own model's task-level behaviour is a modelled
relationship between two things that really are related. Somebody may
reasonably want none of it; ``strict_isolation=True`` sets every pooling
strength to zero and gives each cell nothing but its own data and the fixed
global prior.

**Cost and latency are not here.** They are unbounded and a Beta over them
would be meaningless. :class:`CostLatencySummary` reports them in their own
units, with percentiles rather than a distribution.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from .betamath import beta_mean, beta_quantile
from .calibration import may_pool
from .keys import ConditionBucket, EvidenceKey, MetricScale, Scenario, TaskType
from .layers import EvidenceLayer
from .observations import OUTCOME_SCALES, OutcomeName, ProductionObservation


class PosteriorLevel(StrEnum):
    GLOBAL = "GLOBAL"
    VERSION = "VERSION"
    TASK = "VERSION_TASK"
    SCENARIO = "VERSION_TASK_SCENARIO"
    CONDITION = "VERSION_TASK_SCENARIO_CONDITION"


#: Pseudo-observations a parent level contributes to its child. Small on
#: purpose: at kappa=6 a cell with twenty of its own observations is roughly
#: three-quarters its own data, and a cell with two is mostly its parent — and
#: reports an interval wide enough to say so.
DEFAULT_POOLING_KAPPA: dict[PosteriorLevel, float] = {
    PosteriorLevel.VERSION: 4.0,
    PosteriorLevel.TASK: 6.0,
    PosteriorLevel.SCENARIO: 6.0,
    PosteriorLevel.CONDITION: 6.0,
}

#: The fixed L0 prior, per outcome. Jeffreys (0.5, 0.5) for the binary
#: outcomes: it is the standard non-informative choice for a proportion and,
#: unlike Beta(1,1), it does not pull a genuinely near-perfect success rate
#: down towards a half. The bounded continuous outcomes get Beta(1,1), which is
#: flat over the whole range they can take.
DEFAULT_GLOBAL_PRIOR: dict[OutcomeName, tuple[float, float]] = {
    OutcomeName.GENERATION_SUCCESS: (0.5, 0.5),
    OutcomeName.PROVIDER_FAILURE: (0.5, 0.5),
    OutcomeName.ACCEPTED_OUTPUT: (0.5, 0.5),
    OutcomeName.REGENERATED: (0.5, 0.5),
    OutcomeName.SWITCHED_MODEL: (0.5, 0.5),
    OutcomeName.DOWNLOADED: (0.5, 0.5),
    OutcomeName.USED_IN_NEXT_SHOT: (0.5, 0.5),
    OutcomeName.USER_PREFERENCE_AB: (0.5, 0.5),
    OutcomeName.USER_RATING: (1.0, 1.0),
    OutcomeName.QC_IDENTITY: (1.0, 1.0),
    OutcomeName.QC_MOTION: (1.0, 1.0),
    OutcomeName.QC_PROMPT_ALIGNMENT: (1.0, 1.0),
    OutcomeName.QC_TEMPORAL_CONSISTENCY: (1.0, 1.0),
}

DEFAULT_LOWER_QUANTILE = 0.10
DEFAULT_UPPER_QUANTILE = 0.90


@dataclass(frozen=True)
class ExternalPriorContribution:
    """What one external layer contributes to one cell, in pseudo-counts.

    Kept as a record rather than folded straight into alpha/beta so that a
    posterior can always answer "which of you moved this, and by how much".
    """

    layer: EvidenceLayer
    alpha: float
    beta: float
    record_count: int
    source_version: str
    eligible_record_ids: tuple[str, ...] = ()

    @property
    def pseudo_count(self) -> float:
        return self.alpha + self.beta


@dataclass(frozen=True)
class PosteriorRecord:
    """One saved posterior. The shape the mandate asks to be stored."""

    key: EvidenceKey
    outcome: OutcomeName
    level: PosteriorLevel
    condition: ConditionBucket | None
    posterior_mean: float
    posterior_lower_quantile: float
    posterior_upper_quantile: float
    lower_quantile_level: float
    upper_quantile_level: float
    effective_sample_size: float
    observation_count: int
    alpha: float
    beta: float
    prior_alpha: float
    prior_beta: float
    prior_sources: tuple[str, ...]
    prior_version: str
    external_contributions: tuple[ExternalPriorContribution, ...]
    parent_level: PosteriorLevel | None
    parent_mean: float | None
    calculated_at: datetime
    engine_version: str

    @property
    def interval_width(self) -> float:
        return self.posterior_upper_quantile - self.posterior_lower_quantile

    @property
    def sufficient(self) -> bool:
        """Whether this cell has enough of its own data to act on.

        Deliberately strict, and deliberately about the *data* rather than the
        interval: a narrow interval produced mostly by a prior is confidence
        borrowed from somewhere else, and acting on it is how a router talks
        itself into a model it has never actually run.
        """

        return self.observation_count >= 20 and self.effective_sample_size >= 10.0


@dataclass
class CostLatencySummary:
    """Cost and latency for one cell, in their own units.

    Percentiles rather than a mean-and-interval because both distributions are
    long-tailed: the mean latency of a video model is dominated by the queue,
    and the number that matters operationally is p90.
    """

    key: EvidenceKey
    observation_count: int = 0
    latency_ms_mean: float | None = None
    latency_ms_p50: float | None = None
    latency_ms_p90: float | None = None
    latency_ms_max: int | None = None
    cost_credits_mean: float | None = None
    cost_credits_total: float = 0.0
    cost_usd_total: float = 0.0

    @staticmethod
    def _percentile(values: Sequence[float], fraction: float) -> float:
        """Nearest-rank percentile.

        Chosen over interpolation because it always returns a value that was
        actually observed, which matters when someone checks a p90 against the
        log.

        `math.ceil`, not `round(x + 0.5)`. The two agree until `fraction * n`
        is an integer, at which point `round` sees a half and breaks the tie to
        even — so p90 of ten samples returned the maximum and p90 of twenty
        returned the correct rank, an off-by-one that depended on nothing but
        parity.
        """

        if not values:
            raise ValueError("no values")
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
        return ordered[index]


@dataclass
class PosteriorRun:
    """Everything one offline computation produced."""

    run_id: str
    calculated_at: datetime
    engine_version: str
    records: list[PosteriorRecord] = field(default_factory=list)
    cost_latency: list[CostLatencySummary] = field(default_factory=list)
    observation_count: int = 0
    quarantined: list[tuple[str, str]] = field(default_factory=list)
    strict_isolation: bool = False

    def leaf_records(self) -> list[PosteriorRecord]:
        return [record for record in self.records if record.level is PosteriorLevel.CONDITION]

    def scenario_records(self) -> list[PosteriorRecord]:
        return [record for record in self.records if record.level is PosteriorLevel.SCENARIO]


@dataclass(frozen=True)
class _Cell:
    """Accumulated evidence for one (grouping, outcome) pair."""

    alpha_data: float
    beta_data: float
    observation_count: int
    weight_sum: float
    weight_square_sum: float

    @property
    def effective_sample_size(self) -> float:
        return (self.weight_sum**2 / self.weight_square_sum) if self.weight_square_sum else 0.0


class HierarchicalPosteriorEngine:
    """Compute posteriors offline. Reads observations; touches no routing code."""

    version = "router-posterior-v1"

    def __init__(
        self,
        *,
        pooling_kappa: dict[PosteriorLevel, float] | None = None,
        global_prior: dict[OutcomeName, tuple[float, float]] | None = None,
        lower_quantile: float = DEFAULT_LOWER_QUANTILE,
        upper_quantile: float = DEFAULT_UPPER_QUANTILE,
        strict_isolation: bool = False,
        prior_version: str = "none",
    ):
        if not 0.0 < lower_quantile < upper_quantile < 1.0:
            raise ValueError("quantile levels must satisfy 0 < lower < upper < 1")
        self.pooling_kappa = (
            dict.fromkeys(DEFAULT_POOLING_KAPPA, 0.0)
            if strict_isolation
            else dict(pooling_kappa or DEFAULT_POOLING_KAPPA)
        )
        self.global_prior = dict(global_prior or DEFAULT_GLOBAL_PRIOR)
        self.lower_quantile = lower_quantile
        self.upper_quantile = upper_quantile
        self.strict_isolation = strict_isolation
        self.prior_version = prior_version

    # ------------------------------------------------------------------
    # accumulation

    @staticmethod
    def _unit_value(outcome: OutcomeName, raw: float) -> float:
        scale: MetricScale = OUTCOME_SCALES[outcome]
        return scale.to_unit(raw)

    def _collect(
        self,
        observations: Iterable[ProductionObservation],
        outcome: OutcomeName,
        *,
        grouping: str,
    ) -> dict[tuple[str, ...], _Cell]:
        """Bucket observations for one outcome at one level of the hierarchy.

        The ``may_pool`` assertion looks redundant — every observation for one
        outcome carries the same scale by construction — and it is kept because
        the construction is exactly the thing a future change could break
        without any test noticing.
        """

        scale_id = OUTCOME_SCALES[outcome].scale_id
        alpha: dict[tuple[str, ...], float] = defaultdict(float)
        beta: dict[tuple[str, ...], float] = defaultdict(float)
        counts: dict[tuple[str, ...], int] = defaultdict(int)
        weights: dict[tuple[str, ...], float] = defaultdict(float)
        squares: dict[tuple[str, ...], float] = defaultdict(float)
        for observation in observations:
            raw = observation.outcome_value(outcome)
            if raw is None:
                continue
            observation_scale = observation.key_for(outcome).metric_scale_id
            if not may_pool(observation_scale, scale_id):
                raise ValueError(
                    f"observation {observation.observation_id} is on scale {observation_scale} "
                    f"and cannot join a {scale_id} posterior without a calibration bridge"
                )
            unit = self._unit_value(outcome, raw)
            bucket = self._group_key(observation, grouping)
            # A continuous observation contributes its own mass to each side.
            # For a binary outcome this reduces exactly to a success/failure
            # count, so the two kinds of outcome share one code path without
            # either being distorted.
            alpha[bucket] += unit
            beta[bucket] += 1.0 - unit
            counts[bucket] += 1
            weights[bucket] += 1.0
            squares[bucket] += 1.0
        return {
            bucket: _Cell(alpha[bucket], beta[bucket], counts[bucket], weights[bucket], squares[bucket])
            for bucket in counts
        }

    @staticmethod
    def _group_key(observation: ProductionObservation, grouping: str) -> tuple[str, ...]:
        base = (observation.provider, observation.model_id, observation.exact_version)
        if grouping == "version":
            return base
        if grouping == "task":
            return (*base, observation.task_type.value)
        if grouping == "scenario":
            return (*base, observation.task_type.value, observation.scenario.value)
        if grouping == "condition":
            return (
                *base,
                observation.task_type.value,
                observation.scenario.value,
                observation.conditions.token,
            )
        raise AssertionError(f"unknown grouping {grouping}")  # pragma: no cover

    # ------------------------------------------------------------------
    # computation

    def compute(
        self,
        observations: Sequence[ProductionObservation],
        *,
        run_id: str,
        external_priors: dict[tuple[str, OutcomeName], tuple[ExternalPriorContribution, ...]] | None = None,
        now: datetime | None = None,
        outcomes: Sequence[OutcomeName] | None = None,
    ) -> PosteriorRun:
        """Build every level of the hierarchy, top down.

        ``external_priors`` is keyed by the *scenario-level* key token, because
        that is the finest granularity external evidence is ever measured at.
        A benchmark never tells you how a model does at 1080P specifically, so
        the condition level inherits the scenario level's external prior
        through pooling rather than being given its own copy.
        """

        calculated_at = now or datetime.now(UTC)
        external_priors = external_priors or {}
        run = PosteriorRun(
            run_id=run_id,
            calculated_at=calculated_at,
            engine_version=self.version,
            observation_count=len(observations),
            strict_isolation=self.strict_isolation,
        )
        usable, quarantined = quarantine_unversioned(observations)
        run.quarantined = quarantined

        for outcome in outcomes or list(OutcomeName):
            scale = OUTCOME_SCALES[outcome]
            global_alpha, global_beta = self.global_prior[outcome]
            global_mean = beta_mean(global_alpha, global_beta)

            version_cells = self._collect(usable, outcome, grouping="version")
            task_cells = self._collect(usable, outcome, grouping="task")
            scenario_cells = self._collect(usable, outcome, grouping="scenario")
            condition_cells = self._collect(usable, outcome, grouping="condition")

            version_means: dict[tuple[str, ...], float] = {}
            task_means: dict[tuple[str, ...], float] = {}
            scenario_means: dict[tuple[str, ...], float] = {}

            for bucket, cell in sorted(version_cells.items()):
                prior_alpha, prior_beta = self._shrink(
                    global_mean, self.pooling_kappa[PosteriorLevel.VERSION], global_alpha, global_beta
                )
                record = self._record(
                    key=EvidenceKey(
                        provider=bucket[0],
                        model_id=bucket[1],
                        exact_version=bucket[2],
                        task_type=TaskType.ANY,
                        scenario=Scenario.ANY,
                        metric_scale_id=scale.scale_id,
                    ),
                    outcome=outcome,
                    level=PosteriorLevel.VERSION,
                    condition=None,
                    cell=cell,
                    prior_alpha=prior_alpha,
                    prior_beta=prior_beta,
                    prior_sources=("global_fixed",),
                    external=(),
                    parent_level=PosteriorLevel.GLOBAL,
                    parent_mean=global_mean,
                    calculated_at=calculated_at,
                )
                version_means[bucket] = record.posterior_mean
                run.records.append(record)

            for bucket, cell in sorted(task_cells.items()):
                parent_mean = version_means.get(bucket[:3], global_mean)
                prior_alpha, prior_beta = self._shrink(
                    parent_mean, self.pooling_kappa[PosteriorLevel.TASK], global_alpha, global_beta
                )
                record = self._record(
                    key=EvidenceKey(
                        provider=bucket[0],
                        model_id=bucket[1],
                        exact_version=bucket[2],
                        task_type=TaskType(bucket[3]),
                        scenario=Scenario.ANY,
                        metric_scale_id=scale.scale_id,
                    ),
                    outcome=outcome,
                    level=PosteriorLevel.TASK,
                    condition=None,
                    cell=cell,
                    prior_alpha=prior_alpha,
                    prior_beta=prior_beta,
                    prior_sources=("global_fixed", "version_level"),
                    external=(),
                    parent_level=PosteriorLevel.VERSION,
                    parent_mean=parent_mean,
                    calculated_at=calculated_at,
                )
                task_means[bucket] = record.posterior_mean
                run.records.append(record)

            for bucket, cell in sorted(scenario_cells.items()):
                parent_mean = task_means.get(bucket[:4], version_means.get(bucket[:3], global_mean))
                prior_alpha, prior_beta = self._shrink(
                    parent_mean, self.pooling_kappa[PosteriorLevel.SCENARIO], global_alpha, global_beta
                )
                key = EvidenceKey(
                    provider=bucket[0],
                    model_id=bucket[1],
                    exact_version=bucket[2],
                    task_type=TaskType(bucket[3]),
                    scenario=Scenario(bucket[4]),
                    metric_scale_id=scale.scale_id,
                )
                external = external_priors.get((key.token, outcome), ())
                record = self._record(
                    key=key,
                    outcome=outcome,
                    level=PosteriorLevel.SCENARIO,
                    condition=None,
                    cell=cell,
                    prior_alpha=prior_alpha,
                    prior_beta=prior_beta,
                    prior_sources=("global_fixed", "task_level"),
                    external=external,
                    parent_level=PosteriorLevel.TASK,
                    parent_mean=parent_mean,
                    calculated_at=calculated_at,
                )
                scenario_means[bucket] = record.posterior_mean
                run.records.append(record)

            for bucket, cell in sorted(condition_cells.items()):
                scenario_bucket = bucket[:5]
                parent_mean = scenario_means.get(scenario_bucket, global_mean)
                prior_alpha, prior_beta = self._shrink(
                    parent_mean, self.pooling_kappa[PosteriorLevel.CONDITION], global_alpha, global_beta
                )
                duration, resolution, reference_mode = bucket[5].split("|")
                run.records.append(
                    self._record(
                        key=EvidenceKey(
                            provider=bucket[0],
                            model_id=bucket[1],
                            exact_version=bucket[2],
                            task_type=TaskType(bucket[3]),
                            scenario=Scenario(bucket[4]),
                            metric_scale_id=scale.scale_id,
                        ),
                        outcome=outcome,
                        level=PosteriorLevel.CONDITION,
                        condition=ConditionBucket(
                            duration_bucket=duration,  # type: ignore[arg-type]
                            resolution=resolution,
                            reference_mode=reference_mode,  # type: ignore[arg-type]
                        ),
                        cell=cell,
                        prior_alpha=prior_alpha,
                        prior_beta=prior_beta,
                        prior_sources=("global_fixed", "scenario_level"),
                        external=(),
                        parent_level=PosteriorLevel.SCENARIO,
                        parent_mean=parent_mean,
                        calculated_at=calculated_at,
                    )
                )

        run.cost_latency = summarize_cost_and_latency(usable)
        return run

    @staticmethod
    def _shrink(
        parent_mean: float, kappa: float, global_alpha: float, global_beta: float
    ) -> tuple[float, float]:
        """Turn a parent's mean into pseudo-counts for its child.

        The fixed global prior is **added** to the parent's contribution rather
        than replaced by it. Without that floor a cell whose parent is already
        near certainty inherits a near-zero pseudo-count on one side, and thirty
        consecutive successes then produce a Beta with b below 0.01 — a
        distribution so concentrated that its interval is [0.99999, 1.0] and
        every later comparison with it is noise. Keeping Jeffreys' half on each
        side means no cell can ever claim more certainty than its own data
        supports.

        With kappa=0 — strict isolation — only the global prior remains, which
        is exactly the intended "this cell knows nothing but its own data".
        """

        if kappa <= 0:
            return global_alpha, global_beta
        mean = min(max(parent_mean, 1e-6), 1.0 - 1e-6)
        return global_alpha + mean * kappa, global_beta + (1.0 - mean) * kappa

    def _record(
        self,
        *,
        key: EvidenceKey,
        outcome: OutcomeName,
        level: PosteriorLevel,
        condition: ConditionBucket | None,
        cell: _Cell,
        prior_alpha: float,
        prior_beta: float,
        prior_sources: tuple[str, ...],
        external: tuple[ExternalPriorContribution, ...],
        parent_level: PosteriorLevel | None,
        parent_mean: float | None,
        calculated_at: datetime,
    ) -> PosteriorRecord:
        external_alpha = sum(item.alpha for item in external)
        external_beta = sum(item.beta for item in external)
        total_prior_alpha = prior_alpha + external_alpha
        total_prior_beta = prior_beta + external_beta
        alpha = total_prior_alpha + cell.alpha_data
        beta = total_prior_beta + cell.beta_data
        sources = (*prior_sources, *(item.layer.value for item in external))
        return PosteriorRecord(
            key=key,
            outcome=outcome,
            level=level,
            condition=condition,
            posterior_mean=beta_mean(alpha, beta),
            posterior_lower_quantile=beta_quantile(self.lower_quantile, alpha, beta),
            posterior_upper_quantile=beta_quantile(self.upper_quantile, alpha, beta),
            lower_quantile_level=self.lower_quantile,
            upper_quantile_level=self.upper_quantile,
            effective_sample_size=cell.effective_sample_size,
            observation_count=cell.observation_count,
            alpha=alpha,
            beta=beta,
            prior_alpha=total_prior_alpha,
            prior_beta=total_prior_beta,
            prior_sources=sources,
            prior_version=self.prior_version,
            external_contributions=external,
            parent_level=parent_level,
            parent_mean=parent_mean,
            calculated_at=calculated_at,
            engine_version=self.version,
        )


def quarantine_unversioned(
    observations: Sequence[ProductionObservation],
) -> tuple[list[ProductionObservation], list[tuple[str, str]]]:
    """Split off observations that cannot safely be attributed.

    An observation whose model was recorded by alias describes whatever the
    alias pointed at that day. Counting it under the snapshot the alias
    currently resolves to is precisely the cross-version contamination this
    work exists to prevent, so it is held back with its reason rather than
    dropped silently.
    """

    usable: list[ProductionObservation] = []
    quarantined: list[tuple[str, str]] = []
    for observation in observations:
        if observation.model_is_alias:
            quarantined.append((observation.observation_id, "MODEL_RECORDED_AS_ALIAS"))
            continue
        if not observation.exact_version.strip():
            quarantined.append((observation.observation_id, "NO_EXACT_VERSION"))
            continue
        usable.append(observation)
    return usable, quarantined


def summarize_cost_and_latency(
    observations: Sequence[ProductionObservation],
) -> list[CostLatencySummary]:
    """Cost and latency per (version, task, scenario), in their own units."""

    grouped: dict[tuple[str, str, str, str, str], list[ProductionObservation]] = defaultdict(list)
    for observation in observations:
        grouped[
            (
                observation.provider,
                observation.model_id,
                observation.exact_version,
                observation.task_type.value,
                observation.scenario.value,
            )
        ].append(observation)
    summaries: list[CostLatencySummary] = []
    for bucket, items in sorted(grouped.items()):
        latencies = [float(item.latency_ms) for item in items if item.latency_ms is not None]
        credits = [item.cost_credits for item in items if item.cost_credits is not None]
        usd = [item.cost_usd for item in items if item.cost_usd is not None]
        summary = CostLatencySummary(
            key=EvidenceKey(
                provider=bucket[0],
                model_id=bucket[1],
                exact_version=bucket[2],
                task_type=TaskType(bucket[3]),
                scenario=Scenario(bucket[4]),
                # Cost and latency are not on any quality scale, and saying so
                # in the key stops them being joined to one by accident.
                metric_scale_id="prod.operational-units",
            ),
            observation_count=len(items),
        )
        if latencies:
            summary.latency_ms_mean = sum(latencies) / len(latencies)
            summary.latency_ms_p50 = CostLatencySummary._percentile(latencies, 0.50)
            summary.latency_ms_p90 = CostLatencySummary._percentile(latencies, 0.90)
            summary.latency_ms_max = int(max(latencies))
        if credits:
            summary.cost_credits_mean = sum(credits) / len(credits)
            summary.cost_credits_total = sum(credits)
        if usd:
            summary.cost_usd_total = sum(usd)
        summaries.append(summary)
    return summaries


__all__ = [
    "DEFAULT_GLOBAL_PRIOR",
    "DEFAULT_LOWER_QUANTILE",
    "DEFAULT_POOLING_KAPPA",
    "DEFAULT_UPPER_QUANTILE",
    "CostLatencySummary",
    "ExternalPriorContribution",
    "HierarchicalPosteriorEngine",
    "PosteriorLevel",
    "PosteriorRecord",
    "PosteriorRun",
    "quarantine_unversioned",
    "summarize_cost_and_latency",
]
