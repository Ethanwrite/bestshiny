"""The four layers, and the walls between them.

    official_prior       what the vendor says about its own model
    benchmark_prior      what a third party measured, with a stated protocol
    community_prior      what practitioners report having experienced
    production_posterior what this platform actually observed

They are separate because they fail differently. A vendor's own number is
optimistic in a predictable direction. A benchmark is honest about a task that
may not be yours. A community report is a real observation of a real failure
with an unknown denominator. Only the fourth is measured on this platform's own
traffic, and it is the only one that describes what a user here will get.

Averaging them would produce a number with no interpretation at all, so the
layers never merge. They are carried side by side, each with its own weight and
its own ceiling, and the posterior treats the first three as *priors with
bounded strength* — never as observations.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class EvidenceLayer(StrEnum):
    OFFICIAL = "official_prior"
    BENCHMARK = "benchmark_prior"
    COMMUNITY = "community_prior"
    PRODUCTION = "production_posterior"


#: The three layers that are *external* to this platform. They live in frozen
#: files under ``config/router-evidence/``; production observations live in the
#: database. That is the physical separation the mandate asks for, and it is
#: the reason no query can accidentally return both.
EXTERNAL_LAYERS: Final[frozenset[EvidenceLayer]] = frozenset(
    {EvidenceLayer.OFFICIAL, EvidenceLayer.BENCHMARK, EvidenceLayer.COMMUNITY}
)


class LayerIsolationError(RuntimeError):
    """Raised when two things that must not meet were about to meet."""


class SourceClassMismatch(LayerIsolationError):
    """A record was filed under a layer its source class does not belong to."""


#: Source types, by the layer that is allowed to hold them. A Reddit thread is
#: not a benchmark however carefully it was written up, and a vendor blog post
#: reporting the vendor's own eval is not independent however many numbers it
#: contains.
LAYER_SOURCE_TYPES: Final[dict[EvidenceLayer, frozenset[str]]] = {
    EvidenceLayer.OFFICIAL: frozenset(
        {
            "official_technical_report",
            "official_benchmark",
            "official_model_card",
            "official_docs",
            "official_pricing",
            "official_release",
            "official_changelog",
        }
    ),
    EvidenceLayer.BENCHMARK: frozenset(
        {
            "academic_paper",
            "independent_benchmark",
            "arena_leaderboard",
            "third_party_benchmark",
        }
    ),
    EvidenceLayer.COMMUNITY: frozenset(
        {
            "reddit",
            "x",
            "github_issue",
            "github_discussion",
            "huggingface_discussion",
            "discord",
            "forum",
            "creator_comparison",
        }
    ),
}

_SOURCE_TYPE_TO_LAYER: Final[dict[str, EvidenceLayer]] = {
    source_type: layer for layer, types in LAYER_SOURCE_TYPES.items() for source_type in types
}


def layer_for_source_type(source_type: str) -> EvidenceLayer:
    try:
        return _SOURCE_TYPE_TO_LAYER[source_type]
    except KeyError:  # pragma: no cover - guarded by the ingest validator
        raise SourceClassMismatch(
            f"source type {source_type!r} belongs to no layer; add it to LAYER_SOURCE_TYPES "
            "deliberately rather than letting an unclassified source in"
        ) from None


def require_layer(source_type: str, expected: EvidenceLayer) -> None:
    actual = layer_for_source_type(source_type)
    if actual is not expected:
        raise SourceClassMismatch(
            f"source type {source_type!r} is {actual.value} evidence and cannot be filed as {expected.value}"
        )


class PriorStrength(BaseModel):
    """How much a layer is allowed to say, expressed in pseudo-observations.

    The ceiling matters more than the weight. Without it, a benchmark measured
    on a thousand prompts would dominate the first hundred real generations on
    this platform forever, and the router would keep believing a number that
    production had already contradicted.
    """

    model_config = ConfigDict(frozen=True)

    layer: EvidenceLayer
    #: Pseudo-count contributed by a single eligible record.
    per_record: float = Field(gt=0)
    #: Hard ceiling on the layer's total pseudo-count for one key, whatever the
    #: number of records. Production data always overtakes it eventually.
    ceiling: float = Field(gt=0)

    def contribution(self, record_count: int) -> float:
        return min(self.ceiling, self.per_record * max(0, record_count))


#: Deliberately conservative and deliberately ordered. Official claims are the
#: weakest because they are the least adversarial; benchmarks are strongest of
#: the three because they publish a protocol; community evidence sits between
#: them because it is real experience with an unknown denominator.
#:
#: The ceilings are small on purpose. Eight pseudo-observations is roughly
#: "worth about eight real generations", which is the honest exchange rate for
#: a number measured on someone else's prompts.
DEFAULT_PRIOR_STRENGTHS: Final[dict[EvidenceLayer, PriorStrength]] = {
    EvidenceLayer.OFFICIAL: PriorStrength(layer=EvidenceLayer.OFFICIAL, per_record=1.0, ceiling=4.0),
    EvidenceLayer.BENCHMARK: PriorStrength(layer=EvidenceLayer.BENCHMARK, per_record=2.0, ceiling=8.0),
    EvidenceLayer.COMMUNITY: PriorStrength(layer=EvidenceLayer.COMMUNITY, per_record=0.5, ceiling=3.0),
}


__all__ = [
    "DEFAULT_PRIOR_STRENGTHS",
    "EXTERNAL_LAYERS",
    "LAYER_SOURCE_TYPES",
    "EvidenceLayer",
    "LayerIsolationError",
    "PriorStrength",
    "SourceClassMismatch",
    "layer_for_source_type",
    "require_layer",
]
