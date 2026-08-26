"""Loading the External Evidence Registry, and asking it what it knows.

The service deliberately exposes two different questions and keeps them apart:

- `record_for` returns everything on file for a model, including the evidence
  that must never move a score. That is the audit view.
- `prior_for` returns only what is eligible to move a score. That is the
  routing view, and it is a strict subset.

Anything that blurs those two is the failure this registry exists to prevent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .schemas import (
    PRIOR_ELIGIBLE_GRADES,
    PRIOR_ELIGIBLE_MATCHES,
    Binding,
    Evidence,
    ExternalEvidenceRegistry,
    Metric,
    Source,
)

DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "external-evidence" / "registry-v1.json"
)


@dataclass(frozen=True)
class EvidenceItem:
    """One metric, with everything needed to judge whether to believe it."""

    evidence_id: str
    binding: Binding
    evidence: Evidence
    metric: Metric
    grade: str
    sources: tuple[Source, ...]

    @property
    def prior_eligible(self) -> bool:
        return (
            self.grade in PRIOR_ELIGIBLE_GRADES
            and self.binding.version_match in PRIOR_ELIGIBLE_MATCHES
            and self.metric.mapping_confidence != "LOW"
        )

    @property
    def ineligibility_reasons(self) -> tuple[str, ...]:
        """Every reason this metric may not move a score, not just the first.

        A record is often ineligible twice over — a grade C source *and* a
        version mismatch — and reporting only one buries the other. The version
        mismatch is usually the more important of the two, because a grade can
        improve when a better source appears while a version mismatch never
        becomes true.
        """

        reasons: list[str] = []
        if self.grade not in PRIOR_ELIGIBLE_GRADES:
            reasons.append(f"SOURCE_GRADE_{self.grade}")
        if self.binding.version_match not in PRIOR_ELIGIBLE_MATCHES:
            reasons.append(self.binding.version_match)
        if self.metric.mapping_confidence == "LOW":
            reasons.append("MAPPING_CONFIDENCE_LOW")
        return tuple(reasons)


class ExternalEvidenceService:
    """Read-only access to a frozen registry version."""

    def __init__(self, registry: ExternalEvidenceRegistry):
        self.registry = registry
        self._sources = {item.source_id: item for item in registry.sources}
        self._evidence = {item.evidence_id: item for item in registry.evidence}

    @classmethod
    def load(cls, path: Path | str | None = None) -> ExternalEvidenceService:
        source = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
        return cls(ExternalEvidenceRegistry.model_validate(json.loads(source.read_text("utf-8"))))

    @property
    def version(self) -> str:
        return self.registry.registry_version

    def _grade_for(self, evidence: Evidence) -> str:
        """The weakest grade among the cited sources.

        A record that leans on one A source and one C source is a C record. The
        conservative direction is the only defensible one when the point of the
        grade is to decide whether a number may influence spending.
        """

        grades = [self._sources[name].grade for name in evidence.source_ids]
        return max(grades)  # "A" < "B" < "C" < "D" lexicographically

    def items_for(self, logical_name: str) -> list[EvidenceItem]:
        """Every metric on file for a model, eligible or not."""

        items: list[EvidenceItem] = []
        for binding in self.registry.bindings:
            if binding.logical_name != logical_name:
                continue
            evidence = self._evidence[binding.evidence_id]
            grade = self._grade_for(evidence)
            sources = tuple(self._sources[name] for name in evidence.source_ids)
            items.extend(
                EvidenceItem(binding.evidence_id, binding, evidence, metric, grade, sources)
                for metric in evidence.metrics
            )
        return items

    def prior_items_for(self, logical_name: str) -> list[EvidenceItem]:
        """Only the metrics that are allowed to move a routing score."""

        return [item for item in self.items_for(logical_name) if item.prior_eligible]

    def capabilities_with_prior(self, logical_name: str) -> set[str]:
        return {
            capability
            for item in self.prior_items_for(logical_name)
            for capability in item.metric.canonical_capability
        }

    def is_backed(self, logical_name: str) -> bool:
        return bool(self.prior_items_for(logical_name))

    def unbacked_model_names(self) -> set[str]:
        return {item.logical_name for item in self.registry.unbacked_models}

    def coverage(self) -> dict[str, dict[str, object]]:
        """What the registry can and cannot say about each bound model.

        Written for the operator endpoint, and for the answer to "why is this
        model's prior still a hand-authored number?".
        """

        report: dict[str, dict[str, object]] = {}
        for name in sorted({binding.logical_name for binding in self.registry.bindings}):
            items = self.items_for(name)
            eligible = [item for item in items if item.prior_eligible]
            report[name] = {
                "metrics_on_file": len(items),
                "prior_eligible_metrics": len(eligible),
                "capabilities_with_prior": sorted(
                    {c for item in eligible for c in item.metric.canonical_capability}
                ),
                "scenes_with_prior": sorted({item.metric.canonical_scene for item in eligible}),
                "excluded": sorted(
                    {
                        f"{item.evidence_id}:{'+'.join(item.ineligibility_reasons)}"
                        for item in items
                        if not item.prior_eligible
                    }
                ),
            }
        for item in self.registry.unbacked_models:
            report[item.logical_name] = {
                "metrics_on_file": 0,
                "prior_eligible_metrics": 0,
                "capabilities_with_prior": [],
                "scenes_with_prior": [],
                "excluded": [],
                "status": item.status,
                "note": item.note,
            }
        return report
