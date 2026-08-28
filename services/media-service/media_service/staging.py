"""Deterministic staging for provider generation output, and its reclamation.

A completed generation used to be persisted in pieces: sibling candidate rows in
one transaction, one media row per artefact in others, and the job completion in
a last one. A process death between any two of those left half a batch — empty
CREATED candidates, or media without a completed job. The staging area inverts
the order: every artefact is written to object storage *first*, under a key that
is a pure function of the job and its provider attempt, and the database learns
about all of it in one transaction afterwards.

Two properties carry the whole design:

**The key is deterministic.** ``staging/generation/{job_id}/{attempt}/{index}``
means a crashed attempt that re-runs overwrites its own slots instead of
accreting new objects, and the finalize transaction is idempotent because the
job-row fence — not the storage layer — decides who may adopt the slots.

**Adoption is in place.** The finalize transaction records the staging key as
the asset's ``storage_key``; nothing is copied or renamed. If that transaction
fails, no row references the objects and they are plain recyclable staging; if
it commits, the reference is what tells the sweeper the object is now media.

The sweeper deletes a staged object only when every one of these holds: it is
older than the TTL, its job (parsed from the key) is terminal or unknown, and no
``MediaAsset`` row references its key. A job that is still running — or one an
operator may still reconcile back into the poll loop — keeps its slots, however
old. Deleting a terminal job's unadopted slots is safe because adoption happens
only under a RESERVED claim, which a terminal job can never hold again.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from platform_database import Database
from platform_shared import StorageProvider
from production_domain.models import GenerationJob, JobStatus, MediaAsset, utcnow
from sqlalchemy import select

GENERATION_STAGING_PREFIX = "staging/generation/"

# Statuses whose staged objects can never be adopted again: adoption runs only
# under a RESERVED completion claim, and none of these can be claimed.
_TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.COMPLETED.value,
        JobStatus.FAILED.value,
        JobStatus.CANCELLED.value,
    }
)

DEFAULT_STAGING_SWEEP_LIMIT = 500
MIN_STAGING_TTL_SECONDS = 60


@dataclass(frozen=True)
class StagedProviderOutput:
    """One validated provider artefact, written to its staging slot.

    Everything the finalize transaction needs to register the asset without
    touching the bytes again: identity (key + digest), size, the validated MIME
    type, and the probe results taken while a local copy was at hand.
    """

    storage_key: str
    sha256: str
    size_bytes: int
    mime_type: str
    local_path: str | None
    public_url: str | None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    source_url: str | None = None


def generation_staging_prefix(job_id: str, provider_job_id: str) -> str:
    """The deterministic slot prefix for one job's one provider attempt.

    The provider job id is hashed rather than embedded: it is provider-chosen
    text and has no business shaping object keys. A retry that resubmits gets a
    new provider job id and therefore a disjoint set of slots; the abandoned
    ones age out through the sweeper.
    """

    attempt = hashlib.sha256(provider_job_id.encode("utf-8")).hexdigest()[:16]
    return f"{GENERATION_STAGING_PREFIX}{job_id}/{attempt}/"


def job_id_from_staging_key(key: str) -> str | None:
    if not key.startswith(GENERATION_STAGING_PREFIX):
        return None
    job_id = key[len(GENERATION_STAGING_PREFIX) :].split("/", 1)[0].strip()
    return job_id or None


@dataclass(frozen=True)
class GenerationStagingSweep:
    deleted: list[dict[str, Any]] = field(default_factory=list)
    # Slots whose job is still live. Not garbage: the job's next attempt is
    # entitled to overwrite or adopt them.
    kept_job_active: int = 0
    # Slots a MediaAsset row references. Not staging any more: the finalize
    # transaction adopted them in place, and deleting one would delete media.
    kept_referenced: int = 0
    kept_young: int = 0
    failed: list[dict[str, Any]] = field(default_factory=list)

    def as_response(self) -> dict[str, Any]:
        return {
            "deleted": self.deleted,
            "deleted_count": len(self.deleted),
            "kept_job_active": self.kept_job_active,
            "kept_referenced": self.kept_referenced,
            "kept_young": self.kept_young,
            "failed": self.failed,
        }


def sweep_generation_staging(
    *,
    database: Database,
    storage: StorageProvider,
    ttl_seconds: int,
    limit: int = DEFAULT_STAGING_SWEEP_LIMIT,
    now: datetime | None = None,
) -> GenerationStagingSweep:
    """Reclaim staged generation output nothing can ever adopt again.

    The storage listing is the ground truth — a process that died between
    writing a slot and any database write left no row to enumerate — and the
    database is the safety check: a slot is deleted only when its job is
    terminal or unknown *and* no asset row adopted the key. Everything else is
    kept and counted, so the response says why storage is holding what it
    holds.
    """

    deadline = (now or utcnow()) - timedelta(seconds=max(MIN_STAGING_TTL_SECONDS, ttl_seconds))
    sweep_limit = max(1, limit)
    expired: list[tuple[str, datetime]] = []
    kept_young = 0
    for key, modified in storage.list_keys(GENERATION_STAGING_PREFIX):
        normalized = modified if modified.tzinfo is not None else modified.replace(tzinfo=UTC)
        if normalized > deadline:
            kept_young += 1
            continue
        expired.append((key, normalized))

    deleted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    kept_job_active = 0
    kept_referenced = 0
    # Chunked, not sliced: adopted objects stay in this listing for the life of
    # their media, so any fixed window taken before the database check would
    # eventually hold nothing but keys that must be kept, and the deletable
    # ones behind them would never be reached.
    chunk_size = 400
    for start in range(0, len(expired), chunk_size):
        if len(deleted) >= sweep_limit:
            break
        chunk = expired[start : start + chunk_size]
        job_ids = {job_id for key, _ in chunk if (job_id := job_id_from_staging_key(key))}
        with database.session() as session:
            live_jobs = {
                job_id
                for job_id, status in session.execute(
                    select(GenerationJob.id, GenerationJob.status).where(GenerationJob.id.in_(job_ids))
                )
                if status not in _TERMINAL_JOB_STATUSES
            }
            referenced = set(
                session.scalars(
                    select(MediaAsset.storage_key).where(
                        MediaAsset.storage_key.in_([key for key, _ in chunk])
                    )
                )
            )
        for key, modified in chunk:
            if len(deleted) >= sweep_limit:
                break
            job_id = job_id_from_staging_key(key)
            if job_id is not None and job_id in live_jobs:
                kept_job_active += 1
                continue
            if key in referenced:
                kept_referenced += 1
                continue
            try:
                removed = storage.delete(key)
            except Exception as exc:
                failed.append({"key": key, "error": str(exc)[:200]})
                continue
            if removed:
                deleted.append(
                    {
                        "key": key,
                        "job_id": job_id,
                        "last_modified": modified.isoformat(),
                    }
                )

    return GenerationStagingSweep(
        deleted=deleted,
        kept_job_active=kept_job_active,
        kept_referenced=kept_referenced,
        kept_young=kept_young,
        failed=failed,
    )


__all__ = [
    "DEFAULT_STAGING_SWEEP_LIMIT",
    "GENERATION_STAGING_PREFIX",
    "GenerationStagingSweep",
    "StagedProviderOutput",
    "generation_staging_prefix",
    "job_id_from_staging_key",
    "sweep_generation_staging",
]
