"""Reclaiming the media a deleted creation exclusively owned.

Deleting a creation is a database transaction: the row is stamped
``deleted_at``/``deleted_by`` and disappears from every user surface. Object
storage cannot join that transaction — a bucket call inside it would either
hold the transaction open across a network round trip or, when the bucket is
briefly unreachable, roll back a deletion the user already saw succeed. So the
deletion commits with a ``creation_media_cleanups`` row, and this sweep does
the storage work afterwards, retried under a backoff until the object is
genuinely gone. A failure here never touches the deletion.

Two rules make the reclamation safe:

*Exclusivity.* Generated media is content-addressed and freely re-used — the
same file can be a shot's plate, a character's master reference, a saved
project asset and another creation's output all at once. Every foreign key
into ``media_assets`` is therefore checked before anything is deleted, and a
single holder closes the row as ``KEPT_SHARED`` with the holder named. The
storage key is checked separately, because two asset rows can address one
object.

*Late results.* The queue points at the creation, not at an asset id captured
at deletion time, and the sweep resolves the creation's *current* output when
it runs. A provider result that lands after the user deleted a running
creation is collected by the same row that was already queued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from platform_database import Database
from platform_shared import StorageProvider
from production_domain.models import (
    AssetVersion,
    AssetVersionMedia,
    CandidateStyleEvaluation,
    CharacterIdentityVersion,
    CharacterStateValidation,
    CreationMediaCleanup,
    CreativeVisualAnchor,
    EmbeddingEvidence,
    EvaluationResult,
    GenerationCandidate,
    GenerationIdempotency,
    GenerationJob,
    Location,
    MediaAsset,
    MediaRendition,
    Prop,
    Shot,
    StorageReservation,
    new_id,
    utcnow,
)
from sqlalchemy import or_, select, update

DEFAULT_CREATION_CLEANUP_LIMIT = 50
#: After this many failed attempts the row stops retrying on its own and waits
#: for an operator. Ten attempts under the backoff below span about a day.
DEFAULT_CREATION_CLEANUP_MAX_ATTEMPTS = 10
DEFAULT_CREATION_CLEANUP_BACKOFF_SECONDS = 60
MAX_CREATION_CLEANUP_BACKOFF_SECONDS = 3600

DELETE_REASON = "CREATION_DELETED"


@dataclass(frozen=True)
class CreationMediaCleanupSweep:
    examined: int = 0
    claimed: int = 0
    completed: int = 0
    kept_shared: int = 0
    objects_deleted: int = 0
    renditions_deleted: int = 0
    retried: int = 0
    failed: int = 0
    contended: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)

    def as_response(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "claimed": self.claimed,
            "completed": self.completed,
            "kept_shared": self.kept_shared,
            "objects_deleted": self.objects_deleted,
            "renditions_deleted": self.renditions_deleted,
            "retried": self.retried,
            "failed": self.failed,
            "contended": self.contended,
            "rows": self.rows,
        }


def enqueue_creation_media_cleanup(session, job: GenerationJob) -> CreationMediaCleanup:  # type: ignore[no-untyped-def]
    """Queue a deleted creation's media for reclamation, at most once.

    Called inside the deletion transaction, so a queue row exists if and only
    if the deletion committed. Re-deleting an already deleted creation finds
    the row and returns it rather than queuing the work twice.
    """

    existing = session.scalar(
        select(CreationMediaCleanup).where(CreationMediaCleanup.generation_job_id == job.id)
    )
    if existing is not None:
        return existing
    row = CreationMediaCleanup(
        generation_job_id=job.id,
        project_id=job.project_id,
        media_asset_id=job.output_asset_id,
        status="PENDING",
        next_attempt_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _holders_of_asset(session, asset_id: str, *, job_id: str) -> list[str]:  # type: ignore[no-untyped-def]
    """Everything other than this creation that still needs the asset.

    Every foreign key into ``media_assets`` is represented. A name is returned
    rather than a boolean so the queue row can record *why* an asset was kept,
    which is the difference between an auditable decision and a mystery.
    """

    checks: list[tuple[str, Any]] = [
        (
            "another creation",
            select(GenerationJob.id).where(
                GenerationJob.output_asset_id == asset_id,
                GenerationJob.id != job_id,
                GenerationJob.deleted_at.is_(None),
            ),
        ),
        (
            "a repeated request",
            select(GenerationIdempotency.id).where(
                GenerationIdempotency.result_asset_id == asset_id,
                GenerationIdempotency.generation_job_id != job_id,
            ),
        ),
        (
            "a shot",
            select(Shot.id).where(
                or_(
                    Shot.start_frame_asset_id == asset_id,
                    Shot.end_frame_asset_id == asset_id,
                    Shot.output_video_asset_id == asset_id,
                )
            ),
        ),
        (
            "a shot take",
            select(GenerationCandidate.id).where(GenerationCandidate.output_asset_id == asset_id),
        ),
        (
            "a character",
            select(CharacterIdentityVersion.id).where(
                or_(
                    CharacterIdentityVersion.master_asset_id == asset_id,
                    CharacterIdentityVersion.front_asset_id == asset_id,
                    CharacterIdentityVersion.left_profile_asset_id == asset_id,
                    CharacterIdentityVersion.right_profile_asset_id == asset_id,
                    CharacterIdentityVersion.three_quarter_left_asset_id == asset_id,
                    CharacterIdentityVersion.three_quarter_right_asset_id == asset_id,
                    CharacterIdentityVersion.full_body_asset_id == asset_id,
                )
            ),
        ),
        (
            "a character record",
            select(CharacterStateValidation.id).where(
                CharacterStateValidation.evidence_asset_id == asset_id
            ),
        ),
        ("a location", select(Location.id).where(Location.canonical_asset_id == asset_id)),
        ("a prop", select(Prop.id).where(Prop.canonical_asset_id == asset_id)),
        (
            "a saved project asset",
            select(AssetVersion.id).where(AssetVersion.primary_media_asset_id == asset_id),
        ),
        (
            "a saved project asset",
            select(AssetVersionMedia.id).where(AssetVersionMedia.media_asset_id == asset_id),
        ),
        (
            "a derived file",
            select(MediaAsset.id).where(MediaAsset.parent_asset_id == asset_id),
        ),
        (
            "a style check",
            select(CandidateStyleEvaluation.id).where(
                CandidateStyleEvaluation.output_asset_id == asset_id
            ),
        ),
        (
            "a quality review",
            select(EvaluationResult.id).where(EvaluationResult.generated_asset_id == asset_id),
        ),
        ("project memory", select(EmbeddingEvidence.id).where(EmbeddingEvidence.asset_id == asset_id)),
        (
            "stored capacity",
            select(StorageReservation.id).where(
                StorageReservation.asset_id == asset_id,
                StorageReservation.released_at.is_(None),
            ),
        ),
        (
            "a story reference",
            select(CreativeVisualAnchor.id).where(CreativeVisualAnchor.media_asset_id == asset_id),
        ),
    ]
    holders: list[str] = []
    for label, statement in checks:
        if session.scalar(statement.limit(1)) is not None and label not in holders:
            holders.append(label)
    return holders


def _object_is_shared(session, storage_key: str, *, asset_id: str) -> bool:  # type: ignore[no-untyped-def]
    """Whether another row addresses the same stored object.

    Content addressing puts one object behind several rows. Deleting it
    because *this* row is going away would take the others' bytes with it.
    """

    other_asset = session.scalar(
        select(MediaAsset.id)
        .where(MediaAsset.storage_key == storage_key, MediaAsset.id != asset_id)
        .limit(1)
    )
    if other_asset is not None:
        return True
    other_rendition = session.scalar(
        select(MediaRendition.id)
        .where(
            MediaRendition.storage_key == storage_key,
            MediaRendition.media_asset_id != asset_id,
            MediaRendition.lifecycle_status != "DELETED",
        )
        .limit(1)
    )
    return other_rendition is not None


def _backoff_seconds(attempts: int, base: int) -> int:
    step = max(1, int(base)) * (2 ** max(0, attempts - 1))
    return min(MAX_CREATION_CLEANUP_BACKOFF_SECONDS, step)


def sweep_creation_media_cleanup(
    *,
    database: Database,
    storage: StorageProvider,
    limit: int = DEFAULT_CREATION_CLEANUP_LIMIT,
    lease_seconds: int = 600,
    max_attempts: int = DEFAULT_CREATION_CLEANUP_MAX_ATTEMPTS,
    backoff_seconds: int = DEFAULT_CREATION_CLEANUP_BACKOFF_SECONDS,
    cleanup_ids: list[str] | None = None,
    include_failed: bool = False,
) -> CreationMediaCleanupSweep:
    """Reclaim the storage of deleted creations that own their media alone.

    ``cleanup_ids`` (with ``include_failed``) is the operator's re-drive: a row
    that exhausted its attempts because the bucket was down for a day is
    retried by naming it, without waiting for a schedule.
    """

    claim = new_id()
    now = utcnow()
    lease_cutoff = now - timedelta(seconds=max(60, int(lease_seconds)))
    retryable = ["PENDING"] + (["FAILED"] if include_failed else [])

    with database.session() as session:
        statement = select(CreationMediaCleanup.id).where(
            or_(
                CreationMediaCleanup.status.in_(retryable),
                # An expired claim is a sweeper that died mid-delete.
                (CreationMediaCleanup.status == "CLAIMED")
                & (CreationMediaCleanup.claimed_at <= lease_cutoff),
            )
        )
        if cleanup_ids:
            statement = statement.where(CreationMediaCleanup.id.in_(cleanup_ids))
        else:
            statement = statement.where(
                or_(
                    CreationMediaCleanup.next_attempt_at.is_(None),
                    CreationMediaCleanup.next_attempt_at <= now,
                )
            )
        candidates = list(
            session.scalars(
                statement.order_by(CreationMediaCleanup.created_at).limit(max(1, limit))
            )
        )

    examined = len(candidates)
    claimed_ids: list[str] = []
    contended = 0
    for cleanup_id in candidates:
        with database.session() as session:
            result = session.execute(
                update(CreationMediaCleanup)
                .where(
                    CreationMediaCleanup.id == cleanup_id,
                    or_(
                        CreationMediaCleanup.status.in_(retryable),
                        (CreationMediaCleanup.status == "CLAIMED")
                        & (CreationMediaCleanup.claimed_at <= lease_cutoff),
                    ),
                )
                .values(
                    status="CLAIMED",
                    claim_id=claim,
                    claimed_at=now,
                    attempts=CreationMediaCleanup.attempts + 1,
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) == 1:
                claimed_ids.append(cleanup_id)
            else:
                contended += 1

    completed = kept_shared = objects_deleted = renditions_deleted = retried = failed = 0
    rows: list[dict[str, Any]] = []
    for cleanup_id in claimed_ids:
        try:
            outcome = _reclaim_one(
                database=database, storage=storage, cleanup_id=cleanup_id, claim=claim
            )
        except Exception as exc:  # noqa: BLE001 - the row records why and retries.
            outcome = _record_failure(
                database=database,
                cleanup_id=cleanup_id,
                claim=claim,
                error=exc,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
        if outcome is None:
            contended += 1
            continue
        rows.append(outcome)
        status = outcome["status"]
        if status == "DONE":
            completed += 1
            objects_deleted += int(bool(outcome.get("object_deleted")))
            renditions_deleted += int(outcome.get("renditions_deleted") or 0)
        elif status == "KEPT_SHARED":
            kept_shared += 1
        elif status == "PENDING":
            retried += 1
        elif status == "FAILED":
            failed += 1
    return CreationMediaCleanupSweep(
        examined=examined,
        claimed=len(claimed_ids),
        completed=completed,
        kept_shared=kept_shared,
        objects_deleted=objects_deleted,
        renditions_deleted=renditions_deleted,
        retried=retried,
        failed=failed,
        contended=contended,
        rows=rows,
    )


def _close(row: CreationMediaCleanup, status: str, **detail: Any) -> dict[str, Any]:
    row.status = status
    row.completed_at = utcnow()
    row.claim_id = None
    row.next_attempt_at = None
    row.last_error = None
    row.detail_json = {**(row.detail_json or {}), **detail}
    return {
        "id": row.id,
        "generation_job_id": row.generation_job_id,
        "media_asset_id": row.media_asset_id,
        "status": status,
        **detail,
    }


def _reclaim_one(
    *, database: Database, storage: StorageProvider, cleanup_id: str, claim: str
) -> dict[str, Any] | None:
    with database.session() as session:
        # The row lock spans the storage calls, so a competing sweeper waits
        # rather than double-deleting the same object.
        row = session.scalar(
            select(CreationMediaCleanup)
            .where(CreationMediaCleanup.id == cleanup_id)
            .with_for_update()
        )
        if row is None or row.status != "CLAIMED" or row.claim_id != claim:
            return None

        job = session.get(GenerationJob, row.generation_job_id)
        if job is None:
            return _close(row, "DONE", reason="creation no longer exists")
        if job.deleted_at is None:
            # Belt and braces: nothing restores a deleted creation today, but
            # if anything ever does, its media must survive the queue row.
            return _close(row, "DONE", reason="creation is not deleted")

        # Resolved now, not at deletion time: a provider result that landed
        # after the user deleted a running creation is collected here.
        asset_id = job.output_asset_id
        row.media_asset_id = asset_id
        if not asset_id:
            return _close(row, "DONE", reason="creation produced no media")
        asset = session.get(MediaAsset, asset_id)
        if asset is None:
            return _close(row, "DONE", reason="media already gone")

        holders = _holders_of_asset(session, asset_id, job_id=job.id)
        if holders:
            return _close(row, "KEPT_SHARED", kept_for=holders)

        renditions = list(
            session.scalars(
                select(MediaRendition).where(
                    MediaRendition.media_asset_id == asset_id,
                    MediaRendition.lifecycle_status != "DELETED",
                )
            )
        )
        deleted_renditions = 0
        for rendition in renditions:
            rendition_shared = _object_is_shared(
                session, rendition.storage_key, asset_id=asset_id
            ) or (rendition.storage_key == asset.storage_key)
            if not rendition_shared:
                # Idempotent: a missing object (a crashed earlier attempt got
                # this far) reads as already deleted, which is the goal state.
                if storage.delete(rendition.storage_key):
                    deleted_renditions += 1
            rendition.lifecycle_status = "DELETED"
            rendition.deleted_at = utcnow()
            rendition.delete_reason = DELETE_REASON

        object_shared = _object_is_shared(session, asset.storage_key, asset_id=asset_id)
        object_deleted = False
        if not object_shared:
            object_deleted = bool(storage.delete(asset.storage_key))
        # The asset row itself stays: RESTRICT foreign keys and the evidence
        # trail both point at it. What it records is that its bytes are gone.
        asset.public_url = None
        asset.local_path = None
        asset.metadata_json = {
            **(asset.metadata_json or {}),
            "creation_deleted": {
                "generation_job_id": job.id,
                "deleted_at": utcnow().isoformat(),
                "object_deleted": object_deleted,
                "object_shared": object_shared,
                "sha256": asset.sha256,
                "size_bytes": asset.size_bytes,
            },
        }
        outcome = _close(
            row,
            "DONE",
            object_deleted=object_deleted,
            object_shared=object_shared,
            renditions_deleted=deleted_renditions,
            storage_key=asset.storage_key,
        )
        session.flush()
        return outcome


def _record_failure(
    *,
    database: Database,
    cleanup_id: str,
    claim: str,
    error: Exception,
    max_attempts: int,
    backoff_seconds: int,
) -> dict[str, Any] | None:
    """Park a row for another attempt; the creation stays deleted regardless.

    Written in its own transaction because the failing one was rolled back
    with the storage error.
    """

    with database.session() as session:
        row = session.get(CreationMediaCleanup, cleanup_id)
        if row is None or row.claim_id != claim:
            return None
        row.last_error = f"{type(error).__name__}: {error}"[:500]
        row.claim_id = None
        if row.attempts >= max(1, int(max_attempts)):
            row.status = "FAILED"
            row.next_attempt_at = None
        else:
            row.status = "PENDING"
            row.next_attempt_at = utcnow() + timedelta(
                seconds=_backoff_seconds(row.attempts, backoff_seconds)
            )
        session.flush()
        return {
            "id": row.id,
            "generation_job_id": row.generation_job_id,
            "media_asset_id": row.media_asset_id,
            "status": row.status,
            "attempts": row.attempts,
            "error": row.last_error,
        }


__all__ = [
    "DEFAULT_CREATION_CLEANUP_BACKOFF_SECONDS",
    "DEFAULT_CREATION_CLEANUP_LIMIT",
    "DEFAULT_CREATION_CLEANUP_MAX_ATTEMPTS",
    "CreationMediaCleanupSweep",
    "enqueue_creation_media_cleanup",
    "sweep_creation_media_cleanup",
]
