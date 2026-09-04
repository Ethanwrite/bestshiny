"""The boundary between writing Canon and remembering it.

Embedding is an external HTTPS call to a third party. Making it inside the
transaction that locks a visual bible or commits a candidate would put a
vendor's availability on the critical path of Canon, which is exactly
backwards: this memory is ADVISORY and the Canon is not. So a writer enqueues
one durable row and this worker drains it afterwards.

Three properties matter and are the reason this file exists rather than a
direct call:

* **Nothing here is authoritative.** Every enqueued memory carries
  ``authority_level = ADVISORY`` and an advisory ``evidence_purpose``; the
  schema refuses anything else. A vector never writes a narrative fact, an
  identity conclusion or a commit authorization.
* **Nothing here can fail a business request.** The enqueue is a row insert in
  the caller's own transaction; the embedding happens later, and an outage
  backs off and retries. Generation keeps using the structured Canon, the
  timeline and the ledger whether or not this ever succeeds.
* **Nothing here is written twice.** The idempotency key is stable per source
  artefact, so a replayed lock, a re-run worker and a concurrent drain all
  converge on one ShotMemory.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from platform_database import Database
from production_domain.models import MediaAsset, MemoryIndexOutbox, utcnow
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from .schemas import (
    AuthorityLevel,
    EvidencePurpose,
    MemoryLayer,
    MultimodalContent,
    ShotMemoryInput,
)

#: Sources, so an operator can tell what produced a queued memory.
SOURCE_VISUAL_BIBLE_LOCK = "VISUAL_BIBLE_LOCK"
SOURCE_CANDIDATE_COMMIT = "CANDIDATE_COMMIT"

#: A queued memory is a nicety; it never deserves an unbounded retry budget.
MAX_ATTEMPTS = 5
#: Backoff between attempts, by attempt number.
RETRY_BACKOFF_SECONDS = (30, 300, 1800, 7200)
#: The flag that governs whether anything is embedded at all.
MEMORY_FEATURE_FLAG = "voyage_memory"


class MemoryIndexer(Protocol):
    """The slice of ``MultimodalMemoryEngine`` this worker needs."""

    def index(self, value: ShotMemoryInput) -> Any: ...


class FeatureFlags(Protocol):
    def enabled(self, name: str, *, project_id: str | None = None) -> bool: ...


@dataclass(frozen=True)
class MemoryOutboxResult:
    claimed: int = 0
    indexed: int = 0
    deferred: int = 0
    failed: int = 0
    retried: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "claimed": self.claimed,
            "indexed": self.indexed,
            "deferred": self.deferred,
            "failed": self.failed,
            "retried": self.retried,
        }


class MemoryIndexOutboxWriter:
    """Enqueue advisory memory work. Never embeds, never raises on a duplicate."""

    def __init__(self, database: Database):
        self.database = database

    def enqueue(  # noqa: PLR0913 - one row records the whole memory to build
        self,
        project_id: str,
        *,
        session: Any | None = None,
        idempotency_key: str,
        source: str,
        memory_type: str,
        text: str,
        layer: MemoryLayer = MemoryLayer.EPISODIC,
        media_asset_ids: Sequence[str] = (),
        entity_ids: Sequence[str] = (),
        asset_version_ids: Sequence[str] = (),
        shot_id: str | None = None,
        scene_id: str | None = None,
        evidence_purpose: EvidencePurpose = EvidencePurpose.RETRIEVAL_HINT,
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Queue one memory. Returns its row id, or None when already queued."""

        payload = {
            "memory_type": memory_type,
            "layer": layer.value,
            "text": text,
            "media_asset_ids": [str(item) for item in media_asset_ids if item],
            "entity_ids": [str(item) for item in entity_ids if item],
            "asset_version_ids": [str(item) for item in asset_version_ids if item],
            "shot_id": shot_id,
            "scene_id": scene_id,
            "evidence_purpose": EvidencePurpose(evidence_purpose).value,
            # Not a parameter: an authoritative vector is a contradiction, and
            # the schema would refuse it anyway.
            "authority_level": AuthorityLevel.ADVISORY.value,
            "metadata": dict(metadata or {}),
        }
        def _insert(active: Any) -> str | None:
            existing = active.scalar(
                select(MemoryIndexOutbox).where(
                    MemoryIndexOutbox.idempotency_key == idempotency_key[:250]
                )
            )
            if existing is not None:
                # Already queued by an earlier attempt of the same lock or
                # commit; one artefact, one memory.
                return None
            row = MemoryIndexOutbox(
                project_id=project_id,
                idempotency_key=idempotency_key[:250],
                source=source,
                payload_json=payload,
                status="PENDING",
                next_attempt_at=utcnow(),
            )
            active.add(row)
            active.flush()
            return str(row.id)

        if session is not None:
            # Enqueued inside the caller's own transaction: a memory is never
            # queued for Canon that rolled back, and the caller's transaction
            # is never blocked on a second connection.
            return _insert(session)
        try:
            with self.database.session() as owned:
                return _insert(owned)
        except IntegrityError:
            return None


class MemoryIndexOutboxWorker:
    """Drain the queue into the vector memory, idempotently and off the hot path."""

    version = "memory-index-outbox-v1"

    def __init__(
        self,
        database: Database,
        memory: MemoryIndexer,
        *,
        flags: FeatureFlags | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ):
        self.database = database
        self.memory = memory
        self.flags = flags
        self.max_attempts = max(1, int(max_attempts))

    def _enabled(self, project_id: str) -> bool:
        if self.flags is None:
            return True
        try:
            return bool(self.flags.enabled(MEMORY_FEATURE_FLAG, project_id=project_id))
        except KeyError:
            # An unregistered flag is not permission to spend money on a
            # third-party embedding call.
            return False

    def drain(self, *, limit: int = 20) -> MemoryOutboxResult:
        claim_id = uuid.uuid4().hex
        now = utcnow()
        with self.database.session() as session:
            due = [
                row.id
                for row in session.scalars(
                    select(MemoryIndexOutbox)
                    .where(
                        MemoryIndexOutbox.status == "PENDING",
                        or_(
                            MemoryIndexOutbox.next_attempt_at.is_(None),
                            MemoryIndexOutbox.next_attempt_at <= now,
                        ),
                    )
                    .order_by(MemoryIndexOutbox.created_at)
                    .limit(max(1, limit))
                    .with_for_update(skip_locked=True)
                )
            ]
        claimed = indexed = deferred = failed = retried = 0
        for row_id in due:
            with self.database.session() as session:
                row = session.get(MemoryIndexOutbox, row_id)
                if row is None or row.status != "PENDING":
                    continue
                if not self._enabled(row.project_id):
                    # Waiting, not lost: enabling the flag later indexes it.
                    deferred += 1
                    continue
                row.status = "CLAIMED"
                row.claim_id = claim_id
                row.claimed_at = now
                row.attempts += 1
                attempts = row.attempts
                project_id = row.project_id
                payload = dict(row.payload_json)
                session.flush()
            claimed += 1
            try:
                value = self._build(project_id, payload)
                memory = self.memory.index(value)
            except Exception as exc:  # noqa: BLE001 - advisory work never escapes
                with self.database.session() as session:
                    row = session.get(MemoryIndexOutbox, row_id)
                    if row is None:
                        continue
                    row.last_error = f"{type(exc).__name__}: {exc}"[:500]
                    if attempts >= self.max_attempts:
                        row.status = "FAILED"
                        row.claim_id = None
                        failed += 1
                    else:
                        backoff = RETRY_BACKOFF_SECONDS[
                            min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)
                        ]
                        row.status = "PENDING"
                        row.claim_id = None
                        row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=backoff)
                        retried += 1
                    session.flush()
                continue
            degraded = bool(
                (getattr(memory, "metadata_json", None) or {}).get("vector_degraded")
            )
            with self.database.session() as session:
                row = session.get(MemoryIndexOutbox, row_id)
                if row is None:
                    continue
                row.status = "DONE"
                row.claim_id = None
                row.completed_at = utcnow()
                row.shot_memory_id = getattr(memory, "id", None)
                # The engine writes the structurally retrievable row even when
                # the embedding provider is down, and marks the vector
                # degraded. That is DONE, not a retry: re-indexing would append
                # a second ShotMemory for the same artefact, and one artefact
                # is meant to have one memory. The degradation is recorded here
                # and on the row itself, so a re-embed can be driven from it.
                row.last_error = "VECTOR_DEGRADED" if degraded else None
                session.flush()
            indexed += 1
        return MemoryOutboxResult(
            claimed=claimed, indexed=indexed, deferred=deferred, failed=failed, retried=retried
        )

    def _build(self, project_id: str, payload: dict[str, Any]) -> ShotMemoryInput:
        """Resolve the queued payload into one memory input, at drain time.

        Media URLs are resolved now rather than stored, so a re-signed or
        re-hosted asset is embedded from its current location instead of a URL
        that expired while the row waited.
        """

        image_urls: list[str] = []
        video_urls: list[str] = []
        with self.database.session() as session:
            for media_id in payload.get("media_asset_ids", []):
                media = session.get(MediaAsset, media_id)
                url = (media.public_url or "") if media is not None else ""
                if not url.startswith("https://"):
                    continue
                if media.mime_type.startswith("image/"):
                    image_urls.append(url)
                elif media.mime_type.startswith("video/"):
                    video_urls.append(url)
        return ShotMemoryInput(
            project_id=project_id,
            layer=MemoryLayer(payload.get("layer") or MemoryLayer.EPISODIC.value),
            memory_type=str(payload.get("memory_type") or "ASSET_VERSION"),
            content=MultimodalContent(
                text=str(payload.get("text") or ""),
                image_urls=image_urls[:16],
                video_urls=video_urls[:4],
                evidence_purpose=EvidencePurpose(
                    payload.get("evidence_purpose") or EvidencePurpose.RETRIEVAL_HINT.value
                ),
                authority_level=AuthorityLevel.ADVISORY,
            ),
            entity_ids=list(payload.get("entity_ids") or []),
            asset_version_ids=list(payload.get("asset_version_ids") or []),
            shot_id=payload.get("shot_id"),
            scene_id=payload.get("scene_id"),
            metadata={
                **dict(payload.get("metadata") or {}),
                "indexer_version": self.version,
                "authority_level": AuthorityLevel.ADVISORY.value,
            },
        )


__all__ = [
    "MAX_ATTEMPTS",
    "MEMORY_FEATURE_FLAG",
    "SOURCE_CANDIDATE_COMMIT",
    "SOURCE_VISUAL_BIBLE_LOCK",
    "MemoryIndexOutboxWorker",
    "MemoryIndexOutboxWriter",
    "MemoryOutboxResult",
]
