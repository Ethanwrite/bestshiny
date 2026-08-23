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
from sqlalchemy.orm import Session

_SHA256_LENGTH = 64


class DirectUploadUnsupported(RuntimeError):
    """This storage backend cannot accept a direct upload."""


class DirectUploadConflict(RuntimeError):
    """The upload is not in a state that can be completed."""


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
            if existing_upload is not None:
                if (
                    existing_upload.sha256 != digest
                    or existing_upload.asset_type != normalized_type
                    or existing_upload.storage_key != storage_key
                ):
                    raise DirectUploadConflict(
                        "Idempotency-Key was already used for a different upload"
                    )
                if existing_upload.status != DirectUploadStatus.PENDING.value:
                    raise DirectUploadConflict("this upload has already been completed")
                upload_id = existing_upload.id
                expires_at = _aware(existing_upload.expires_at)
                replay = True
            else:
                upload_id, expires_at, replay = new_id(), utcnow() + timedelta(
                    seconds=self.ttl_seconds
                ), False
            duplicate = session.scalar(
                select(MediaAsset).where(
                    MediaAsset.project_id == project_id,
                    MediaAsset.sha256 == digest,
                    MediaAsset.asset_type == normalized_type,
                    MediaAsset.lineage_key == lineage_key,
                )
            )
            duplicate_id = duplicate.id if duplicate else None

        presigned = self.storage.presigned_upload(
            storage_key,
            sha256=digest,
            mime_type=mime_type,
            expires_in=self.ttl_seconds,
        )
        if presigned is None:
            raise DirectUploadUnsupported(
                "the configured storage backend cannot accept a direct upload; "
                "configure S3-compatible storage, or use the multipart upload endpoint"
            )

        if replay:
            return AuthorizedUpload(upload_id, presigned, expires_at, duplicate_id)

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
        return AuthorizedUpload(upload_id, presigned, expires_at, duplicate_id)

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

    def mark_completed(self, session: Session, upload_id: str, *, media_asset_id: str) -> None:
        upload = session.get(DirectUpload, upload_id)
        if upload is None:  # pragma: no cover - guarded by the caller.
            raise LookupError("upload not found")
        upload.status = DirectUploadStatus.COMPLETED.value
        upload.media_asset_id = media_asset_id

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
    "DirectUploadConflict",
    "DirectUploadNotFinished",
    "DirectUploadService",
    "DirectUploadUnsupported",
    "UnsafeMediaUpload",
]
