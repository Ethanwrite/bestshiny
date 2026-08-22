from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class MemoryLayer(StrEnum):
    CANONICAL = "L0"
    TEMPORAL = "L1"
    EPISODIC = "L2"


class EvidencePurpose(StrEnum):
    """Declared use of embedding output at the narrative-memory boundary.

    Voyage-style embeddings are useful for retrieval and ranking, but cosine
    similarity is not an observation of a discrete story fact.  Forbidden
    purposes remain typed enum members so attempts to cross that boundary fail
    with a precise policy error instead of becoming an untyped string.
    """

    RETRIEVAL_HINT = "RETRIEVAL_HINT"
    SUPPORTING_SIMILARITY = "SUPPORTING_SIMILARITY"
    EVIDENCE_FRAME_RANKING = "EVIDENCE_FRAME_RANKING"
    IDENTITY_VERDICT = "IDENTITY_VERDICT"
    STATE_FACT_ASSERTION = "STATE_FACT_ASSERTION"
    STATE_DELTA_APPROVAL = "STATE_DELTA_APPROVAL"
    COMMIT_AUTHORIZATION = "COMMIT_AUTHORIZATION"


class AuthorityLevel(StrEnum):
    ADVISORY = "ADVISORY"
    AUTHORITATIVE = "AUTHORITATIVE"


ADVISORY_EVIDENCE_PURPOSES = frozenset(
    {
        EvidencePurpose.RETRIEVAL_HINT,
        EvidencePurpose.SUPPORTING_SIMILARITY,
        EvidencePurpose.EVIDENCE_FRAME_RANKING,
    }
)


def _enforce_advisory_use(
    evidence_purpose: EvidencePurpose,
    authority_level: AuthorityLevel,
) -> None:
    if authority_level is not AuthorityLevel.ADVISORY:
        raise ValueError("multimodal embedding evidence is advisory and cannot be authoritative")
    if evidence_purpose not in ADVISORY_EVIDENCE_PURPOSES:
        raise ValueError(
            f"multimodal embedding evidence is advisory and cannot be used for {evidence_purpose.value}"
        )


class MultimodalContent(BaseModel):
    text: str = ""
    image_urls: list[str] = Field(default_factory=list, max_length=16)
    video_urls: list[str] = Field(default_factory=list, max_length=4)
    evidence_purpose: EvidencePurpose = EvidencePurpose.RETRIEVAL_HINT
    authority_level: AuthorityLevel = AuthorityLevel.ADVISORY

    @model_validator(mode="after")
    def embeddings_are_advisory(self) -> MultimodalContent:
        _enforce_advisory_use(self.evidence_purpose, self.authority_level)
        return self


class ShotMemoryInput(BaseModel):
    project_id: str
    layer: MemoryLayer
    memory_type: str = Field(min_length=1, max_length=80)
    content: MultimodalContent
    entity_ids: list[str] = Field(default_factory=list, max_length=40)
    scene_id: str | None = None
    shot_id: str | None = None
    asset_version_ids: list[str] = Field(default_factory=list, max_length=40)
    temporal_position: float | None = None
    canonical: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def canonical_layer_is_truth(self) -> ShotMemoryInput:
        if self.layer == MemoryLayer.CANONICAL:
            self.canonical = True
        if not (self.content.text or self.content.image_urls or self.content.video_urls):
            raise ValueError("memory content cannot be empty")
        return self


class MemoryQuery(BaseModel):
    project_id: str
    text: str = ""
    image_urls: list[str] = Field(default_factory=list, max_length=8)
    video_urls: list[str] = Field(default_factory=list, max_length=2)
    entity_ids: list[str] = Field(default_factory=list, max_length=40)
    scene_id: str | None = None
    shot_id: str | None = None
    layers: list[MemoryLayer] = Field(
        default_factory=lambda: [MemoryLayer.CANONICAL, MemoryLayer.TEMPORAL, MemoryLayer.EPISODIC]
    )
    temporal_position: float | None = None
    top_k: int = Field(default=8, ge=1, le=50)
    evidence_purpose: EvidencePurpose = EvidencePurpose.RETRIEVAL_HINT
    authority_level: AuthorityLevel = AuthorityLevel.ADVISORY

    @model_validator(mode="after")
    def embeddings_are_advisory(self) -> MemoryQuery:
        _enforce_advisory_use(self.evidence_purpose, self.authority_level)
        return self


class RetrievedMemory(BaseModel):
    id: str
    project_id: str
    layer: MemoryLayer
    memory_type: str
    text: str
    image_urls: list[str]
    video_urls: list[str]
    entity_ids: list[str]
    scene_id: str | None
    shot_id: str | None
    asset_version_ids: list[str]
    canonical: bool
    score: float
    score_components: dict[str, float]
    metadata: dict[str, Any]
    evidence_purpose: EvidencePurpose = EvidencePurpose.RETRIEVAL_HINT
    authority_level: AuthorityLevel = AuthorityLevel.ADVISORY

    @model_validator(mode="after")
    def retrieval_is_advisory(self) -> RetrievedMemory:
        _enforce_advisory_use(self.evidence_purpose, self.authority_level)
        return self


class ContextBudget(BaseModel):
    max_characters: int = Field(default=12_000, ge=500)
    max_tokens: int = Field(default=3_000, ge=100)
    max_images: int = Field(default=8, ge=0, le=50)
    max_videos: int = Field(default=2, ge=0, le=10)


class GenerationContext(BaseModel):
    canonical_assets: list[dict[str, Any]] = Field(default_factory=list)
    temporal_state: dict[str, Any] = Field(default_factory=dict)
    shot_requirement: dict[str, Any] = Field(default_factory=dict)
    episodic_memories: list[RetrievedMemory] = Field(default_factory=list)
    world_rules: list[str] = Field(default_factory=list)
    canonical_asset_ids: list[str] = Field(default_factory=list)
    reference_images: list[str] = Field(default_factory=list)
    reference_videos: list[str] = Field(default_factory=list)
    previous_final_frame_asset_id: str | None = None
    assembled_text: str = ""
    omitted: list[str] = Field(default_factory=list)
    budget_used: dict[str, int] = Field(default_factory=dict)


class EmbeddingProvenance(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    model: str
    dimension: int
    input_type: Literal["query", "document"]
    evidence_purpose: EvidencePurpose = EvidencePurpose.RETRIEVAL_HINT
    authority_level: AuthorityLevel = AuthorityLevel.ADVISORY

    @model_validator(mode="after")
    def embeddings_are_advisory(self) -> EmbeddingProvenance:
        _enforce_advisory_use(self.evidence_purpose, self.authority_level)
        return self
