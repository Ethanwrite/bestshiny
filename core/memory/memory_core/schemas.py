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


class VideoFrameStatus(StrEnum):
    """Whether the frames a video memory should have been built from exist."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    EXTRACTED = "EXTRACTED"
    UNAVAILABLE = "UNAVAILABLE"


class VideoFrameReference(BaseModel):
    """One still frame an embedding was actually built from.

    The embedding provider never sends a video to the vendor; it sends stills
    taken at fixed positions.  Recording where each still came from is what
    lets a retrieved memory say which moments of which video it represents,
    rather than implying the whole clip was understood.
    """

    #: The content-addressed media URL the frame was sampled from. The memory
    #: row's `asset_version_ids` carry the logical version binding.
    source_video_url: str
    frame_index: int = Field(ge=0)
    normalized_position: float = Field(ge=0.0, le=1.0)
    timestamp_seconds: float = Field(ge=0.0)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    byte_length: int = Field(ge=1)


class VideoFrameLineage(BaseModel):
    """Provenance for the frames one embedding call was built from."""

    sampler_version: str = ""
    status: VideoFrameStatus = VideoFrameStatus.NOT_APPLICABLE
    source_video_urls: list[str] = Field(default_factory=list)
    frames: list[VideoFrameReference] = Field(default_factory=list)
    #: Why frames are missing or fewer than the fixed positions asked for.
    reason_codes: list[str] = Field(default_factory=list)
    total_pixels: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)


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


class EpisodeScope(StrEnum):
    """How far a retrieval may reach across a series.

    A 60-episode series makes the two directions genuinely different questions.
    `EPISODE` answers "what happened in the episode I am shooting" and must not
    surface a neighbouring episode's beat as if it were current. `SERIES`
    answers "what has this series established", and needs the current episode to
    outrank a distant one rather than being flattened by cosine similarity.
    """

    EPISODE = "EPISODE"
    SERIES = "SERIES"


class MemoryQuery(BaseModel):
    project_id: str
    text: str = ""
    image_urls: list[str] = Field(default_factory=list, max_length=8)
    video_urls: list[str] = Field(default_factory=list, max_length=2)
    entity_ids: list[str] = Field(default_factory=list, max_length=40)
    episode_id: str | None = None
    scene_id: str | None = None
    shot_id: str | None = None
    # Meaningful only together with `episode_id`; without one there is no
    # current episode to scope to or to rank against.
    episode_scope: EpisodeScope = EpisodeScope.EPISODE
    layers: list[MemoryLayer] = Field(
        default_factory=lambda: [MemoryLayer.CANONICAL, MemoryLayer.TEMPORAL, MemoryLayer.EPISODIC]
    )
    temporal_position: float | None = None
    # Age at which an undated memory's recency contribution halves. Retrieval
    # previously hard-coded 30 days, which is a reasonable single-episode shoot
    # and a poor series that ran for a year.
    recency_half_life_days: float = Field(default=30.0, gt=0, le=3_650)
    top_k: int = Field(default=8, ge=1, le=50)
    evidence_purpose: EvidencePurpose = EvidencePurpose.RETRIEVAL_HINT
    authority_level: AuthorityLevel = AuthorityLevel.ADVISORY

    @model_validator(mode="after")
    def embeddings_are_advisory(self) -> MemoryQuery:
        _enforce_advisory_use(self.evidence_purpose, self.authority_level)
        if self.episode_scope is EpisodeScope.SERIES and self.episode_id is None:
            # Series scope ranks *relative to* a current episode. Without one it
            # would silently behave like an unscoped query.
            raise ValueError("episode_scope=SERIES requires episode_id")
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
    episode_id: str | None = None
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


class ContextSegmentSource(StrEnum):
    """Why one assembled context segment is present.

    ``EXPLICIT_DEPENDENCY`` and ``OPEN_OBLIGATION`` are forced (stage one of
    retrieval) and may not be dropped by the budget; ``SIMILARITY`` is the
    supplementary stage-two material and is the first thing the budget sheds.
    """

    CANONICAL = "CANONICAL"
    TEMPORAL_STATE = "TEMPORAL_STATE"
    SHOT_REQUIREMENT = "SHOT_REQUIREMENT"
    EXPLICIT_DEPENDENCY = "EXPLICIT_DEPENDENCY"
    OPEN_OBLIGATION = "OPEN_OBLIGATION"
    SIMILARITY = "SIMILARITY"
    WORLD_RULES = "WORLD_RULES"


FORCED_SEGMENT_SOURCES = frozenset(
    {ContextSegmentSource.EXPLICIT_DEPENDENCY, ContextSegmentSource.OPEN_OBLIGATION}
)


class DependencySegment(BaseModel):
    """One forced context segment carrying explicit narrative material."""

    key: str = Field(min_length=1, max_length=200)
    source_reason: ContextSegmentSource
    text: str
    dependency_type: str | None = None
    source_shot_id: str | None = None
    fact_key: str | None = None
    obligation_key: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def forced_reasons_only(self) -> DependencySegment:
        if self.source_reason not in FORCED_SEGMENT_SOURCES:
            raise ValueError(
                "dependency segments carry EXPLICIT_DEPENDENCY or OPEN_OBLIGATION provenance"
            )
        return self

    def render(self) -> dict[str, Any]:
        rendered: dict[str, Any] = {"summary": self.text}
        if self.dependency_type:
            rendered["dependency_type"] = self.dependency_type
        if self.source_shot_id:
            rendered["source_shot_id"] = self.source_shot_id
        if self.fact_key:
            rendered["fact_key"] = self.fact_key
        if self.obligation_key:
            rendered["obligation_key"] = self.obligation_key
        if self.payload:
            rendered["payload"] = self.payload
        return rendered


class GenerationContext(BaseModel):
    canonical_assets: list[dict[str, Any]] = Field(default_factory=list)
    temporal_state: dict[str, Any] = Field(default_factory=dict)
    shot_requirement: dict[str, Any] = Field(default_factory=dict)
    dependency_segments: list[DependencySegment] = Field(default_factory=list)
    episodic_memories: list[RetrievedMemory] = Field(default_factory=list)
    world_rules: list[str] = Field(default_factory=list)
    canonical_asset_ids: list[str] = Field(default_factory=list)
    reference_images: list[str] = Field(default_factory=list)
    reference_videos: list[str] = Field(default_factory=list)
    previous_final_frame_asset_id: str | None = None
    assembled_text: str = ""
    omitted: list[str] = Field(default_factory=list)
    #: One entry per assembled section: {"label": ..., "source_reason": ...}.
    #: The provenance of every context segment stays auditable after the fact.
    segment_provenance: list[dict[str, str]] = Field(default_factory=list)
    budget_used: dict[str, int] = Field(default_factory=dict)


class EmbeddingProvenance(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    model: str
    dimension: int
    input_type: Literal["query", "document"]
    evidence_purpose: EvidencePurpose = EvidencePurpose.RETRIEVAL_HINT
    authority_level: AuthorityLevel = AuthorityLevel.ADVISORY
    #: Present only where the embedded content carried video. `None` means the
    #: call had no video input, not that extraction was skipped.
    video_frame_lineage: VideoFrameLineage | None = None

    @model_validator(mode="after")
    def embeddings_are_advisory(self) -> EmbeddingProvenance:
        _enforce_advisory_use(self.evidence_purpose, self.authority_level)
        return self
