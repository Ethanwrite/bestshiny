"""Record shapes for the three external layers.

Every field here exists because leaving it out would let a number arrive
without the thing that makes it checkable. The mandate is explicit about the
minimum — source URL, publication time, crawl time, the original fragment,
model version, scenario, credibility, provenance — and the schema is where
"we should record that" becomes "a record without it does not validate".

Two rules are enforced by the types rather than by discipline:

* a numeric claim must be accompanied by the fragment it came from
  (``verbatim_quote``), and
* a sample size, a confidence interval or a version mapping may only be
  present when it was *stated by the source*, never inferred.

The second is the one that matters for a research assistant. A model asked to
find benchmark results will happily produce a plausible n=1000 for a page that
never gave one, and a plausible number is indistinguishable from a real one at
the point of use. So each of those three fields is paired with a
``*_stated_by_source`` boolean, and the ingest validator refuses the record if
the value is present and the boolean is false.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .keys import ReferenceMode, Scenario, TaskType
from .layers import EvidenceLayer, SourceClassMismatch, layer_for_source_type

Credibility = Literal["A", "B", "C", "D"]

VersionMatch = Literal[
    "EXACT",
    "EXACT_VERSION_UNSPECIFIED_REVISION",
    "VARIANT_MISMATCH",
    "VERSION_MISMATCH",
    "MODEL_MISMATCH",
    "UNKNOWN",
]

#: Grade C is admissible in the community layer and nowhere else. A benchmark
#: on a grade C source is a screenshot of a number and should not move a prior;
#: a practitioner's report is *inherently* grade C — one person, one venue, no
#: protocol — and holding it to the benchmark bar would exclude the entire
#: layer while leaving it in the file, which is worse than not gathering it.
#: Credibility still multiplies the weight (see ``community.CREDIBILITY_WEIGHT``),
#: so a C post counts for half of a B one. Grade D stays out everywhere.
PRIOR_ELIGIBLE_CREDIBILITY_COMMUNITY: frozenset[str] = frozenset({"A", "B", "C"})

#: Only these two may influence a prior, and only from grade A or B sources.
#: Identical to the rule the frozen External Evidence Registry already applies,
#: restated here because this package must not import that one's constants and
#: then quietly diverge from them; ``test_router_evidence_layers.py`` asserts
#: the two definitions still agree.
PRIOR_ELIGIBLE_MATCHES: frozenset[str] = frozenset({"EXACT", "EXACT_VERSION_UNSPECIFIED_REVISION"})
PRIOR_ELIGIBLE_CREDIBILITY: frozenset[str] = frozenset({"A", "B"})


class Provenance(BaseModel):
    """Where a record came from and when it was seen.

    ``retrieved_at`` is separate from ``published_at`` because a leaderboard is
    a reading rather than a constant: the same URL says something different
    next month, and a record that carries only a publication date cannot be
    told apart from a stale one.
    """

    model_config = ConfigDict(frozen=True)

    source_url: str | None = Field(default=None, max_length=2048)
    source_type: str = Field(min_length=1, max_length=60)
    publisher: str = Field(min_length=1, max_length=200)
    published_at: str | None = Field(default=None, max_length=40)
    retrieved_at: datetime
    retrieved_by: str = Field(min_length=1, max_length=80)
    dynamic: bool = False
    #: The exact words the claim rests on. Not a paraphrase: the point is that a
    #: reader can decide for themselves whether the number was really stated.
    verbatim_quote: str = Field(min_length=1, max_length=4000)
    summary: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _url_or_reason(self) -> Provenance:
        if self.source_url is None and self.source_type not in {"discord", "forum"}:
            raise ValueError(
                f"{self.source_type} evidence must carry a source_url; only closed venues "
                "(discord, forum) may be recorded without one"
            )
        return self


class ModelBinding(BaseModel):
    """The claim that this record is about a model we actually run.

    It is a claim, not a fact, which is why it carries its own confidence and
    its own match level. The most common way external evidence goes wrong is
    not a wrong number — it is a right number attached to the wrong snapshot.
    """

    model_config = ConfigDict(frozen=True)

    logical_name: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=80)
    model_id: str = Field(min_length=1, max_length=160)
    #: What the *source* named. Kept verbatim so a mismatch stays visible.
    source_model_name: str = Field(min_length=1, max_length=200)
    source_model_version: str | None = Field(default=None, max_length=120)
    #: What we run. Never inferred from the source's wording.
    exact_version: str = Field(min_length=1, max_length=120)
    version_match: VersionMatch
    version_match_stated_by_source: bool = False
    mapping_confidence: Literal["HIGH", "MEDIUM", "LOW"]
    mapping_rationale: str = Field(min_length=1, max_length=1200)
    is_alias: bool = False

    @model_validator(mode="after")
    def _exact_needs_a_stated_version(self) -> ModelBinding:
        if self.version_match == "EXACT" and self.source_model_version is None:
            raise ValueError(
                "version_match=EXACT requires the source to have named a version; "
                "use EXACT_VERSION_UNSPECIFIED_REVISION when it did not"
            )
        return self


class Measurement(BaseModel):
    """One number, on one scale, for one scene.

    ``value`` is optional because sources routinely state a result in words —
    "clearly the best at physics" — and publish no number. That is real
    evidence about a stance and no evidence at all about a magnitude, so it is
    recorded with a null value rather than given one.
    """

    model_config = ConfigDict(frozen=True)

    metric_name: str = Field(min_length=1, max_length=160)
    value: float | None = None
    metric_scale_id: str = Field(min_length=1, max_length=80)
    scenario: Scenario
    task_type: TaskType
    reference_mode: ReferenceMode = ReferenceMode.NONE
    higher_is_better: bool = True
    scenario_mapping_confidence: Literal["HIGH", "MEDIUM", "LOW"] = "MEDIUM"
    scenario_mapping_rationale: str = Field(min_length=1, max_length=1200)


class SampleStatistics(BaseModel):
    """Sample size and interval, only ever as the source stated them.

    Each value is paired with the assertion that the source said it. A research
    assistant that fills in a believable ``n`` is the single most damaging
    failure mode available to this pipeline, because the number then travels
    with the authority of a measurement it never had.
    """

    model_config = ConfigDict(frozen=True)

    sample_size: int | str | None = None
    sample_size_stated_by_source: bool = False
    human_eval_size: int | str | None = None
    human_eval_size_stated_by_source: bool = False
    confidence_interval: str | None = Field(default=None, max_length=200)
    confidence_interval_stated_by_source: bool = False

    @model_validator(mode="after")
    def _no_invented_statistics(self) -> SampleStatistics:
        problems: list[str] = []
        if self.sample_size is not None and not self.sample_size_stated_by_source:
            problems.append("sample_size")
        if self.human_eval_size is not None and not self.human_eval_size_stated_by_source:
            problems.append("human_eval_size")
        if self.confidence_interval is not None and not self.confidence_interval_stated_by_source:
            problems.append("confidence_interval")
        if problems:
            raise ValueError(
                "these values are present but not attributed to the source: "
                + ", ".join(problems)
                + " — an inferred sample size or interval is a fabrication, drop the value instead"
            )
        return self


class ExternalRecord(BaseModel):
    """Base shape shared by the three external layers."""

    model_config = ConfigDict(frozen=True)

    record_id: str = Field(min_length=1, max_length=120)
    layer: EvidenceLayer
    provenance: Provenance
    binding: ModelBinding
    measurements: list[Measurement] = Field(min_length=1)
    credibility: Credibility
    credibility_rationale: str = Field(min_length=1, max_length=1200)
    conditions: dict[str, str | int | float | bool] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _source_type_matches_layer(self) -> ExternalRecord:
        try:
            actual = layer_for_source_type(self.provenance.source_type)
        except SourceClassMismatch as error:
            # Pydantic only wraps ValueError. Letting the isolation error
            # escape turns "this record named a source type we do not
            # recognise" — an ordinary, expected research mistake — into a
            # crash that takes the whole ingest run with it.
            raise ValueError(str(error)) from error
        if actual is not self.layer:
            raise ValueError(
                f"record {self.record_id} is filed as {self.layer.value} but its source type "
                f"{self.provenance.source_type!r} is {actual.value} evidence"
            )
        return self

    @property
    def prior_eligible(self) -> bool:
        """Whether this record may move a prior at all.

        Ineligible records are kept, not deleted. The reason is unchanged from
        the frozen registry: a deleted near-miss gets re-derived from the same
        public page in six months and attached silently to the wrong version.
        """

        return (
            self.credibility in PRIOR_ELIGIBLE_CREDIBILITY
            and self.binding.version_match in PRIOR_ELIGIBLE_MATCHES
            and self.binding.mapping_confidence != "LOW"
            and not self.binding.is_alias
        )

    @property
    def ineligibility_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.credibility not in PRIOR_ELIGIBLE_CREDIBILITY:
            reasons.append(f"CREDIBILITY_{self.credibility}")
        if self.binding.version_match not in PRIOR_ELIGIBLE_MATCHES:
            reasons.append(self.binding.version_match)
        if self.binding.mapping_confidence == "LOW":
            reasons.append("MAPPING_CONFIDENCE_LOW")
        if self.binding.is_alias:
            reasons.append("ALIAS_NOT_SNAPSHOT")
        return tuple(reasons)


class OfficialRecord(ExternalRecord):
    """A vendor's statement about its own model.

    ``claim_kind`` separates a measured number from a marketing adjective from
    a hard capability limit. The third is the most useful and the least
    glamorous: a documented maximum duration is a fact the router can act on,
    whereas "unprecedented realism" is not evidence of anything.
    """

    layer: Literal[EvidenceLayer.OFFICIAL] = EvidenceLayer.OFFICIAL
    claim_kind: Literal["measured", "qualitative_claim", "capability_limit", "pricing", "parameter"]
    self_reported: Literal[True] = True


class BenchmarkRecord(ExternalRecord):
    """A third-party measurement with a stated protocol."""

    layer: Literal[EvidenceLayer.BENCHMARK] = EvidenceLayer.BENCHMARK
    benchmark_name: str = Field(min_length=1, max_length=200)
    benchmark_version: str | None = Field(default=None, max_length=80)
    evaluation_method: str = Field(min_length=1, max_length=600)
    evaluator: str = Field(min_length=1, max_length=200)
    human_or_automatic: Literal["human", "automatic", "mixed", "unstated"]
    statistics: SampleStatistics = Field(default_factory=SampleStatistics)
    comparison_models: list[str] = Field(default_factory=list)
    #: Leaderboards move. A benchmark record without a protocol *and* without a
    #: snapshot date is a screenshot, and is graded accordingly at ingest.
    protocol_url: str | None = Field(default=None, max_length=2048)


class CommunityRecord(ExternalRecord):
    """One practitioner's report, labelled well enough to be counted carefully.

    This is the layer with the most volume and the least structure, so it
    carries the most labelling. The fields exist to answer, for each report:
    is this a person who ran it themselves, or someone repeating what they
    read; are they describing this exact version; is this an opinion or a
    reproducible failure; and is this the same person saying the same thing for
    the fourth time.
    """

    layer: Literal[EvidenceLayer.COMMUNITY] = EvidenceLayer.COMMUNITY
    author_handle: str = Field(min_length=1, max_length=200)
    #: Stable hash of the venue + author, so the same person can be recognised
    #: across posts without the handle itself becoming a join key elsewhere.
    author_key: str = Field(min_length=1, max_length=120)
    venue: str = Field(min_length=1, max_length=120)
    stance: Literal["positive", "negative", "mixed", "neutral"]
    experience: Literal["firsthand", "paraphrased", "secondhand", "unclear"]
    failure_modes: list[str] = Field(default_factory=list)
    #: Engagement is recorded but never used as a weight. A viral post is not a
    #: larger sample; it is one observation that many people saw.
    engagement: dict[str, int] = Field(default_factory=dict)
    #: How many generations *the author says* they ran. Same rule as benchmarks:
    #: absent unless stated.
    reported_generation_count: int | None = None
    reported_generation_count_stated_by_source: bool = False
    spam_signals: list[str] = Field(default_factory=list)
    is_marketing: bool = False
    is_bot_suspected: bool = False
    duplicate_of: str | None = Field(default=None, max_length=120)
    content_hash: str = Field(min_length=8, max_length=64)

    @model_validator(mode="after")
    def _no_invented_counts(self) -> CommunityRecord:
        if self.reported_generation_count is not None and not self.reported_generation_count_stated_by_source:
            raise ValueError(
                "reported_generation_count is present but not attributed to the author; "
                "a guessed count turns one anecdote into a sample size"
            )
        return self

    @property
    def prior_eligible(self) -> bool:
        """Community evidence has three extra ways to be inadmissible.

        Marketing, suspected automation and known duplicates are excluded
        before anything else is considered, because each of them inflates a
        count without adding an observation.
        """

        if self.is_marketing or self.is_bot_suspected or self.duplicate_of is not None:
            return False
        if self.experience not in {"firsthand", "paraphrased"}:
            return False
        return (
            self.credibility in PRIOR_ELIGIBLE_CREDIBILITY_COMMUNITY
            and self.binding.version_match in PRIOR_ELIGIBLE_MATCHES
            and self.binding.mapping_confidence != "LOW"
            and not self.binding.is_alias
        )

    @property
    def ineligibility_reasons(self) -> tuple[str, ...]:
        reasons = [
            reason
            for reason in super().ineligibility_reasons
            # The base class applies the benchmark credibility bar; the
            # community layer's bar is one grade lower, so a C that the base
            # class objected to is not a reason here.
            if reason != "CREDIBILITY_C"
        ]
        if self.credibility not in PRIOR_ELIGIBLE_CREDIBILITY_COMMUNITY:
            reasons.append(f"CREDIBILITY_{self.credibility}")
        if self.is_marketing:
            reasons.append("MARKETING")
        if self.is_bot_suspected:
            reasons.append("BOT_SUSPECTED")
        if self.duplicate_of is not None:
            reasons.append("DUPLICATE")
        if self.experience not in {"firsthand", "paraphrased"}:
            reasons.append(f"EXPERIENCE_{self.experience.upper()}")
        return tuple(reasons)


__all__ = [
    "PRIOR_ELIGIBLE_CREDIBILITY",
    "PRIOR_ELIGIBLE_CREDIBILITY_COMMUNITY",
    "PRIOR_ELIGIBLE_MATCHES",
    "BenchmarkRecord",
    "CommunityRecord",
    "Credibility",
    "ExternalRecord",
    "Measurement",
    "ModelBinding",
    "OfficialRecord",
    "Provenance",
    "SampleStatistics",
    "VersionMatch",
]
