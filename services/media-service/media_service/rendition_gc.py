"""Retiring derived renditions nothing will ask for again.

A derived rendition is disposable cache: the original is never touched, so the
worst a collection can cost is one re-derivation. What still has to be
engineered is *safe* disposal — two workers must not double-delete, a copy a
current provider would ask for tomorrow should not be collected today, an
object shared with other rows must survive, and a deletion must leave evidence
rather than a mystery.

The sweep is two-phase per row. Phase one claims the row under a lease
(``GC_CLAIMED`` + claim id + timestamp, compare-and-set from ``ACTIVE`` or an
*expired* claim), so a crashed sweeper's work is re-claimable and a live
competitor's is not. Phase two checks object sharing, deletes the object where
it is exclusively ours, and marks the row ``DELETED`` — a tombstone keeping
the sha256, size and reason, which is what makes the deletion reconcilable
and lets `insert_or_revive_rendition` bring the slot back when the same
constraints matter again.

Eligibility is deliberately narrow: never ``ORIGINAL`` rows, never a rendition
whose constraint profile any *current* provider still declares (those are the
useful cache — the point of collection is the copies stranded by constraint
changes), and never anything accessed inside the idle window, which also
protects references handed out to in-flight generations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from platform_database import Database
from platform_shared import StorageProvider
from production_domain.models import MediaAsset, MediaRendition, MediaRenditionKind, new_id, utcnow
from sqlalchemy import func, or_, select, update

from .thumbnails import THUMBNAIL_CONSTRAINT_KEY

DEFAULT_RENDITION_GC_LIMIT = 100


@dataclass(frozen=True)
class RenditionGcSweep:
    examined: int = 0
    claimed: int = 0
    deleted_rows: list[dict[str, Any]] = field(default_factory=list)
    objects_deleted: int = 0
    objects_kept_shared: int = 0
    kept_current_profile: int = 0
    contended: int = 0

    def as_response(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "claimed": self.claimed,
            "deleted": self.deleted_rows,
            "deleted_count": len(self.deleted_rows),
            "objects_deleted": self.objects_deleted,
            "objects_kept_shared": self.objects_kept_shared,
            "kept_current_profile": self.kept_current_profile,
            "contended": self.contended,
        }


def _profile_of(row: MediaRendition) -> str | None:
    profile = (row.metadata_json or {}).get("constraint_profile")
    return str(profile) if profile else None


def sweep_rendition_gc(
    *,
    database: Database,
    storage: StorageProvider,
    active_constraint_profiles: frozenset[str],
    min_idle_seconds: int,
    lease_seconds: int = 600,
    limit: int = DEFAULT_RENDITION_GC_LIMIT,
    claim_id: str | None = None,
) -> RenditionGcSweep:
    """Collect idle, stale-profile derived renditions, safely and observably.

    ``active_constraint_profiles`` is the set of constraint identities current
    providers declare (``ProviderReferenceConstraints.key()`` values); a
    rendition matching one — by its ``constraint_key`` for images, or by its
    recorded ``constraint_profile`` for digest-keyed video renditions — is the
    live cache and is kept regardless of idleness. Thumbnails are always a
    current profile.
    """

    claim = claim_id or new_id()
    now = utcnow()
    idle_cutoff = now - timedelta(seconds=max(60, int(min_idle_seconds)))
    lease_cutoff = now - timedelta(seconds=max(60, int(lease_seconds)))
    keep_profiles = set(active_constraint_profiles) | {THUMBNAIL_CONSTRAINT_KEY}

    with database.session() as session:
        candidates = list(
            session.scalars(
                select(MediaRendition)
                .where(
                    MediaRendition.kind != MediaRenditionKind.ORIGINAL.value,
                    or_(
                        MediaRendition.lifecycle_status == "ACTIVE",
                        # An expired claim is a sweeper that died mid-delete.
                        MediaRendition.lifecycle_status == "GC_CLAIMED",
                    ),
                    func.coalesce(MediaRendition.last_accessed_at, MediaRendition.created_at)
                    <= idle_cutoff,
                )
                .order_by(
                    func.coalesce(MediaRendition.last_accessed_at, MediaRendition.created_at)
                )
                .limit(max(1, limit) * 2)
            )
        )
    examined = len(candidates)
    kept_current = 0
    eligible: list[str] = []
    for row in candidates:
        if row.constraint_key in keep_profiles or _profile_of(row) in keep_profiles:
            kept_current += 1
            continue
        eligible.append(row.id)
        if len(eligible) >= max(1, limit):
            break

    claimed_ids: list[str] = []
    contended = 0
    for rendition_id in eligible:
        with database.session() as session:
            result = session.execute(
                update(MediaRendition)
                .where(
                    MediaRendition.id == rendition_id,
                    or_(
                        MediaRendition.lifecycle_status == "ACTIVE",
                        # Reclaim only a claim whose lease has lapsed; a live
                        # competitor keeps its row.
                        (MediaRendition.lifecycle_status == "GC_CLAIMED")
                        & (MediaRendition.gc_claimed_at <= lease_cutoff),
                    ),
                )
                .values(
                    lifecycle_status="GC_CLAIMED",
                    gc_claim_id=claim,
                    gc_claimed_at=now,
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) == 1:
                claimed_ids.append(rendition_id)
            else:
                contended += 1

    deleted_rows: list[dict[str, Any]] = []
    objects_deleted = 0
    objects_kept_shared = 0
    for rendition_id in claimed_ids:
        with database.session() as session:
            row = session.get(MediaRendition, rendition_id)
            if row is None or row.lifecycle_status != "GC_CLAIMED" or row.gc_claim_id != claim:
                contended += 1
                continue
            # Content-addressed storage can share one object between rows.
            # The object is only ours to delete when nothing else references
            # its key — another live rendition, or a MediaAsset original.
            shared_with_asset = (
                session.scalar(
                    select(MediaAsset.id).where(MediaAsset.storage_key == row.storage_key).limit(1)
                )
                is not None
            )
            shared_with_rendition = (
                session.scalar(
                    select(MediaRendition.id)
                    .where(
                        MediaRendition.storage_key == row.storage_key,
                        MediaRendition.id != row.id,
                        MediaRendition.lifecycle_status != "DELETED",
                    )
                    .limit(1)
                )
                is not None
            )
            shared = shared_with_asset or shared_with_rendition
            object_deleted = False
            if not shared:
                # Idempotent: a missing object (a crashed earlier attempt got
                # this far) reads as already-deleted, which is the goal state.
                object_deleted = bool(storage.delete(row.storage_key))
            row.lifecycle_status = "DELETED"
            row.deleted_at = utcnow()
            row.delete_reason = "RENDITION_GC_IDLE_STALE_PROFILE"
            row.metadata_json = {
                **(row.metadata_json or {}),
                "gc": {
                    "claim_id": claim,
                    "object_deleted": object_deleted,
                    "object_shared": shared,
                    "sha256": row.sha256,
                    "size_bytes": row.size_bytes,
                },
            }
            session.flush()
            if shared:
                objects_kept_shared += 1
            elif object_deleted:
                objects_deleted += 1
            deleted_rows.append(
                {
                    "id": row.id,
                    "media_asset_id": row.media_asset_id,
                    "kind": row.kind,
                    "constraint_key": row.constraint_key,
                    "object_deleted": object_deleted,
                    "object_shared": shared,
                }
            )

    return RenditionGcSweep(
        examined=examined,
        claimed=len(claimed_ids),
        deleted_rows=deleted_rows,
        objects_deleted=objects_deleted,
        objects_kept_shared=objects_kept_shared,
        kept_current_profile=kept_current,
        contended=contended,
    )


def active_reference_profiles(providers: dict[str, Any]) -> frozenset[str]:
    """The constraint identities every currently registered provider declares."""

    profiles: set[str] = set()
    for provider in providers.values():
        constraints = getattr(provider, "reference_constraints", None)
        if constraints is None:
            continue
        try:
            profiles.add(constraints.key())
        except Exception:  # pragma: no cover - a malformed declaration
            continue
    return frozenset(profiles)


__all__ = [
    "DEFAULT_RENDITION_GC_LIMIT",
    "RenditionGcSweep",
    "active_reference_profiles",
    "sweep_rendition_gc",
]
