"""Typed shape of the External Evidence Registry.

The registry answers one question — *what does the public record actually say
about the exact model version we are about to route to* — and it is built so
that the answer can be wrong out loud rather than quietly. Every number keeps
the scale it was measured on, every record names the source it came from, and
every binding to a model we run declares how well the versions match.

Nothing here fuses, averages or ranks across sources. A Likert 3.75, an Elo
1154 and a 0-1 automatic 0.939 are three different measurements of three
different things, and the registry's job is to keep them that way.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Grade = Literal["A", "B", "C", "D"]
MappingConfidence = Literal["HIGH", "MEDIUM", "LOW"]
VersionMatch = Literal[
    "EXACT",
    "EXACT_VERSION_UNSPECIFIED_REVISION",
    "VARIANT_MISMATCH",
    "VERSION_MISMATCH",
    "MODEL_MISMATCH",
]

# The only two match levels that may move a routing score. A variant or version
# mismatch is recorded rather than dropped, because the reason to keep it is
# exactly that someone will otherwise re-derive it from the same public source
# in six months and this time attach it to the wrong model.
PRIOR_ELIGIBLE_MATCHES: frozenset[str] = frozenset(
    {"EXACT", "EXACT_VERSION_UNSPECIFIED_REVISION"}
)
PRIOR_ELIGIBLE_GRADES: frozenset[str] = frozenset({"A", "B"})


class Source(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str | None = None
    publisher: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    published_at: str | None = None
    snapshot_at: str = Field(min_length=1)
    grade: Grade
    grade_rationale: str = Field(min_length=1)
    # A leaderboard is a reading, not a constant. Dynamic sources carry their
    # snapshot date into every downstream use.
    dynamic: bool = False


class Metric(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric_name: str = Field(min_length=1)
    # None where the source stated a result in words ("best", "preferred") and
    # published no number. Inventing one would be the whole failure mode.
    value: float | None = None
    canonical_scene: str = Field(min_length=1)
    canonical_capability: list[str] = Field(min_length=1)
    mapping_confidence: MappingConfidence
    mapping_rationale: str = Field(min_length=1)
    metric_scale_override: str | None = None


class EvidenceModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    model_id: str | None = None
    version: str = Field(min_length=1)
    revision: str | None = None
    provider: str = Field(min_length=1)


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    benchmark_name: str = Field(min_length=1)
    benchmark_version: str | None = None
    evaluation_method: str = Field(min_length=1)
    evaluator: str = Field(min_length=1)
    human_or_automatic: str = Field(min_length=1)
    # Sample size is a string as often as an int, because sources report
    # "1,000 T2V; 646 I2V" as readily as a single number. It is kept as stated.
    sample_size_prompts: int | str | None = None
    sample_size_runs: int | str | None = None
    human_eval_size: int | str | None = None
    confidence_interval: str | None = None
    comparison_models: list[str] = Field(default_factory=list)
    model: EvidenceModel
    modality: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    conditions: dict[str, object] = Field(default_factory=dict)
    metric_scale: str = Field(min_length=1)
    higher_is_better: bool
    metrics: list[Metric] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)
    notes: str | None = None


class Binding(BaseModel):
    """One piece of evidence, attached to one model this platform runs."""

    model_config = ConfigDict(frozen=True)

    logical_name: str = Field(min_length=1)
    provider_model_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    version_match: VersionMatch
    rationale: str = Field(min_length=1)
    note: str | None = None


class SupersededBy(BaseModel):
    """The canonical route that took over execution for a retired model."""

    model_config = ConfigDict(frozen=True)

    logical_name: str = Field(min_length=1)
    provider_model_id: str = Field(min_length=1)


class UnbackedModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    logical_name: str = Field(min_length=1)
    provider_model_id: str = Field(min_length=1)
    status: Literal["NO_EXTERNAL_EVIDENCE"]
    note: str = Field(min_length=1)
    # Execution and provenance are different facts. A model can stop being
    # executable without its verdict changing: what was looked for, in which
    # source, against which version, stays exactly as it was recorded. Retiring
    # the row instead would quietly rewrite history to match the current routing
    # table, which is the opposite of what an evidence registry is for.
    lifecycle: Literal["ACTIVE", "RETIRED"] = "ACTIVE"
    retired_on: str = ""
    retirement_reason: str = ""
    superseded_by: SupersededBy | None = None


class Gap(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: str = Field(min_length=1)
    canonical_scene: str = Field(min_length=1)
    status: Literal["INSUFFICIENT_EXTERNAL_EVIDENCE"]
    reason: str = Field(min_length=1)


class Conflict(BaseModel):
    model_config = ConfigDict(frozen=True)

    conflict_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    description: str = Field(min_length=1)
    resolution: str = Field(min_length=1)


class ExternalEvidenceRegistry(BaseModel):
    model_config = ConfigDict(frozen=True)

    registry_version: str = Field(min_length=1)
    frozen_at: str = Field(min_length=1)
    compiled_from: list[dict[str, str]] = Field(min_length=1)
    grade_definitions: dict[str, str]
    version_match_levels: dict[str, str]
    prior_eligibility_rule: str = Field(min_length=1)
    prohibitions: list[str] = Field(min_length=1)
    sources: list[Source] = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    bindings: list[Binding] = Field(min_length=1)
    unbacked_models: list[UnbackedModel] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_referential_integrity(self) -> ExternalEvidenceRegistry:
        source_ids = {item.source_id for item in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("duplicate source_id in the registry")
        evidence_ids = {item.evidence_id for item in self.evidence}
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("duplicate evidence_id in the registry")
        for item in self.evidence:
            missing = [name for name in item.source_ids if name not in source_ids]
            if missing:
                raise ValueError(f"{item.evidence_id} cites unknown source(s): {missing}")
        for binding in self.bindings:
            if binding.evidence_id not in evidence_ids:
                raise ValueError(
                    f"binding {binding.logical_name} -> {binding.evidence_id} cites unknown evidence"
                )
        backed = {binding.logical_name for binding in self.bindings}
        overlap = backed.intersection(item.logical_name for item in self.unbacked_models)
        if overlap:
            raise ValueError(f"model(s) listed as both backed and unbacked: {sorted(overlap)}")
        return self
