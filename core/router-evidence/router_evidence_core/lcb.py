"""Conservative lower-confidence-bound routing evidence.

The one thing in this package that is allowed anywhere near a live decision,
and it is off by default.

**What it does.** For the model, task, scenario and conditions of one request,
it looks up the production posterior and offers the router the *lower quantile*
of each relevant dimension instead of the mean. A model with a great average
and four observations has a low lower bound, so it does not win on the strength
of four observations. A model with a slightly worse average and three hundred
observations has a tight interval and wins. That is the whole idea: pay for
uncertainty out of the score rather than out of the user's shot.

**What it deliberately does not do.**

* It does not change the router. ``VideoModelRouter`` already accepts
  per-request ``RoutingEvidence``; this produces one. No line of the ranking
  code moves, so with the flag off the ranking is byte-identical to before.
* It does not explore. A lower bound is pessimistic by construction, which is
  the opposite of an exploration bonus. Optimism lives in ``exploration.py``
  and is not wired to anything.
* It does not invent evidence. A cell that is not
  :attr:`~router_evidence_core.posterior.PosteriorRecord.sufficient` is
  omitted from the adjustment map entirely, and the router then falls back to
  the hand-authored capability prior exactly as it does today.

**The collapse.** The router keys on ``provider:model_id`` and knows nothing
about versions, tasks or scenes. Reaching it therefore means collapsing a
six-part key down to two parts. That happens here, once, for one request, using
the version the registry says each candidate will actually run — and never
during aggregation, where it would be contamination.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .keys import ConditionBucket, Scenario, TaskType
from .observations import OUTCOME_TO_ROUTER_DIMENSION, OutcomeName
from .posterior import PosteriorLevel, PosteriorRecord

#: Outcomes the LCB is allowed to move a routing dimension with. A subset of
#: what has a posterior: ``regenerated`` and ``switched_model`` are real
#: signals and they are *negative* ones, and subtracting them from a capability
#: score would double-count the same dissatisfaction that ``accepted_output``
#: already carries.
LCB_OUTCOMES: tuple[OutcomeName, ...] = (
    OutcomeName.ACCEPTED_OUTPUT,
    OutcomeName.QC_IDENTITY,
    OutcomeName.QC_MOTION,
    OutcomeName.QC_TEMPORAL_CONSISTENCY,
)


@dataclass(frozen=True)
class LcbSettings:
    """Everything about how conservative this is, in one reviewable object."""

    #: The master switch. Off means :meth:`ConservativeLcbBuilder.build` returns
    #: an empty adjustment map, which makes the router behave exactly as it did
    #: before this package existed.
    enabled: bool = False
    #: Minimum observations in the cell before it may move anything.
    min_observations: int = 20
    #: Minimum effective sample size in the cell.
    min_effective_sample_size: float = 10.0
    #: Widest interval that still counts as knowing something. A cell that is
    #: sufficient by count but still spans half the range is a cell where the
    #: outcomes disagree, and its lower bound would be pessimistic for the
    #: wrong reason.
    max_interval_width: float = 0.55
    #: Whether to fall back to the scenario level when the exact condition
    #: bucket has too little data. On by default: refusing to answer because
    #: nobody has run this model at 1080P specifically is more conservative
    #: than useful.
    allow_scenario_fallback: bool = True


@dataclass
class LcbDecision:
    """Why one model/dimension pair was or was not adjusted.

    Kept for every candidate, including the ones that changed nothing, because
    "the LCB did not fire" is the answer to most questions about a decision and
    it is unrecoverable after the fact.
    """

    router_model_key: str
    exact_version: str
    dimension: str
    outcome: OutcomeName
    applied: bool
    reason: str
    level: PosteriorLevel | None = None
    lower_quantile: float | None = None
    posterior_mean: float | None = None
    observation_count: int = 0
    effective_sample_size: float = 0.0


@dataclass
class LcbAdjustments:
    """The router-shaped result, plus the audit trail that produced it."""

    adjustments: dict[str, dict[str, float]] = field(default_factory=dict)
    sample_counts: dict[str, int] = field(default_factory=dict)
    decisions: list[LcbDecision] = field(default_factory=list)
    enabled: bool = False
    fallback_reason: str | None = None

    @property
    def affected_models(self) -> list[str]:
        return sorted(self.adjustments)

    @property
    def is_noop(self) -> bool:
        return not self.adjustments


class PosteriorLookup:
    """Indexed access to one run's records, by key and level.

    A plain dictionary rather than a query object: the whole point of the
    offline/online split is that the routing path never touches a database for
    this, it receives a snapshot that was computed and reviewed earlier.
    """

    def __init__(self, records: list[PosteriorRecord]):
        self._by_scenario: dict[tuple[str, OutcomeName], PosteriorRecord] = {}
        self._by_condition: dict[tuple[str, str, OutcomeName], PosteriorRecord] = {}
        for record in records:
            if record.level is PosteriorLevel.SCENARIO:
                self._by_scenario[(record.key.token, record.outcome)] = record
            elif record.level is PosteriorLevel.CONDITION and record.condition is not None:
                self._by_condition[(record.key.token, record.condition.token, record.outcome)] = record

    def scenario(self, key_token: str, outcome: OutcomeName) -> PosteriorRecord | None:
        return self._by_scenario.get((key_token, outcome))

    def condition(
        self, key_token: str, condition: ConditionBucket, outcome: OutcomeName
    ) -> PosteriorRecord | None:
        return self._by_condition.get((key_token, condition.token, outcome))

    def __len__(self) -> int:
        return len(self._by_scenario) + len(self._by_condition)


@dataclass(frozen=True)
class CandidateModel:
    """One model the router might pick, as the LCB needs to see it.

    Built from the capability registry at the call site. The important field is
    ``exact_version``: if the registry cannot name the snapshot a candidate
    will run, the LCB declines to adjust it rather than looking up whatever
    posterior happens to share the alias.
    """

    provider: str
    model_id: str
    exact_version: str


class ConservativeLcbBuilder:
    """Produce router-shaped adjustments from an offline posterior run."""

    version = "router-lcb-v1"

    def __init__(self, lookup: PosteriorLookup, settings: LcbSettings | None = None):
        self.lookup = lookup
        self.settings = settings or LcbSettings()

    def _usable(self, record: PosteriorRecord) -> tuple[bool, str]:
        if record.observation_count < self.settings.min_observations:
            return False, f"INSUFFICIENT_OBSERVATIONS_{record.observation_count}"
        if record.effective_sample_size < self.settings.min_effective_sample_size:
            return False, f"INSUFFICIENT_ESS_{record.effective_sample_size:.2f}"
        if record.interval_width > self.settings.max_interval_width:
            return False, f"INTERVAL_TOO_WIDE_{record.interval_width:.3f}"
        return True, "OK"

    def build(
        self,
        candidates: list[CandidateModel],
        *,
        task_type: TaskType,
        scenario: Scenario,
        conditions: ConditionBucket | None = None,
    ) -> LcbAdjustments:
        """The adjustment map for one request.

        Returns an empty map — and says why — whenever the flag is off, so a
        caller that forgets to check the flag still gets the safe behaviour.
        """

        result = LcbAdjustments(enabled=self.settings.enabled)
        if not self.settings.enabled:
            result.fallback_reason = "FEATURE_FLAG_OFF"
            return result
        if not len(self.lookup):
            result.fallback_reason = "NO_POSTERIOR_DATA"
            return result

        for candidate in candidates:
            if not candidate.exact_version.strip():
                result.decisions.append(
                    LcbDecision(
                        router_model_key=f"{candidate.provider}:{candidate.model_id}",
                        exact_version="",
                        dimension="*",
                        outcome=OutcomeName.ACCEPTED_OUTPUT,
                        applied=False,
                        reason="NO_EXACT_VERSION_FOR_CANDIDATE",
                    )
                )
                continue
            for outcome in LCB_OUTCOMES:
                dimension = OUTCOME_TO_ROUTER_DIMENSION.get(outcome)
                if dimension is None:  # pragma: no cover - LCB_OUTCOMES are all mapped
                    continue
                self._apply_one(result, candidate, task_type, scenario, conditions, outcome, dimension)

        if not result.adjustments:
            result.fallback_reason = "NO_SUFFICIENT_CELL"
        return result

    def _apply_one(
        self,
        result: LcbAdjustments,
        candidate: CandidateModel,
        task_type: TaskType,
        scenario: Scenario,
        conditions: ConditionBucket | None,
        outcome: OutcomeName,
        dimension: str,
    ) -> None:
        from .observations import OUTCOME_SCALES

        key_token = "|".join(
            (
                candidate.provider,
                candidate.model_id,
                candidate.exact_version,
                task_type.value,
                scenario.value,
                OUTCOME_SCALES[outcome].scale_id,
            )
        )
        router_key = f"{candidate.provider}:{candidate.model_id}"

        # Prefer the condition-level cell — it is the one that actually
        # describes this request — and fall back to the scenario level only
        # when the narrower cell is missing or too thin to act on.
        record: PosteriorRecord | None = None
        level_note = ""
        if conditions is not None:
            narrow = self.lookup.condition(key_token, conditions, outcome)
            if narrow is not None and self._usable(narrow)[0]:
                record = narrow
            elif narrow is not None and not self.settings.allow_scenario_fallback:
                result.decisions.append(
                    LcbDecision(
                        router_model_key=router_key,
                        exact_version=candidate.exact_version,
                        dimension=dimension,
                        outcome=outcome,
                        applied=False,
                        reason=self._usable(narrow)[1],
                        level=PosteriorLevel.CONDITION,
                        observation_count=narrow.observation_count,
                        effective_sample_size=narrow.effective_sample_size,
                    )
                )
                return
            elif narrow is not None:
                level_note = "CONDITION_CELL_TOO_THIN;"
        if record is None:
            if conditions is not None and not self.settings.allow_scenario_fallback:
                result.decisions.append(
                    LcbDecision(
                        router_model_key=router_key,
                        exact_version=candidate.exact_version,
                        dimension=dimension,
                        outcome=outcome,
                        applied=False,
                        reason="NO_CONDITION_CELL_AND_FALLBACK_DISABLED",
                    )
                )
                return
            record = self.lookup.scenario(key_token, outcome)
        if record is None:
            result.decisions.append(
                LcbDecision(
                    router_model_key=router_key,
                    exact_version=candidate.exact_version,
                    dimension=dimension,
                    outcome=outcome,
                    applied=False,
                    reason=level_note + "NO_POSTERIOR_FOR_KEY",
                )
            )
            return

        usable, reason = self._usable(record)
        decision = LcbDecision(
            router_model_key=router_key,
            exact_version=candidate.exact_version,
            dimension=dimension,
            outcome=outcome,
            applied=usable,
            reason=level_note + reason,
            level=record.level,
            lower_quantile=record.posterior_lower_quantile,
            posterior_mean=record.posterior_mean,
            observation_count=record.observation_count,
            effective_sample_size=record.effective_sample_size,
        )
        result.decisions.append(decision)
        if not usable:
            return

        # The lower bound, not the mean. Two outcomes mapping to one dimension
        # would be an average across metrics, so the more pessimistic one wins
        # instead — which is also the conservative direction.
        existing = result.adjustments.setdefault(router_key, {})
        proposed = record.posterior_lower_quantile
        existing[dimension] = min(existing.get(dimension, 1.0), proposed)
        result.sample_counts[router_key] = max(
            result.sample_counts.get(router_key, 0), record.observation_count
        )


def merge_with_baseline(
    baseline: Mapping[str, Mapping[str, float]],
    lcb: LcbAdjustments,
) -> dict[str, dict[str, float]]:
    """Overlay LCB adjustments on whatever the caller was already using.

    An overlay rather than a replacement: models the LCB has nothing to say
    about keep the baseline's view of them, so switching the flag on cannot
    blank out evidence that already existed.
    """

    merged: dict[str, dict[str, float]] = {key: dict(value) for key, value in baseline.items()}
    for model_key, dimensions in lcb.adjustments.items():
        merged.setdefault(model_key, {}).update(dimensions)
    return merged


__all__ = [
    "LCB_OUTCOMES",
    "CandidateModel",
    "ConservativeLcbBuilder",
    "LcbAdjustments",
    "LcbDecision",
    "LcbSettings",
    "PosteriorLookup",
    "merge_with_baseline",
]
