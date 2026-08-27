"""Turning external records into priors — and finding out how few qualify.

Each external layer gets its own prior, on its own scale, for its own key.
Three layers times N scales means a lot of small distributions rather than one
comfortable number, and that is the point: a Likert 4.2 from a creator
comparison and a VBench 0.87 are not two readings of the same quantity, and
this module never pretends otherwise.

**Why almost nothing reaches the production posterior.** A prior can only be
folded into the production posterior for a key if the external scale and the
production outcome's scale can be pooled, which needs a calibration bridge.
``calibration.BRIDGES`` is empty. So the admission check in
:func:`production_contributions` will refuse every external record today, and
report exactly why for each one.

That is not a defect to work around. It is the mandate's own rule —
independent posteriors where there is no bridge — arriving at its logical
conclusion. The mechanism is built and exercised so that the day a bridge is
established, the priors flow through a reviewed path rather than a new one
written in a hurry.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .betamath import moment_match
from .calibration import bridge_between, may_pool
from .community import COMMUNITY_STANCE_SCALE_ID, CommunityAggregator
from .keys import BOUNDED_SCALE_KINDS, EvidenceKey, MetricScale, ScaleKind
from .layers import DEFAULT_PRIOR_STRENGTHS, EvidenceLayer
from .observations import OUTCOME_SCALES, OutcomeName
from .posterior import ExternalPriorContribution
from .records import BenchmarkRecord, CommunityRecord, ExternalRecord
from .store import LayerSnapshot

#: Scales that carry a *score* — something a model can be better or worse at.
#: Only the bounded ones here can become a prior; ``arena-elo`` and
#: ``leaderboard-rank`` are scores with no upper bound and no fixed field, so
#: they are carried, reported, and never turned into a probability.
SCORING_SCALES: dict[str, MetricScale] = {
    "vbench2-total-0-1": MetricScale(
        scale_id="vbench2-total-0-1",
        kind=ScaleKind.RATIO_0_1,
        description="VBench-style automatic total score in [0, 1].",
    ),
    "vbench2-dimension-0-1": MetricScale(
        scale_id="vbench2-dimension-0-1",
        kind=ScaleKind.RATIO_0_1,
        description="VBench-style automatic per-dimension score in [0, 1].",
    ),
    "percent-0-100": MetricScale(
        scale_id="percent-0-100",
        kind=ScaleKind.PERCENT_0_100,
        description="A percentage as published.",
    ),
    "likert-1-5": MetricScale(
        scale_id="likert-1-5",
        kind=ScaleKind.LIKERT_1_5,
        description="Five-point human rating.",
    ),
    "likert-1-10": MetricScale(
        scale_id="likert-1-10",
        kind=ScaleKind.LIKERT_1_10,
        description="Ten-point human rating.",
    ),
    "pairwise-win-rate": MetricScale(
        scale_id="pairwise-win-rate",
        kind=ScaleKind.WIN_RATE,
        description="Share of head-to-head comparisons won, in [0, 1].",
    ),
    "arena-elo": MetricScale(
        scale_id="arena-elo",
        kind=ScaleKind.ELO,
        description="Arena rating. Unbounded and relative to the field on the day it was read.",
    ),
    "leaderboard-rank": MetricScale(
        scale_id="leaderboard-rank",
        kind=ScaleKind.RANK,
        higher_is_better=False,
        description="Position on a leaderboard. Depends on who else entered.",
    ),
    "community-stance-net": MetricScale(
        scale_id="community-stance-net",
        kind=ScaleKind.ORDINAL_STANCE,
        description="Weighted net stance across deduplicated community reports, in [-1, 1].",
    ),
}

#: Scales that carry a *fact* rather than a score — a documented duration
#: ceiling, a reference-image limit, a published price, an API enum, or a
#: qualitative claim with no number at all.
#:
#: These are the most trustworthy external evidence there is and the least
#: useful for ranking: "the maximum is 30 seconds" is checkable, actionable and
#: says nothing about whether the model is any good. They are registered so
#: that official documentation can be recorded at all, and every one of them is
#: ``COUNT`` — unbounded — which is what stops any of them ever becoming a
#: prior. ``unscored`` is the honest scale for a marketing adjective.
DESCRIPTIVE_SCALES: dict[str, MetricScale] = {
    name: MetricScale(scale_id=name, kind=ScaleKind.COUNT, description=description)
    for name, description in {
        "seconds": "A duration in seconds, as documented.",
        "count": "A documented count: reference images, extension rounds, characters.",
        "usd": "A published price in US dollars.",
        "usd-per-video": "A published per-generation price in US dollars.",
        "usd-per-second": "A published per-second price in US dollars.",
        "cny-per-video": "A published per-generation price in yuan.",
        "cny-per-second": "A published per-second price in yuan.",
        "api-enum": "An enumerated value the API accepts, such as a resolution or aspect ratio.",
        "unscored": "A claim stated in words with no number. `value` must be null.",
    }.items()
}

#: The union, and the whole of what an external record may declare. A record on
#: a scale outside this set is refused at ingest: an unknown scale is an
#: unknown unit, and adding one is a deliberate act rather than an accommodation
#: of whatever a research pass happened to write.
KNOWN_EXTERNAL_SCALES: dict[str, MetricScale] = {**SCORING_SCALES, **DESCRIPTIVE_SCALES}


@dataclass(frozen=True)
class LayerPrior:
    """One layer's independent prior for one key, on that key's own scale.

    ``mean`` is ``None`` for unbounded scales — an Elo has no probability
    interpretation — and the record is still returned, because "we know the
    Elo and cannot turn it into a prior" is the useful answer.
    """

    layer: EvidenceLayer
    key: EvidenceKey
    scale: MetricScale
    mean: float | None
    alpha: float | None
    beta: float | None
    record_count: int
    eligible_record_count: int
    effective_sample_size: float
    record_ids: tuple[str, ...]
    excluded: dict[str, int]
    source_version: str
    notes: tuple[str, ...] = ()

    @property
    def usable_as_prior(self) -> bool:
        return self.mean is not None and self.eligible_record_count > 0


@dataclass(frozen=True)
class RefusedContribution:
    """An external prior that was offered to a production posterior and declined."""

    layer: EvidenceLayer
    key: EvidenceKey
    outcome: OutcomeName
    external_scale_id: str
    production_scale_id: str
    reason: str


def _measurement_keys(record: ExternalRecord) -> list[tuple[EvidenceKey, float | None, str]]:
    keys: list[tuple[EvidenceKey, float | None, str]] = []
    for measurement in record.measurements:
        keys.append(
            (
                EvidenceKey(
                    provider=record.binding.provider,
                    model_id=record.binding.model_id,
                    exact_version=record.binding.exact_version,
                    task_type=measurement.task_type,
                    scenario=measurement.scenario,
                    metric_scale_id=measurement.metric_scale_id,
                ),
                measurement.value,
                measurement.metric_scale_id,
            )
        )
    return keys


def build_layer_priors(snapshot: LayerSnapshot) -> list[LayerPrior]:
    """One prior per (key) for a single layer.

    Community records take a different path from the other two: they are
    counted through :class:`CommunityAggregator` first, so the prior sees an
    effective sample size rather than a post count, and their stance rather
    than a score.
    """

    if snapshot.layer is EvidenceLayer.COMMUNITY:
        return _build_community_priors(snapshot)
    return _build_measured_priors(snapshot)


def _build_measured_priors(snapshot: LayerSnapshot) -> list[LayerPrior]:
    grouped: dict[EvidenceKey, list[tuple[ExternalRecord, float | None]]] = defaultdict(list)
    excluded: dict[EvidenceKey, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for record in snapshot.records:
        for key, value, _scale_id in _measurement_keys(record):
            if not record.prior_eligible:
                for reason in record.ineligibility_reasons:
                    excluded[key][reason] += 1
                grouped[key]  # noqa: B018 - register the key so the exclusion is reported
                continue
            grouped[key].append((record, value))

    strength = DEFAULT_PRIOR_STRENGTHS[snapshot.layer]
    priors: list[LayerPrior] = []
    for key in sorted(grouped, key=lambda item: item.token):
        entries = grouped[key]
        scale = KNOWN_EXTERNAL_SCALES.get(key.metric_scale_id)
        notes: list[str] = []
        if scale is None:
            notes.append("UNKNOWN_SCALE")
        numeric = [value for _record, value in entries if value is not None]
        if not numeric:
            notes.append("NO_NUMERIC_VALUE")
        mean: float | None = None
        alpha: float | None = None
        beta: float | None = None
        unit_values: list[float] = []
        if scale is not None and scale.kind in BOUNDED_SCALE_KINDS:
            # Averaging inside one scale, one key and one layer is the only
            # averaging this package does. Two VBench totals for the same
            # snapshot on the same scene are two readings of one quantity;
            # anything wider than that is refused elsewhere.
            #
            # Ingest rejects a value outside its own scale, so reaching one
            # here means a file was hand-edited or written by an older ingest.
            # Note it and carry on rather than taking the whole report down.
            for value in numeric:
                try:
                    unit_values.append(scale.to_unit(value))
                except ValueError:
                    notes.append("VALUE_OUT_OF_SCALE")
        elif scale is not None and numeric:
            notes.append(f"UNBOUNDED_SCALE_{scale.kind.value}")
        if unit_values:
            mean = sum(unit_values) / len(unit_values)
            # Sized from the records that actually carried a number, not from
            # every record bound to the key. Four records stating a result in
            # words and one publishing 0.82 is one measurement, and charging
            # the full ceiling for it claims five agreeing sources.
            pseudo = strength.contribution(len(unit_values))
            clamped = min(max(mean, 1e-3), 1.0 - 1e-3)
            alpha, beta = clamped * pseudo, (1.0 - clamped) * pseudo
        priors.append(
            LayerPrior(
                layer=snapshot.layer,
                key=key,
                scale=scale
                or MetricScale(
                    scale_id=key.metric_scale_id,
                    kind=ScaleKind.COUNT,
                    description="Scale not registered in KNOWN_EXTERNAL_SCALES.",
                ),
                mean=mean,
                alpha=alpha,
                beta=beta,
                record_count=len(entries) + sum(excluded[key].values()),
                eligible_record_count=len(entries),
                effective_sample_size=float(len({record.record_id for record, _ in entries})),
                record_ids=tuple(sorted({record.record_id for record, _ in entries})),
                excluded=dict(excluded[key]),
                source_version=snapshot.version,
                notes=tuple(notes),
            )
        )
    return priors


def _build_community_priors(snapshot: LayerSnapshot) -> list[LayerPrior]:
    aggregator = CommunityAggregator()
    grouped: dict[EvidenceKey, list[CommunityRecord]] = defaultdict(list)
    for record in snapshot.records:
        assert isinstance(record, CommunityRecord)
        for key, _value, _scale in _measurement_keys(record):
            # Whatever scale the post's measurement declared, what this layer
            # actually produces is a stance — so the key says so. A record
            # quoting "15 seconds max" on the `seconds` scale contributes its
            # author's opinion of the model on that scene, not a reading of a
            # duration; filing the stance under `seconds` would invite exactly
            # the comparison that has no meaning.
            grouped[key.model_copy(update={"metric_scale_id": COMMUNITY_STANCE_SCALE_ID})].append(
                record
            )

    strength = DEFAULT_PRIOR_STRENGTHS[EvidenceLayer.COMMUNITY]
    priors: list[LayerPrior] = []
    for key in sorted(grouped, key=lambda item: item.token):
        aggregate = aggregator.aggregate(key, grouped[key])
        scale = KNOWN_EXTERNAL_SCALES.get(key.metric_scale_id)
        notes: list[str] = ["COMMUNITY_STANCE_NOT_A_BENCHMARK_VALUE"]
        if aggregate.has_conflict:
            notes.append("COMMUNITY_INTERNAL_CONFLICT")
        mean: float | None = None
        alpha: float | None = None
        beta: float | None = None
        if aggregate.stance_score is not None and aggregate.effective_sample_size > 0:
            # Stance lives on [-1, 1]; the prior needs [0, 1]. This is the
            # affine map within one scale, not a conversion to any other.
            mean = (aggregate.stance_score + 1.0) / 2.0
            pseudo = min(strength.ceiling, strength.per_record * aggregate.effective_sample_size)
            if pseudo > 0:
                clamped = min(max(mean, 1e-3), 1.0 - 1e-3)
                alpha, beta = clamped * pseudo, (1.0 - clamped) * pseudo
        priors.append(
            LayerPrior(
                layer=EvidenceLayer.COMMUNITY,
                key=key,
                scale=scale
                or MetricScale(
                    scale_id=key.metric_scale_id,
                    kind=ScaleKind.ORDINAL_STANCE,
                    description="Community stance.",
                ),
                mean=mean,
                alpha=alpha,
                beta=beta,
                record_count=len(grouped[key]),
                eligible_record_count=aggregate.observation_count,
                effective_sample_size=aggregate.effective_sample_size,
                record_ids=tuple(sorted({record.record_id for record in grouped[key]})),
                excluded=aggregate.excluded,
                source_version=snapshot.version,
                notes=tuple(notes),
            )
        )
    return priors


def production_contributions(
    priors: list[LayerPrior],
    outcome: OutcomeName,
) -> tuple[dict[tuple[str, OutcomeName], tuple[ExternalPriorContribution, ...]], list[RefusedContribution]]:
    """Offer external priors to one production outcome, and record every refusal.

    Returns the contributions that were admitted — today, none — and a refusal
    for each one that was not, naming the two scales that have no bridge
    between them. The refusals are the report; a silent empty dictionary would
    look identical to "there was no evidence".
    """

    production_scale = OUTCOME_SCALES[outcome].scale_id
    admitted: dict[tuple[str, OutcomeName], list[ExternalPriorContribution]] = defaultdict(list)
    refused: list[RefusedContribution] = []
    for prior in priors:
        if not prior.usable_as_prior:
            continue
        if not may_pool(prior.key.metric_scale_id, production_scale):
            refused.append(
                RefusedContribution(
                    layer=prior.layer,
                    key=prior.key,
                    outcome=outcome,
                    external_scale_id=prior.key.metric_scale_id,
                    production_scale_id=production_scale,
                    reason="NO_CALIBRATION_BRIDGE",
                )
            )
            continue
        bridge = bridge_between(prior.key.metric_scale_id, production_scale)
        assert prior.alpha is not None and prior.beta is not None
        production_key = prior.key.model_copy(update={"metric_scale_id": production_scale})
        admitted[(production_key.token, outcome)].append(
            ExternalPriorContribution(
                layer=prior.layer,
                alpha=prior.alpha,
                beta=prior.beta,
                record_count=prior.eligible_record_count,
                source_version=(
                    f"{prior.source_version}+bridge:{bridge.from_scale_id}->{bridge.to_scale_id}"
                    if bridge
                    else prior.source_version
                ),
                eligible_record_ids=prior.record_ids,
            )
        )
    return {key: tuple(value) for key, value in admitted.items()}, refused


def benchmark_protocol_complete(record: BenchmarkRecord) -> bool:
    """Whether a benchmark record states enough to be reproducible.

    A benchmark without a sample size, a protocol or a stated evaluator is a
    screenshot of a number. It is still recorded; it just does not get to
    claim the authority of a measurement.
    """

    return (
        record.statistics.sample_size is not None
        and record.human_or_automatic != "unstated"
        and (record.protocol_url is not None or record.benchmark_version is not None)
    )


def prior_summary(priors: list[LayerPrior]) -> dict[str, object]:
    usable = [prior for prior in priors if prior.usable_as_prior]
    return {
        "keys": len(priors),
        "usable_as_prior": len(usable),
        "unusable": len(priors) - len(usable),
        "scales": sorted({prior.key.metric_scale_id for prior in priors}),
        "effective_sample_size_total": round(sum(prior.effective_sample_size for prior in priors), 3),
    }


def moment_matched_prior(mean: float, pseudo_count: float) -> tuple[float, float]:
    """Pseudo-counts for a stated mean, expressed through the shared helper.

    Kept as a named function because ``moment_match`` takes a variance and the
    call sites here think in strength; converting in one place stops the two
    parameterisations being mixed up at a call site.
    """

    variance = mean * (1.0 - mean) / (pseudo_count + 1.0)
    return moment_match(min(max(mean, 1e-3), 1.0 - 1e-3), variance)


__all__ = [
    "DESCRIPTIVE_SCALES",
    "KNOWN_EXTERNAL_SCALES",
    "SCORING_SCALES",
    "LayerPrior",
    "RefusedContribution",
    "benchmark_protocol_complete",
    "build_layer_priors",
    "moment_matched_prior",
    "prior_summary",
    "production_contributions",
]
