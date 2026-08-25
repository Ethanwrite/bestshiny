"""Reclaiming what an abandoned direct upload leaves behind.

A client that authorizes an upload and walks away leaves three things: a
`PENDING` row, a quota hold taken against the declared size, and possibly an
object it did PUT before giving up. `expires_at` was written and indexed from
the start and nothing ever read it, so the hold was capacity the workspace never
got back.

Two properties this module exists to hold:

**One transaction per upload, in the completion path's lock order.** The row is
locked first and its reservation second, and the expiry predicate is re-checked
under that lock. Taking them in either a different order or different
transactions is what let a sweep release a hold out from under a completion that
already owned the row — see `DirectUploadService.claim_expired`.

**The object is never deleted.** Removing bytes a user may have paid to upload
is not a decision a sweeper should make, so an abandoned upload still leaves one
orphan in the bucket and says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from platform_database import Database
from production_domain.models import (
    DirectUpload,
    DirectUploadStatus,
    StorageReservation,
    utcnow,
)
from sqlalchemy import select

from .direct_upload import DirectUploadService
from .quota import StorageReservationConflict, WorkspaceStorageQuota

DEFAULT_SWEEP_LIMIT = 200
MIN_RESERVATION_STALE_SECONDS = 60


@dataclass(frozen=True)
class ExpiredUploadSweep:
    swept: list[dict[str, Any]] = field(default_factory=list)
    # Candidates a concurrent completion or re-authorization took ownership of
    # between the unlocked read and the lock. Not failures: the other path is
    # the one that should finish them.
    contended: list[dict[str, Any]] = field(default_factory=list)
    reservations_needing_reconciliation: list[dict[str, Any]] = field(default_factory=list)

    def as_response(self) -> dict[str, Any]:
        return {
            "swept": self.swept,
            "swept_count": len(self.swept),
            "contended": self.contended,
            "contended_count": len(self.contended),
            # Reported, never released. A hold this old with no PENDING upload
            # behind it is either a process that died mid-request or a
            # registration whose settlement failed — and those two need
            # opposite actions, which only an operator can tell apart.
            "reservations_needing_reconciliation": self.reservations_needing_reconciliation,
        }


def sweep_expired_uploads(
    *,
    database: Database,
    uploads: DirectUploadService,
    quota: WorkspaceStorageQuota,
    limit: int = DEFAULT_SWEEP_LIMIT,
    reservation_stale_after_seconds: int = 86_400,
    now: datetime | None = None,
) -> ExpiredUploadSweep:
    """Abandon every expired upload and reclaim the hold it was still carrying."""

    deadline = now or utcnow()
    swept: list[dict[str, Any]] = []
    contended: list[dict[str, Any]] = []

    for upload_id in uploads.expired(limit=limit, now=deadline):
        try:
            with database.session() as session:
                claim = uploads.claim_expired(session, upload_id, now=deadline)
                if not claim.claimed:
                    contended.append({"upload_id": upload_id})
                    continue
                released = False
                if claim.reservation_id and claim.media_asset_id is None:
                    try:
                        released = quota.release_in(session, claim.reservation_id)
                    except LookupError:
                        # Raised before any statement runs, so the abandon this
                        # transaction already made still commits.
                        released = False
                record = {
                    "upload_id": claim.upload_id,
                    "project_id": claim.project_id,
                    "storage_key": claim.storage_key,
                    "expires_at": claim.expires_at.isoformat() if claim.expires_at else None,
                    "reservation_released": released,
                    # The bucket still holds whatever the client wrote.
                    "orphaned_object": True,
                }
        except StorageReservationConflict as exc:
            # The whole unit rolled back, so the row is still PENDING and the
            # next sweep will try again. Reclaiming a hold whose counters do not
            # add up is exactly the thing that must not be forced.
            contended.append({"upload_id": upload_id, "reason": str(exc)})
            continue
        swept.append(record)

    return ExpiredUploadSweep(
        swept=swept,
        contended=contended,
        reservations_needing_reconciliation=_stale_reservations(
            database,
            stale_before=deadline
            - timedelta(seconds=max(MIN_RESERVATION_STALE_SECONDS, reservation_stale_after_seconds)),
        ),
    )


def _stale_reservations(database: Database, *, stale_before: datetime) -> list[dict[str, Any]]:
    with database.session() as session:
        accounted = set(
            session.scalars(
                select(DirectUpload.storage_reservation_id).where(
                    DirectUpload.status == DirectUploadStatus.PENDING.value,
                    DirectUpload.storage_reservation_id.is_not(None),
                )
            )
        )
        return [
            {
                "reservation_id": item.id,
                "workspace_id": item.workspace_id,
                "project_id": item.project_id,
                "reserved_bytes": item.reserved_bytes,
                "created_at": _aware(item.created_at).isoformat(),
            }
            for item in session.scalars(
                select(StorageReservation).where(
                    StorageReservation.status == "RESERVED",
                    StorageReservation.created_at < stale_before,
                )
            )
            if item.id not in accounted
        ]


def _aware(value: datetime) -> datetime:
    from datetime import UTC

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = ["DEFAULT_SWEEP_LIMIT", "ExpiredUploadSweep", "sweep_expired_uploads"]
