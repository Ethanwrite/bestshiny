from __future__ import annotations

import math
from datetime import UTC, datetime

from platform_database import Database
from production_domain.models import (
    Asset,
    AssetVersion,
    DecisionRecord,
    Episode,
    Project,
    Scene,
    Shot,
    ShotMemory,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from .embedding import EmbeddingProvider, EmbeddingVector, MemoryEmbeddingUnavailable
from .schemas import (
    ADVISORY_EVIDENCE_PURPOSES,
    AuthorityLevel,
    EpisodeScope,
    EvidencePurpose,
    MemoryLayer,
    MemoryQuery,
    MultimodalContent,
    RetrievedMemory,
    ShotMemoryInput,
)

# The provider recorded on a memory row whose vector could not be produced.
# The column is NOT NULL and must not carry a real provider's name: retrieval
# matches on it, so a degraded row can never come back as if it were a Voyage
# embedding, and an operator can count them.
DEGRADED_EMBEDDING_PROVIDER = "unavailable"


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    denominator = math.sqrt(sum(item * item for item in left)) * math.sqrt(sum(item * item for item in right))
    if not denominator:
        return 0.0
    return max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True)) / denominator))


class MultimodalMemoryEngine:
    """Project-scoped L0/L1/L2 retrieval with metadata filtering before similarity."""

    version = "multimodal-memory-v1"

    def __init__(self, database: Database, embeddings: EmbeddingProvider, *, enabled: bool = False):
        self.database = database
        self.embeddings = embeddings
        self.enabled = enabled

    @staticmethod
    def _validate_project_links(session: Session, value: ShotMemoryInput) -> None:
        if session.get(Project, value.project_id) is None:
            raise LookupError("project not found")
        if value.scene_id is not None:
            scene_project_id = session.scalar(
                select(Episode.project_id)
                .join(Scene, Scene.episode_id == Episode.id)
                .where(Scene.id == value.scene_id)
            )
            if scene_project_id is None:
                raise LookupError("scene not found")
            if scene_project_id != value.project_id:
                raise ValueError("scene belongs to a different project")
        if value.shot_id is not None:
            shot_project_id = session.scalar(
                select(Episode.project_id)
                .join(Scene, Scene.episode_id == Episode.id)
                .join(Shot, Shot.scene_id == Scene.id)
                .where(Shot.id == value.shot_id)
            )
            if shot_project_id is None:
                raise LookupError("shot not found")
            if shot_project_id != value.project_id:
                raise ValueError("shot belongs to a different project")
        if value.asset_version_ids:
            requested_ids = set(value.asset_version_ids)
            rows = session.execute(
                select(AssetVersion.id, Asset.project_id)
                .join(Asset, Asset.id == AssetVersion.asset_id)
                .where(AssetVersion.id.in_(requested_ids))
            ).all()
            found_ids = {version_id for version_id, _project_id in rows}
            missing_ids = sorted(requested_ids - found_ids)
            if missing_ids:
                raise LookupError(f"asset version not found: {missing_ids[0]}")
            if any(project_id != value.project_id for _version_id, project_id in rows):
                raise ValueError("asset version belongs to a different project")

    def index(self, value: ShotMemoryInput) -> ShotMemory:
        with self.database.session() as session:
            self._validate_project_links(session, value)
        embedded: EmbeddingVector | None
        try:
            embedded = self.embeddings.embed_with_provenance(
                value.content,
                input_type="document",
                project_id=value.project_id,
            )
        except MemoryEmbeddingUnavailable as exc:
            # Vector memory is advisory, and indexing runs *after* the asset is
            # promoted and Canon is written. An embedding outage therefore
            # records a degradation and keeps the structurally retrievable row,
            # exactly as `search` degrades to the structured timeline. It never
            # rolls anything back and never fails the caller's request.
            self._record_vector_degraded(value.project_id, exc)
            embedded = None
        vector = list(embedded.values) if embedded is not None else []
        provenance = embedded.provenance if embedded is not None else None
        with self.database.session() as session:
            # Revalidate after the external embedding call so deleted or reassigned
            # associations cannot be persisted through the JSON version references.
            self._validate_project_links(session, value)
            metadata = dict(value.metadata)
            # These keys are server-owned policy facts. Caller metadata cannot
            # relabel an advisory similarity vector as decision authority.
            if provenance is not None:
                metadata["evidence_purpose"] = provenance.evidence_purpose.value
                metadata["authority_level"] = provenance.authority_level.value
                if provenance.video_frame_lineage is not None:
                    # Which frames of which video this memory stands for, so a
                    # retrieved memory can say what it was built from.
                    metadata["video_frame_lineage"] = provenance.video_frame_lineage.model_dump(
                        mode="json"
                    )
            else:
                metadata["evidence_purpose"] = value.content.evidence_purpose.value
                metadata["authority_level"] = value.content.authority_level.value
                metadata["vector_degraded"] = True
                metadata["degradation_reason_codes"] = ["MEMORY_VECTOR_DEGRADED"]
            memory = ShotMemory(
                project_id=value.project_id,
                layer=value.layer.value,
                memory_type=value.memory_type,
                text_content=value.content.text,
                image_urls=value.content.image_urls,
                video_urls=value.content.video_urls,
                entity_ids=value.entity_ids,
                scene_id=value.scene_id,
                shot_id=value.shot_id,
                asset_version_ids=value.asset_version_ids,
                temporal_position=value.temporal_position,
                canonical=value.canonical,
                embedding=vector,
                embedding_dimension=len(vector),
                embedding_provider=(
                    provenance.provider if provenance is not None else DEGRADED_EMBEDDING_PROVIDER
                ),
                embedding_model=provenance.model if provenance is not None else "",
                metadata_json=metadata,
            )
            session.add(memory)
            session.flush()
            return memory

    def reindex(self, memory_id: str, value: ShotMemoryInput) -> ShotMemory:
        """Give an existing degraded memory its vector, in place.

        ``index`` writes the structurally retrievable row even when the
        embedding provider is down and marks the vector degraded; the outbox
        then retries. Retrying through ``index`` again appended a *second*
        row per artefact and left the degraded one behind for ever. The
        retry re-embeds the row it already has: same id, same references,
        the vector and its provenance filled in. A row that is not degraded
        is returned untouched, and a row that no longer exists is indexed
        afresh.
        """

        with self.database.session() as session:
            existing = session.get(ShotMemory, memory_id)
            if existing is None:
                return self.index(value)
            if not (existing.metadata_json or {}).get("vector_degraded"):
                return existing
            self._validate_project_links(session, value)
        try:
            embedded = self.embeddings.embed_with_provenance(
                value.content,
                input_type="document",
                project_id=value.project_id,
            )
        except MemoryEmbeddingUnavailable as exc:
            # Still down. The row stays degraded and the outbox backs off
            # again; nothing is duplicated and nothing is rolled back.
            self._record_vector_degraded(value.project_id, exc)
            return existing
        provenance = embedded.provenance
        with self.database.session() as session:
            self._validate_project_links(session, value)
            memory = session.get(ShotMemory, memory_id)
            if memory is None:
                return self.index(value)
            metadata = {
                key: item
                for key, item in dict(memory.metadata_json or {}).items()
                if key not in {"vector_degraded", "degradation_reason_codes"}
            }
            metadata["evidence_purpose"] = provenance.evidence_purpose.value
            metadata["authority_level"] = provenance.authority_level.value
            if provenance.video_frame_lineage is not None:
                metadata["video_frame_lineage"] = provenance.video_frame_lineage.model_dump(mode="json")
            metadata["reembedded"] = True
            memory.embedding = list(embedded.values)
            memory.embedding_dimension = len(embedded.values)
            memory.embedding_provider = provenance.provider
            memory.embedding_model = provenance.model
            memory.metadata_json = metadata
            session.flush()
            return memory

    def search(self, query: MemoryQuery) -> list[RetrievedMemory]:
        if not self.enabled:
            return []
        try:
            embedded = self.embeddings.embed_with_provenance(
                MultimodalContent(
                    text=query.text,
                    image_urls=query.image_urls,
                    video_urls=query.video_urls,
                    evidence_purpose=query.evidence_purpose,
                    authority_level=query.authority_level,
                ),
                input_type="query",
                project_id=query.project_id,
            )
        except MemoryEmbeddingUnavailable as exc:
            self._record_vector_degraded(query.project_id, exc)
            return []
        query_vector = embedded.values
        provenance = embedded.provenance
        layer_values = [layer.value for layer in query.layers]
        with self.database.session() as session:
            statement = select(ShotMemory).where(
                ShotMemory.project_id == query.project_id,
                ShotMemory.layer.in_(layer_values),
                ShotMemory.embedding_provider == provenance.provider,
                ShotMemory.embedding_model == provenance.model,
                ShotMemory.embedding_dimension == len(query_vector),
            )
            # Scoping is per layer, because the three layers answer different
            # questions. L0 is series-wide entity truth and is never narrowed.
            # L1 is *current state*, so inheriting another scene's would be
            # wrong. L2 is "what happened before" — narrowing it to the current
            # scene made episodic recall unable to see anything it exists to
            # recall, which is why the 60-episode case never worked.
            if query.scene_id:
                statement = statement.where(
                    (ShotMemory.scene_id == query.scene_id)
                    | (ShotMemory.layer != MemoryLayer.TEMPORAL.value)
                )
            if query.shot_id:
                statement = statement.where(
                    (ShotMemory.shot_id == query.shot_id)
                    | (ShotMemory.layer != MemoryLayer.TEMPORAL.value)
                )
            if query.episode_id and query.episode_scope is EpisodeScope.EPISODE:
                statement = statement.where(
                    ShotMemory.scene_id.in_(select(Scene.id).where(Scene.episode_id == query.episode_id))
                    | (ShotMemory.layer == MemoryLayer.CANONICAL.value)
                )
            candidates = list(session.scalars(statement))
            # Episode is derived from the scene rather than stored on the row,
            # so it can never drift from the scene the memory actually belongs
            # to. Project-level memories have no scene and no episode.
            scene_ids = {item.scene_id for item in candidates if item.scene_id}
            episode_by_scene: dict[str, str] = (
                {
                    scene_id: episode_id
                    for scene_id, episode_id in session.execute(
                        select(Scene.id, Scene.episode_id).where(Scene.id.in_(scene_ids))
                    ).all()
                }
                if scene_ids
                else {}
            )

        if query.entity_ids:
            requested = set(query.entity_ids)
            candidates = [item for item in candidates if requested.intersection(item.entity_ids)]

        now = datetime.now(UTC)
        ranked: list[RetrievedMemory] = []
        for item in candidates:
            metadata = dict(item.metadata_json or {})
            try:
                evidence_purpose = EvidencePurpose(
                    metadata.get("evidence_purpose", EvidencePurpose.RETRIEVAL_HINT.value)
                )
                authority_level = AuthorityLevel(
                    metadata.get("authority_level", AuthorityLevel.ADVISORY.value)
                )
            except (TypeError, ValueError):
                # Unknown policy labels are not legacy defaults; they are an
                # untrusted attempt to cross the evidence boundary.
                continue
            if (
                evidence_purpose not in ADVISORY_EVIDENCE_PURPOSES
                or authority_level is not AuthorityLevel.ADVISORY
            ):
                # Fail closed for legacy/directly-inserted rows that claim a
                # forbidden purpose or authority. They are not retrieval evidence.
                continue
            metadata["evidence_purpose"] = evidence_purpose.value
            metadata["authority_level"] = AuthorityLevel.ADVISORY.value
            similarity = max(0.0, cosine_similarity(query_vector, list(item.embedding or [])))
            entity_match = (
                len(set(query.entity_ids).intersection(item.entity_ids)) / len(set(query.entity_ids))
                if query.entity_ids
                else 0.5
            )
            if query.temporal_position is not None and item.temporal_position is not None:
                temporal = 1.0 / (1.0 + abs(query.temporal_position - item.temporal_position))
            else:
                created = item.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                age_days = max(0.0, (now - created).total_seconds() / 86_400)
                temporal = 1.0 / (1.0 + age_days / query.recency_half_life_days)
            item_episode_id = episode_by_scene.get(item.scene_id) if item.scene_id else None
            scene_match = 1.0 if query.scene_id and item.scene_id == query.scene_id else 0.0
            # Under SERIES scope every episode is eligible, so the current one
            # must be ranked up rather than left to compete on cosine alone.
            episode_match = 1.0 if query.episode_id and item_episode_id == query.episode_id else 0.0
            canonical = 1.0 if item.canonical or item.layer == MemoryLayer.CANONICAL.value else 0.0
            components = {
                "multimodal_similarity": similarity,
                "entity_match": entity_match,
                "temporal_relevance": temporal,
                "scene_match": scene_match,
                "episode_match": episode_match,
                "canonical_priority": canonical,
            }
            score = (
                0.40 * similarity
                + 0.18 * entity_match
                + 0.14 * temporal
                + 0.09 * scene_match
                + 0.09 * episode_match
                + 0.10 * canonical
            )
            ranked.append(
                RetrievedMemory(
                    id=item.id,
                    project_id=item.project_id,
                    layer=MemoryLayer(item.layer),
                    memory_type=item.memory_type,
                    text=item.text_content,
                    image_urls=item.image_urls,
                    video_urls=item.video_urls,
                    entity_ids=item.entity_ids,
                    episode_id=item_episode_id,
                    scene_id=item.scene_id,
                    shot_id=item.shot_id,
                    asset_version_ids=item.asset_version_ids,
                    canonical=item.canonical,
                    score=round(score, 6),
                    score_components={key: round(value, 6) for key, value in components.items()},
                    metadata=metadata,
                    evidence_purpose=evidence_purpose,
                    authority_level=AuthorityLevel.ADVISORY,
                )
            )
        ranked.sort(key=lambda item: (-item.score, item.layer.value, item.id))
        return ranked[: query.top_k]

    def _record_vector_degraded(self, project_id: str, error: Exception) -> None:
        with self.database.session() as session:
            session.add(
                DecisionRecord(
                    project_id=project_id,
                    decision_type="MEMORY_VECTOR_DEGRADED",
                    input_features={"error_type": type(error).__name__},
                    selected_action="STRUCTURED_TIMELINE_ONLY",
                    reason_codes=["MEMORY_VECTOR_DEGRADED"],
                    model_version=self.version,
                    policy_version="memory-degrade-v1",
                )
            )

    def retrieval_hint(
        self,
        project_id: str,
        *,
        scene_id: str | None = None,
    ) -> RetrievedMemory | None:
        """Return an advisory temporal-memory hint, never authoritative state.

        The result is similarity-ranked historical context. Authoritative shot
        generation and state propagation must continue to read TimelineState or
        another committed state-version store.
        """

        query = MemoryQuery(
            project_id=project_id,
            text="advisory temporal production retrieval hint",
            scene_id=scene_id,
            layers=[MemoryLayer.TEMPORAL],
            top_k=1,
            evidence_purpose=EvidencePurpose.RETRIEVAL_HINT,
            authority_level=AuthorityLevel.ADVISORY,
        )
        values = self.search(query)
        return values[0] if values else None

    def current_state(self, project_id: str, *, scene_id: str | None = None) -> RetrievedMemory | None:
        """Compatibility alias for :meth:`retrieval_hint`.

        Despite the legacy name, this method has never returned authoritative
        narrative state. The returned object is explicitly marked ADVISORY.
        """

        return self.retrieval_hint(project_id, scene_id=scene_id)
