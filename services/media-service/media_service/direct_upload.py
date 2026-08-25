"""Two-phase direct-to-storage upload: authorize, then adopt.

Reads already bypass this service — a provider fetches a reference straight from
object storage. Writes were the remaining half of the same problem: a user
uploading a 38 MB plate streamed it through the control plane on its way to a
bucket that could have received it directly.

The shape:

```text
client ──1. authorize──► API        (tenancy, quota hold, key, presigned PUT)
client ──2. PUT bytes──► object storage
client ──3. complete───► API        (HEAD + bounded header read, register)
```

Two things make phase 3 trustworthy without reading the object:

- the **size** comes from the store's own `HEAD`, never from the client;
- the **digest** is bound into the presigned PUT, so the store rejects bytes
  that do not hash to it. That is what allows a client-declared SHA-256 to
  content-address the key.

What is deliberately given up is the full decode that the multipart path
performs. A truncated file passes header validation and fails at first use,
where `RenditionResolver` already decodes and raises. Pulling every upload back
through the API to catch it one step earlier would undo the whole point.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from platform_shared import (
    MEDIA_HEADER_BYTES,
    PresignedUpload,
    StorageLimitExceeded,
    StorageProvider,
    UnsafeMediaUpload,
    validate_direct_upload_header,
)
from production_domain.models import (
    DirectUpload,
    DirectUploadStatus,
    MediaAsset,
    new_id,
    utcnow,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

_SHA256_LENGTH = 64


class DirectUploadUnsupported(RuntimeError):
    """This storage backend cannot accept a direct upload."""


class DirectUploadConflict(RuntimeError):
    """The upload is not in a state that can be completed."""


class DirectUploadExpired(RuntimeError):
    """The authorized window closed before the client finished.

    Distinct from a conflict because the caller must do something with the
    remains: the row can never complete, so its quota hold is capacity no
    upload will ever use.
    """


class DirectUploadNotFinished(RuntimeError):
    """The object is not in storage, or does not match what was authorized."""


@dataclass(frozen=True)
class AuthorizedUpload:
    upload_id: str
    presigned: PresignedUpload
    expires_at: datetime
    # Set when this exact content already exists for the project: the client can
    # skip the transfer entirely rather than re-uploading bytes we already hold.
    existing_asset_id: str | None = None


@dataclass(frozen=True)
class CompletionClaim:
    """Who owns this completion, and what they must settle."""

    claimed: bool
    reservation_id: str | None = None
    # Set when another request already completed this upload: its asset, not a
    # second one.
    media_asset_id: str | None = None


class DirectUploadService:
    """Owns the authorize/adopt pair. Never touches the object's body."""

    version = "direct-upload-v1"

    def __init__(
        self,
        database,  # type: ignore[no-untyped-def]
        storage: StorageProvider,
        *,
        max_upload_bytes: int,
        max_image_pixels: int,
        ttl_seconds: int = 3600,
    ):
        self.database = database
        self.storage = storage
        self.max_upload_bytes = max(1, max_upload_bytes)
        self.max_image_pixels = max(1, max_image_pixels)
        self.ttl_seconds = max(60, ttl_seconds)

    @staticmethod
    def _storage_key(sha256: str, filename: str) -> str:
        """Content-addressed, exactly as the streaming path keys its objects.

        Safe here only because the store enforces the digest on write; a
        client-chosen key or an unenforced digest would let one upload name
        itself after another's content.
        """

        suffix = Path(Path(filename).name).suffix.lower()
        return f"{sha256[:2]}/{sha256}{suffix}"

    def authorize(
        self,
        *,
        project_id: str,
        workspace_id: str | None,
        created_by_user_id: str | None,
        asset_type: str,
        filename: str,
        mime_type: str,
        sha256: str,
        size_bytes: int,
        idempotency_key: str,
        lineage_key: str = "shared",
        shot_id: str | None = None,
        character_id: str | None = None,
        reservation_id: str | None = None,
    ) -> AuthorizedUpload:
        """Authorize one upload and return a presigned PUT for it."""

        digest = sha256.strip().lower()
        if len(digest) != _SHA256_LENGTH or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("sha256 must be 64 hexadecimal characters")
        if size_bytes <= 0:
            raise ValueError("declared size must be at least one byte")
        if size_bytes > self.max_upload_bytes:
            raise StorageLimitExceeded(self.max_upload_bytes)
        normalized_type = asset_type.strip().upper()
        safe_name = Path(filename).name or "asset.bin"
        storage_key = self._storage_key(digest, safe_name)

        with self.database.session() as session:
            existing_upload = session.scalar(
                select(DirectUpload).where(
                    DirectUpload.project_id == project_id,
                    DirectUpload.idempotency_key == idempotency_key,
                )
            )
            duplicate = session.scalar(
                select(MediaAsset).where(
                    MediaAsset.project_id == project_id,
                    MediaAsset.sha256 == digest,
                    MediaAsset.asset_type == normalized_type,
                    MediaAsset.lineage_key == lineage_key,
                )
            )
            duplicate_id = duplicate.id if duplicate else None
            if existing_upload is not None:
                session.expunge(existing_upload)

        if existing_upload is not None:
            return self._replay(
                existing_upload,
                digest=digest,
                asset_type=normalized_type,
                storage_key=storage_key,
                mime_type=mime_type,
                duplicate_id=duplicate_id,
            )

        upload_id = new_id()
        expires_at = utcnow() + timedelta(seconds=self.ttl_seconds)
        presigned = self._presign(storage_key, digest, mime_type, self.ttl_seconds)
        try:
            with self.database.session() as session:
                session.add(
                    DirectUpload(
                        id=upload_id,
                        project_id=project_id,
                        workspace_id=workspace_id,
                        created_by_user_id=created_by_user_id,
                        idempotency_key=idempotency_key,
                        asset_type=normalized_type,
                        filename=safe_name,
                        mime_type=mime_type,
                        sha256=digest,
                        declared_size_bytes=size_bytes,
                        storage_key=storage_key,
                        lineage_key=lineage_key,
                        shot_id=shot_id,
                        character_id=character_id,
                        storage_reservation_id=reservation_id,
                        status=DirectUploadStatus.PENDING.value,
                        expires_at=expires_at,
                        metadata_json={"service_version": self.version},
                    )
                )
                session.flush()
        except IntegrityError:
            # A concurrent first authorization won this Idempotency-Key. It is
            # the same upload, so this is a replay rather than a failure — the
            # read and the insert are separate transactions, and without this
            # the loser surfaced a raw IntegrityError as a 500. A workspace-
            # backed project is serialized earlier by the reservation's unique
            # constraint; a project with no workspace has no such guard.
            with self.database.session() as session:
                winner = session.scalar(
                    select(DirectUpload).where(
                        DirectUpload.project_id == project_id,
                        DirectUpload.idempotency_key == idempotency_key,
                    )
                )
                if winner is None:
                    raise
                session.expunge(winner)
            return self._replay(
                winner,
                digest=digest,
                asset_type=normalized_type,
                storage_key=storage_key,
                mime_type=mime_type,
                duplicate_id=duplicate_id,
            )
        return AuthorizedUpload(upload_id, presigned, expires_at, duplicate_id)

    def _presign(self, storage_key: str, digest: str, mime_type: str, expires_in: int) -> PresignedUpload:
        presigned = self.storage.presigned_upload(
            storage_key,
            sha256=digest,
            mime_type=mime_type,
            expires_in=expires_in,
        )
        if presigned is None:
            raise DirectUploadUnsupported(
                "the configured storage backend cannot accept a direct upload; "
                "configure S3-compatible storage, or use the multipart upload endpoint"
            )
        return presigned

    def _replay(
        self,
        upload: DirectUpload,
        *,
        digest: str,
        asset_type: str,
        storage_key: str,
        mime_type: str,
        duplicate_id: str | None,
    ) -> AuthorizedUpload:
        """Re-issue the credential for an upload this key already authorized."""

        if upload.sha256 != digest or upload.asset_type != asset_type or upload.storage_key != storage_key:
            raise DirectUploadConflict("Idempotency-Key was already used for a different upload")
        if upload.status != DirectUploadStatus.PENDING.value:
            raise DirectUploadConflict(
                f"this upload is {upload.status.lower()} and cannot be re-authorized"
            )
        expires_at = _aware(upload.expires_at)
        # The row's deadline is authoritative, so a replay is presigned for the
        # time that is left rather than a fresh full TTL. Handing out a URL that
        # outlives the deadline this call reports would make the response a lie
        # about when the window closes.
        expires_in = int((expires_at - utcnow()).total_seconds())
        if expires_in <= 0:
            raise DirectUploadExpired(
                "the authorized upload window has closed; authorize again with a new "
                "Idempotency-Key"
            )
        presigned = self._presign(storage_key, digest, mime_type, expires_in)
        return AuthorizedUpload(upload.id, presigned, expires_at, duplicate_id)

    def find_by_idempotency_key(
        self, *, project_id: str, idempotency_key: str
    ) -> DirectUpload | None:
        """The upload this key already authorized, whatever state it is in.

        A caller holding a quota reservation needs this *before* it reserves:
        re-authorizing is a replay of one upload, not a second one, and the
        hold it already owns is the hold it keeps.
        """

        with self.database.session() as session:
            upload = session.scalar(
                select(DirectUpload).where(
                    DirectUpload.project_id == project_id,
                    DirectUpload.idempotency_key == idempotency_key,
                )
            )
            if upload is not None:
                session.expunge(upload)
            return upload

    def pending(self, upload_id: str) -> DirectUpload:
        with self.database.session() as session:
            upload = session.get(DirectUpload, upload_id)
            if upload is None:
                raise LookupError("upload not found")
            session.expunge(upload)
            return upload

    def verify_object(self, upload: DirectUpload) -> tuple[int, str]:
        """Confirm the object landed, and validate it from a bounded prefix.

        Returns the store-reported size and the validated MIME type. The client
        never supplies either: a declared size is a claim, and `HEAD` is a fact.
        """

        stat = self.storage.stat(upload.storage_key)
        if stat is None or stat.size <= 0:
            raise DirectUploadNotFinished(
                "the object is not present in storage; complete the PUT before finishing"
            )
        if stat.size > self.max_upload_bytes:
            raise StorageLimitExceeded(self.max_upload_bytes)
        header = self.storage.read_prefix(upload.storage_key, MEDIA_HEADER_BYTES)
        if not header:
            raise DirectUploadNotFinished("the uploaded object could not be read back")
        validated = validate_direct_upload_header(
            header,
            filename=upload.filename,
            declared_mime=upload.mime_type,
            asset_type=upload.asset_type,
            size_bytes=stat.size,
            max_bytes=self.max_upload_bytes,
            max_image_pixels=self.max_image_pixels,
        )
        return stat.size, validated.mime_type

    def claim_completion(self, session: Session, upload_id: str) -> CompletionClaim:
        """Take exclusive ownership of one upload's completion.

        Two requests can both find the object in storage and both try to adopt
        it. The `media_assets` unique constraint resolves *that* — one of them is
        told the asset was reused. What must not happen twice is the
        **settlement**: the caller told `reused=True` settles zero bytes, and if
        it settles first the other's settlement is swallowed as a replay and the
        workspace never accounts for the object at all.

        So the row is locked and exactly one caller moves it out of `PENDING`.
        The loser is handed the winner's asset instead of a second accounting.

        The caller owns the session on purpose: the claim, the asset row and the
        quota settlement are one transaction, so a process death between them
        rolls back to a `PENDING` upload with its hold intact rather than
        leaving storage that is real and uncounted.
        """

        upload = session.get(DirectUpload, upload_id, with_for_update=True)
        if upload is None:  # pragma: no cover - guarded by the caller.
            raise LookupError("upload not found")
        if upload.status != DirectUploadStatus.PENDING.value:
            return CompletionClaim(False, media_asset_id=upload.media_asset_id)
        return CompletionClaim(True, reservation_id=upload.storage_reservation_id)

    def mark_completed(self, session: Session, upload_id: str, *, media_asset_id: str) -> None:
        """Record the adopted asset. Only the holder of the claim may call this."""

        upload = session.get(DirectUpload, upload_id)
        if upload is None:  # pragma: no cover - guarded by the caller.
            raise LookupError("upload not found")
        upload.status = DirectUploadStatus.COMPLETED.value
        upload.media_asset_id = media_asset_id

    def expired(self, *, limit: int = 200, now: datetime | None = None) -> list[DirectUpload]:
        """`PENDING` uploads whose authorized window has closed.

        These can never complete: the presigned PUT they were issued has
        expired, so no object can still arrive under that authorization. Until
        something sweeps them the row, its quota hold and any object the client
        did manage to write all persist forever — `expires_at` was written and
        indexed and nothing ever read it.
        """

        deadline = now or utcnow()
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(DirectUpload)
                    .where(
                        DirectUpload.status == DirectUploadStatus.PENDING.value,
                        DirectUpload.expires_at < deadline,
                    )
                    .order_by(DirectUpload.expires_at)
                    .limit(max(1, limit))
                )
            )
            for row in rows:
                session.expunge(row)
            return rows

    def abandon(self, upload_id: str) -> None:
        with self.database.session() as session:
            upload = session.get(DirectUpload, upload_id)
            if upload is None or upload.status != DirectUploadStatus.PENDING.value:
                return
            upload.status = DirectUploadStatus.ABANDONED.value


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "AuthorizedUpload",
    "CompletionClaim",
    "DirectUploadConflict",
    "DirectUploadExpired",
    "DirectUploadNotFinished",
    "DirectUploadService",
    "DirectUploadUnsupported",
    "UnsafeMediaUpload",
]
