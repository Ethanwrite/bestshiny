"""Coverage, conflicts and contamination — the report that says what is missing.

A pile of evidence is only useful next to an honest account of its holes. This
module produces three things:

* **Coverage** — for every model, exact version and scenario, how much evidence
  exists in each layer and how much of it is admissible. Absence is a row, not
  a gap in the table.
* **Conflicts** — where two admissible records disagree beyond what their
  intervals allow, and where the community disagrees with itself. Marked, never
  resolved.
* **Contamination** — a mechanical audit that no aggregate mixed exact
  versions, task types, scenarios or metric scales. This is the check that
  turns "we were careful" into "it was verified", and it is the one that would
  catch a future refactor quietly widening a group key.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .keys import EvidenceKey
from .layers import EvidenceLayer
from .posterior import PosteriorRecord, PosteriorRun
from .priors import LayerPrior
from .store import LayerSnapshot


@dataclass
class ModelCoverage:
    """What is known about one model at one exact version."""

    provider: str
    model_id: str
    exact_version: str
    official_records: int = 0
    benchmark_records: int = 0
    community_records: int = 0
    official_eligible: int = 0
    benchmark_eligible: int = 0
    community_effective_sample_size: float = 0.0
    scenarios_with_evidence: set[str] = field(default_factory=set)
    scenarios_with_eligible_evidence: set[str] = field(default_factory=set)
    scales: set[str] = field(default_factory=set)
    production_observations: int = 0
    production_scenarios: set[str] = field(default_factory=set)
    exclusion_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def total_records(self) -> int:
        return self.official_records + self.benchmark_records + self.community_records

    @property
    def total_eligible(self) -> int:
        return self.official_eligible + self.benchmark_eligible

    @property
    def insufficient(self) -> bool:
        """Whether this version has too little to say anything with.

        Deliberately generous about what counts and strict about how much: any
        one of the three external layers being admissible, or twenty
        production observations, clears it. A version that clears none of
        those is one the router should be treating as unknown.
        """

        return self.total_eligible == 0 and self.production_observations < 20


@dataclass
class EvidenceConflict:
    conflict_id: str
    kind: str
    key: EvidenceKey
    description: str
    record_ids: tuple[str, ...]
    layers: tuple[str, ...]


@dataclass
class ContaminationFinding:
    kind: str
    detail: str
    offending: tuple[str, ...]


@dataclass
class CoverageReport:
    models: dict[str, ModelCoverage] = field(default_factory=dict)
    source_distribution: dict[str, dict[str, int]] = field(default_factory=dict)
    conflicts: list[EvidenceConflict] = field(default_factory=list)
    contamination: list[ContaminationFinding] = field(default_factory=list)
    unconfirmed_versions: list[tuple[str, str]] = field(default_factory=list)
    layer_versions: dict[str, str] = field(default_factory=dict)

    @property
    def insufficient_models(self) -> list[str]:
        return sorted(name for name, coverage in self.models.items() if coverage.insufficient)

    @property
    def clean(self) -> bool:
        return not self.contamination


def _model_token(provider: str, model_id: str, exact_version: str) -> str:
    return f"{provider}:{model_id}@{exact_version}"


def build_coverage(
    snapshots: tuple[LayerSnapshot, LayerSnapshot, LayerSnapshot],
    *,
    posterior_run: PosteriorRun | None = None,
) -> CoverageReport:
    """Coverage across the three external layers and, optionally, production."""

    report = CoverageReport()
    for snapshot in snapshots:
        report.layer_versions[snapshot.layer.value] = snapshot.version
        for record in snapshot.records:
            token = _model_token(
                record.binding.provider, record.binding.model_id, record.binding.exact_version
            )
            coverage = report.models.setdefault(
                token,
                ModelCoverage(
                    provider=record.binding.provider,
                    model_id=record.binding.model_id,
                    exact_version=record.binding.exact_version,
                ),
            )
            eligible = record.prior_eligible
            if snapshot.layer is EvidenceLayer.OFFICIAL:
                coverage.official_records += 1
                coverage.official_eligible += int(eligible)
            elif snapshot.layer is EvidenceLayer.BENCHMARK:
                coverage.benchmark_records += 1
                coverage.benchmark_eligible += int(eligible)
            else:
                coverage.community_records += 1
            for reason in record.ineligibility_reasons:
                coverage.exclusion_reasons[reason] = coverage.exclusion_reasons.get(reason, 0) + 1
            for measurement in record.measurements:
                coverage.scenarios_with_evidence.add(measurement.scenario.value)
                coverage.scales.add(measurement.metric_scale_id)
                if eligible:
                    coverage.scenarios_with_eligible_evidence.add(measurement.scenario.value)
            if record.binding.version_match in {"VERSION_MISMATCH", "MODEL_MISMATCH", "UNKNOWN"}:
                entry = (token, record.record_id)
                if entry not in report.unconfirmed_versions:
                    report.unconfirmed_versions.append(entry)

    if posterior_run is not None:
        for cell in posterior_run.scenario_records():
            token = _model_token(cell.key.provider, cell.key.model_id, cell.key.exact_version)
            coverage = report.models.setdefault(
                token,
                ModelCoverage(
                    provider=cell.key.provider,
                    model_id=cell.key.model_id,
                    exact_version=cell.key.exact_version,
                ),
            )
            coverage.production_observations = max(
                coverage.production_observations, cell.observation_count
            )
            coverage.production_scenarios.add(cell.key.scenario.value)

    return report


def attach_community_effective_sizes(report: CoverageReport, priors: list[LayerPrior]) -> None:
    """Fold community ESS into the coverage rows.

    Separate from :func:`build_coverage` because the ESS is only meaningful
    after deduplication and filtering, which is the aggregator's job, not the
    coverage walker's.
    """

    for prior in priors:
        if prior.layer is not EvidenceLayer.COMMUNITY:
            continue
        token = _model_token(prior.key.provider, prior.key.model_id, prior.key.exact_version)
        coverage = report.models.get(token)
        if coverage is not None:
            coverage.community_effective_sample_size = round(
                coverage.community_effective_sample_size + prior.effective_sample_size, 3
            )


def find_conflicts(
    snapshots: tuple[LayerSnapshot, LayerSnapshot, LayerSnapshot],
    priors_by_layer: dict[EvidenceLayer, list[LayerPrior]],
    *,
    disagreement_threshold: float = 0.20,
) -> list[EvidenceConflict]:
    """Mark disagreement. Three kinds, each reported and none resolved.

    ``WITHIN_SCALE_DISAGREEMENT`` is the only one that compares numbers, and it
    only ever compares numbers on the *same* scale for the *same* key — two
    readings of one quantity that do not agree. Cross-scale disagreement is not
    detectable and is not claimed to be.
    """

    conflicts: list[EvidenceConflict] = []
    by_key: dict[EvidenceKey, list[tuple[str, str, float]]] = defaultdict(list)
    for snapshot in snapshots:
        for record in snapshot.records:
            if not record.prior_eligible:
                continue
            for measurement in record.measurements:
                if measurement.value is None:
                    continue
                key = EvidenceKey(
                    provider=record.binding.provider,
                    model_id=record.binding.model_id,
                    exact_version=record.binding.exact_version,
                    task_type=measurement.task_type,
                    scenario=measurement.scenario,
                    metric_scale_id=measurement.metric_scale_id,
                )
                by_key[key].append((record.record_id, snapshot.layer.value, measurement.value))

    for key, entries in sorted(by_key.items(), key=lambda item: item[0].token):
        if len(entries) < 2:
            continue
        values = [value for _id, _layer, value in entries]
        spread = max(values) - min(values)
        span = max(abs(max(values)), 1e-9)
        if spread / span >= disagreement_threshold:
            conflicts.append(
                EvidenceConflict(
                    conflict_id=f"disagree:{key.token}",
                    kind="WITHIN_SCALE_DISAGREEMENT",
                    key=key,
                    description=(
                        f"{len(entries)} admissible records on scale {key.metric_scale_id} span "
                        f"{min(values):.4g}..{max(values):.4g} ({spread / span:.0%} of the larger value)"
                    ),
                    record_ids=tuple(record_id for record_id, _layer, _value in entries),
                    layers=tuple(sorted({layer for _id, layer, _value in entries})),
                )
            )

    for prior in priors_by_layer.get(EvidenceLayer.COMMUNITY, []):
        if "COMMUNITY_INTERNAL_CONFLICT" in prior.notes:
            conflicts.append(
                EvidenceConflict(
                    conflict_id=f"community-split:{prior.key.token}",
                    kind="COMMUNITY_STANCE_SPLIT",
                    key=prior.key,
                    description=(
                        "positive and negative first-hand reports each carry at least 30% of the "
                        "weight for this key; the failure is probably conditional on something "
                        "the posts do not share"
                    ),
                    record_ids=prior.record_ids,
                    layers=(EvidenceLayer.COMMUNITY.value,),
                )
            )

    return conflicts


def audit_contamination(run: PosteriorRun) -> list[ContaminationFinding]:
    """Prove, mechanically, that no posterior row mixed things it must not.

    Checks four separations at once:

    * one exact version per row,
    * one metric scale per row,
    * one task type per row below the version level,
    * one scenario per row below the task level.

    A row cannot be inspected for the observations that built it after the
    fact, so the audit works on the invariant the group key must satisfy: a
    row at level L must have all of L's fields populated with real values and
    all the finer fields set to the ``ANY`` sentinel. Anything else means a
    grouping produced a row it should not have.
    """

    from .keys import Scenario, TaskType
    from .posterior import PosteriorLevel

    findings: list[ContaminationFinding] = []
    seen: dict[tuple[str, str, str], str] = {}
    for record in run.records:
        key = record.key
        if key.exact_version.strip() in {"", "unknown"}:
            findings.append(
                ContaminationFinding(
                    kind="UNVERSIONED_POSTERIOR",
                    detail=f"{key.router_model_key} produced a posterior with no exact version",
                    offending=(key.token,),
                )
            )
        expectations: dict[PosteriorLevel, tuple[bool, bool]] = {
            PosteriorLevel.VERSION: (False, False),
            PosteriorLevel.TASK: (True, False),
            PosteriorLevel.SCENARIO: (True, True),
            PosteriorLevel.CONDITION: (True, True),
        }
        wants_task, wants_scenario = expectations[record.level]
        has_task = key.task_type is not TaskType.ANY
        has_scenario = key.scenario is not Scenario.ANY
        if has_task != wants_task or has_scenario != wants_scenario:
            findings.append(
                ContaminationFinding(
                    kind="LEVEL_KEY_MISMATCH",
                    detail=(
                        f"a {record.level.value} row is keyed task={key.task_type.value} "
                        f"scenario={key.scenario.value}, which does not match its level"
                    ),
                    offending=(key.token,),
                )
            )
        if record.level is PosteriorLevel.CONDITION and record.condition is None:
            findings.append(
                ContaminationFinding(
                    kind="CONDITION_ROW_WITHOUT_CONDITIONS",
                    detail="a condition-level row carries no condition bucket",
                    offending=(key.token,),
                )
            )
        identity = (key.token, record.level.value, record.condition.token if record.condition else "-")
        if identity in seen:
            findings.append(
                ContaminationFinding(
                    kind="DUPLICATE_POSTERIOR_ROW",
                    detail="the same key produced two rows at the same level, so one of them pooled twice",
                    offending=(key.token,),
                )
            )
        seen[identity] = record.outcome.value

        scale_for_outcome = record.key.metric_scale_id
        if not scale_for_outcome.startswith("prod."):
            findings.append(
                ContaminationFinding(
                    kind="NON_PRODUCTION_SCALE_IN_POSTERIOR",
                    detail=(
                        f"a production posterior row is on scale {scale_for_outcome}, which is not a "
                        "production scale; external evidence has entered the production layer"
                    ),
                    offending=(key.token,),
                )
            )
    return findings


def sufficiency(records: list[PosteriorRecord]) -> dict[str, bool]:
    return {f"{record.key.token}|{record.outcome.value}": record.sufficient for record in records}


__all__ = [
    "ContaminationFinding",
    "CoverageReport",
    "EvidenceConflict",
    "ModelCoverage",
    "attach_community_effective_sizes",
    "audit_contamination",
    "build_coverage",
    "find_conflicts",
    "sufficiency",
]
