"""What makes two embeddings comparable at all.

Kept apart from both the service and the embedder because both need it, and
because it is a fact about vectors rather than about either of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from production_domain.models import StyleEmbedding

_FIELDS = (
    "provider",
    "model",
    "model_revision",
    "input_schema_version",
    "dimension",
    "normalization",
    "distance_metric",
)


@dataclass(frozen=True)
class EmbeddingSpaceIdentity:
    """Everything that has to match before two vectors may be compared.

    A similarity score is only meaningful inside one vector space. Change the
    model, its revision, the number of dimensions, how the stored vector is
    normalized, or the metric — and the *same* number means something else.
    Nothing about the comparison fails loudly on its own: cosine over two
    unrelated 1024-vectors returns a perfectly plausible 0.83.

    So the space is recorded with every vector, and compared before any score
    is taken. Any field differing means the two are not comparable at all.
    """

    provider: str
    model: str
    # What the provider echoed back for this call. Empty when it publishes no
    # revision, which is the case for every model wired here today — see
    # docs/OPEN_ISSUES.md 2.19 for what that leaves undetectable.
    model_revision: str
    # The producing code's own version. It changes when the frames, the request
    # shape or the aggregation change, all of which move the space.
    input_schema_version: str
    dimension: int
    normalization: str
    distance_metric: str

    @classmethod
    def from_embedding(cls, embedding: StyleEmbedding) -> EmbeddingSpaceIdentity:
        return cls(
            provider=embedding.provider,
            model=embedding.model,
            model_revision=embedding.model_revision,
            input_schema_version=embedding.algorithm_version,
            dimension=embedding.dimension,
            normalization=embedding.normalization,
            distance_metric=embedding.distance_metric,
        )

    def differences(self, other: EmbeddingSpaceIdentity) -> list[str]:
        """Field names that differ, for an evidence record that says *what* moved."""

        return [name for name in _FIELDS if getattr(self, name) != getattr(other, name)]
