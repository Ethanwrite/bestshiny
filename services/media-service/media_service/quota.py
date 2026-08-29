from __future__ import annotations

from dataclasses import dataclass

from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import StorageReservation, Workspace, utcnow
from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


class WorkspaceStorageQuotaExceeded(RuntimeError):
    pass


class StorageReservationConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class StorageQuotaReservation:
    id: str
    workspace_id: str
    project_id: str
    idempotency_key: str
    reserved_bytes: int
    status: str
    asset_id: str | None
    storage_key: str | None
    replayed: bool = False


class WorkspaceStorageQuota:
    """Durable, fail-closed accounting around a workspace upload.

    A reservation holds capacity before storage is mutated. The hold is either
    converted to used bytes after a new MediaAsset is committed, or released
    after a known pre-registration failure/deduplicated upload. Every counter
    transition and reservation status change share one database transaction.
    """

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _view(
        reservation: StorageReservation,
        *,
        replayed: bool = False,
    ) -> StorageQuotaReservation:
        return StorageQuotaReservation(
            id=reservation.id,
            workspace_id=reservation.workspace_id,
            project_id=reservation.project_id,
            idempotency_key=reservation.idempotency_key,
            reserved_bytes=reservation.reserved_bytes,
            status=reservation.status,
            asset_id=reservation.asset_id,
            storage_key=reservation.storage_key,
            replayed=replayed,
        )

    @staticmethod
    def _hold_capacity(session, workspace_id: str, byte_count: int) -> None:  # type: ignore[no-untyped-def]
        held = session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.status == "ACTIVE",
                Workspace.used_storage_bytes + Workspace.reserved_storage_bytes + byte_count
                <= Workspace.max_storage_bytes,
            )
            .values(reserved_storage_bytes=Workspace.reserved_storage_bytes + byte_count)
        )
        if affected_rows(held) == 1:
            return
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise LookupError("workspace not found")
        if workspace.status != "ACTIVE":
            raise StorageReservationConflict("workspace is not active")
        raise WorkspaceStorageQuotaExceeded("工作空间存储空间不足")

    def reserve(
        self,
        *,
        workspace_id: str,
        project_id: str,
        byte_count: int,
        idempotency_key: str,
    ) -> StorageQuotaReservation:
        if byte_count <= 0:
            raise ValueError("upload must contain at least one byte")
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise ValueError("Idempotency-Key must contain 1-200 characters")
        try:
            with self.database.session() as session:
                existing = session.scalar(
                    select(StorageReservation)
                    .where(
                        StorageReservation.workspace_id == workspace_id,
                        StorageReservation.idempotency_key == key,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    if existing.project_id != project_id or existing.reserved_bytes != byte_count:
                        raise StorageReservationConflict(
                            "Idempotency-Key was already used for a different upload"
                        )
                    if existing.status == "SETTLED":
                        return self._view(existing, replayed=True)
                    if existing.status == "RESERVED":
                        raise StorageReservationConflict("upload is already in progress")
                    if existing.status == "RELEASED":
                        raise StorageReservationConflict(
                            "released Idempotency-Key cannot be reused; submit a new key"
                        )
                    raise StorageReservationConflict(
                        f"unsupported storage reservation state: {existing.status}"
                    )

                self._hold_capacity(session, workspace_id, byte_count)
                reservation = StorageReservation(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    idempotency_key=key,
                    reserved_bytes=byte_count,
                    status="RESERVED",
                )
                session.add(reservation)
                session.flush()
                return self._view(reservation)
        except IntegrityError as exc:
            # The capacity increment and insert roll back together. A concurrent
            # winner now owns this key; callers must replay after it settles.
            raise StorageReservationConflict("upload reservation raced with another request") from exc

    def settle(
        self,
        reservation_id: str,
        *,
        asset_id: str,
        storage_key: str,
        used_bytes: int,
    ) -> StorageQuotaReservation:
        with self.database.session() as session:
            return self.settle_in(
                session,
                reservation_id,
                asset_id=asset_id,
                storage_key=storage_key,
                used_bytes=used_bytes,
            )

    def settle_in(
        self,
        session: Session,
        reservation_id: str,
        *,
        asset_id: str,
        storage_key: str,
        used_bytes: int,
    ) -> StorageQuotaReservation:
        """Settle inside a transaction the caller owns.

        A direct upload registers the asset, marks its upload row completed and
        settles the hold; those three must commit or roll back together, or a
        crash between them leaves storage that is real and unaccounted.
        """

        reservation = session.get(StorageReservation, reservation_id, with_for_update=True)
        if reservation is None:
            raise LookupError("storage reservation not found")
        if used_bytes < 0 or used_bytes > reservation.reserved_bytes:
            raise ValueError("settled bytes must be within the reserved capacity")
        if reservation.status == "SETTLED":
            if reservation.asset_id != asset_id or reservation.storage_key != storage_key:
                raise StorageReservationConflict("reservation was settled for another asset")
            return self._view(reservation, replayed=True)
        if reservation.status != "RESERVED":
            raise StorageReservationConflict("released reservation cannot be settled")

        workspace_update = session.execute(
            update(Workspace)
            .where(
                Workspace.id == reservation.workspace_id,
                Workspace.reserved_storage_bytes >= reservation.reserved_bytes,
                Workspace.used_storage_bytes + used_bytes <= Workspace.max_storage_bytes,
            )
            .values(
                reserved_storage_bytes=(Workspace.reserved_storage_bytes - reservation.reserved_bytes),
                used_storage_bytes=Workspace.used_storage_bytes + used_bytes,
            )
        )
        if affected_rows(workspace_update) != 1:
            raise StorageReservationConflict("workspace storage counters changed unexpectedly")
        reservation.status = "SETTLED"
        reservation.asset_id = asset_id
        reservation.storage_key = storage_key
        reservation.settled_at = utcnow()
        session.flush()
        return self._view(reservation)

    def release_settled_for_asset_in(
        self, session: Session, *, asset_id: str, size_bytes: int
    ) -> bool:
        """Give back the settled bytes of an asset whose object was deleted.

        Called **only** after the bytes are actually gone from storage. That
        ordering is the whole safety argument: releasing quota while the
        object is retained would let repeated invalid uploads grow the bucket
        without bound, so verification deliberately keeps a rejected asset
        charged, and only the reclamation path — which deletes first — hands
        capacity back. The settled reservation becomes ``RELEASED_INVALID``
        (auditable, never deleted) and the workspace's used counter is
        corrected by the recorded size, floored at zero. Returns False when
        no settled reservation exists (a workspace-less project, or an upload
        that predates quota accounting).
        """

        reservation = session.scalar(
            select(StorageReservation)
            .where(
                StorageReservation.asset_id == asset_id,
                StorageReservation.status == "SETTLED",
            )
            .with_for_update()
        )
        if reservation is None:
            return False
        release = min(max(int(size_bytes), 0), reservation.reserved_bytes)
        session.execute(
            update(Workspace)
            .where(Workspace.id == reservation.workspace_id)
            .values(
                used_storage_bytes=case(
                    (Workspace.used_storage_bytes >= release, Workspace.used_storage_bytes - release),
                    else_=0,
                )
            )
        )
        reservation.status = "RELEASED_INVALID"
        session.flush()
        return True

    def record_deduplicated(
        self,
        *,
        workspace_id: str,
        project_id: str,
        byte_count: int,
        idempotency_key: str,
        asset_id: str,
        storage_key: str,
    ) -> StorageQuotaReservation:
        """Audit an exact logical-asset replay without holding extra capacity."""

        if byte_count <= 0:
            raise ValueError("upload must contain at least one byte")
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise ValueError("Idempotency-Key must contain 1-200 characters")
        try:
            with self.database.session() as session:
                existing = session.scalar(
                    select(StorageReservation)
                    .where(
                        StorageReservation.workspace_id == workspace_id,
                        StorageReservation.idempotency_key == key,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    same_upload = (
                        existing.project_id == project_id
                        and existing.reserved_bytes == byte_count
                        and existing.asset_id == asset_id
                        and existing.storage_key == storage_key
                    )
                    if existing.status == "SETTLED" and same_upload:
                        return self._view(existing, replayed=True)
                    raise StorageReservationConflict(
                        "Idempotency-Key was already used for a different upload"
                    )
                reservation = StorageReservation(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    idempotency_key=key,
                    reserved_bytes=byte_count,
                    status="SETTLED",
                    asset_id=asset_id,
                    storage_key=storage_key,
                    settled_at=utcnow(),
                )
                session.add(reservation)
                session.flush()
                return self._view(reservation)
        except IntegrityError as exc:
            raise StorageReservationConflict("upload reservation raced with another request") from exc

    def release(self, reservation_id: str) -> bool:
        with self.database.session() as session:
            return self.release_in(session, reservation_id)

    def release_in(self, session: Session, reservation_id: str) -> bool:
        """Release inside a transaction the caller owns.

        The mirror of `settle_in`, and it exists for the same reason: the sweep
        that reclaims an abandoned upload has to take the upload row and its
        hold in **one** transaction, in the same order completion takes them.
        Releasing in a transaction of its own is what let a sweeper drop a hold
        out from under a completion that already owned the upload row — the
        completion then found the reservation RELEASED, raised
        `StorageReservationConflict` out of `settle_in`, and answered 500.
        """

        reservation = session.get(StorageReservation, reservation_id, with_for_update=True)
        if reservation is None:
            raise LookupError("storage reservation not found")
        if reservation.status == "RELEASED":
            return False
        if reservation.status == "SETTLED":
            return False
        if reservation.status != "RESERVED":
            raise StorageReservationConflict(
                f"unsupported storage reservation state: {reservation.status}"
            )
        released = session.execute(
            update(Workspace)
            .where(
                Workspace.id == reservation.workspace_id,
                Workspace.reserved_storage_bytes >= reservation.reserved_bytes,
            )
            .values(
                reserved_storage_bytes=(Workspace.reserved_storage_bytes - reservation.reserved_bytes)
            )
        )
        if affected_rows(released) != 1:
            raise StorageReservationConflict("workspace storage counters changed unexpectedly")
        reservation.status = "RELEASED"
        reservation.released_at = utcnow()
        session.flush()
        return True
