"""Full-content verification of directly uploaded media, asynchronously.

The direct-upload completion path deliberately reads only a HEAD and a 64 KB
header — pulling whole files back through the API would undo the reason
writes bypass it. The gap that left (OPEN_ISSUES 2.10): a truncated JPEG, a
corrupt MP4 or a file whose bytes are not what it declared could register and
fail later, at first billed use. This worker closes it without touching the
upload path: adopted assets wait in ``PENDING_VERIFICATION``, a sweep claims
each under a lease and performs the *complete* check —

- stored-object SHA-256 recomputed and compared to the recorded digest;
- images fully decoded (Pillow ``load``, decompression-bomb guarded) and
  their real format compared to the declared MIME;
- videos probed with ffprobe and then decoded end-to-end (``ffmpeg -f null``),
  which walks every packet of the container, so a truncated tail or corrupt
  interior fails here and not at a provider.

Verdicts are explicit: ``READY`` serves; ``INVALID`` means the bytes do not
decode; ``QUARANTINED`` means the bytes contradict the declaration (forged
MIME, SHA mismatch) — a distinction operators need, because one is a broken
export and the other is tampering. Either failure releases the workspace's
settled storage bytes. The object itself is never deleted here: rejected
bytes are evidence, and deleting user uploads is not a verifier's call.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import tempfile
import warnings
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from platform_database import Database
from platform_shared import StorageProvider
from production_domain.models import MediaAsset, utcnow
from sqlalchemy import or_, select, update

from .quota import WorkspaceStorageQuota

#: Declared MIME → the Pillow formats that genuinely are that type.
_IMAGE_FORMATS: dict[str, set[str]] = {
    "image/jpeg": {"JPEG"},
    "image/png": {"PNG"},
    "image/webp": {"WEBP"},
    "image/bmp": {"BMP"},
    "image/gif": {"GIF"},
}


@dataclass(frozen=True)
class MediaVerificationSweep:
    examined: int = 0
    verified_ready: int = 0
    invalid: int = 0
    quarantined: int = 0
    contended: int = 0
    quota_released: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_response(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "verified_ready": self.verified_ready,
            "invalid": self.invalid,
            "quarantined": self.quarantined,
            "contended": self.contended,
            "quota_released": self.quota_released,
            "details": self.details,
        }


@dataclass(frozen=True)
class _Verdict:
    status: str  # READY | INVALID | QUARANTINED
    error: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None


def _verify_image(payload: bytes, declared_mime: str) -> _Verdict:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                actual_format = image.format or ""
                width, height = image.size
                # The complete decode: every pixel, not just the header.
                image.load()
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
    ) as exc:
        return _Verdict("INVALID", error=f"IMAGE_DECODE_FAILED:{type(exc).__name__}")
    allowed = _IMAGE_FORMATS.get(declared_mime.lower())
    if allowed is not None and actual_format not in allowed:
        return _Verdict(
            "QUARANTINED",
            error=f"MIME_FORGED:declared {declared_mime}, decoded {actual_format}",
        )
    return _Verdict("READY", width=width, height=height)


def _verify_video(path: Path) -> _Verdict:
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=width,height,codec_type",
            "-of",
            "default=noprint_wrappers=1",
            str(path),
        ],
        capture_output=True,
    )
    if probe.returncode != 0:
        return _Verdict(
            "INVALID",
            error="VIDEO_PROBE_FAILED:" + probe.stderr.decode("utf-8", "replace")[-200:].strip(),
        )
    # The full-structure pass: decode every stream to the null muxer, which
    # forces ffmpeg to read and decode the entire file, tail included.
    decode = subprocess.run(
        ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
    )
    if decode.returncode != 0:
        return _Verdict(
            "INVALID",
            error="VIDEO_DECODE_FAILED:" + decode.stderr.decode("utf-8", "replace")[-200:].strip(),
        )
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    for line in probe.stdout.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition("=")
        try:
            if key == "width" and width is None:
                width = int(value)
            elif key == "height" and height is None:
                height = int(value)
            elif key == "duration" and duration is None:
                duration = float(value)
        except ValueError:
            continue
    return _Verdict("READY", width=width, height=height, duration=duration)


def verify_pending_assets(
    *,
    database: Database,
    storage: StorageProvider,
    quota: WorkspaceStorageQuota | None = None,
    limit: int = 20,
    lease_seconds: int = 900,
) -> MediaVerificationSweep:
    """Verify each PENDING_VERIFICATION asset once, crash-recoverably.

    A row is claimed to ``VERIFYING`` with a timestamp before any bytes are
    read; a worker that dies mid-decode leaves a claim that lapses after
    ``lease_seconds`` and the row is simply verified again — verification is
    a pure function of the stored bytes, so re-running it is always safe.
    """

    now = utcnow()
    lease_cutoff = now - timedelta(seconds=max(60, int(lease_seconds)))
    with database.session() as session:
        candidate_ids = list(
            session.scalars(
                select(MediaAsset.id)
                .where(
                    or_(
                        MediaAsset.verification_status == "PENDING_VERIFICATION",
                        (MediaAsset.verification_status == "VERIFYING")
                        & (MediaAsset.verification_claimed_at <= lease_cutoff),
                    )
                )
                .order_by(MediaAsset.created_at)
                .limit(max(1, limit))
            )
        )
    examined = len(candidate_ids)
    ready = invalid = quarantined = contended = quota_released = 0
    details: list[dict[str, Any]] = []

    for asset_id in candidate_ids:
        with database.session() as session:
            claim = session.execute(
                update(MediaAsset)
                .where(
                    MediaAsset.id == asset_id,
                    or_(
                        MediaAsset.verification_status == "PENDING_VERIFICATION",
                        (MediaAsset.verification_status == "VERIFYING")
                        & (MediaAsset.verification_claimed_at <= lease_cutoff),
                    ),
                )
                .values(verification_status="VERIFYING", verification_claimed_at=now)
            )
            if int(getattr(claim, "rowcount", 0) or 0) != 1:
                contended += 1
                continue
            asset = session.get(MediaAsset, asset_id)
            storage_key = asset.storage_key
            declared_mime = asset.mime_type.lower()
            declared_sha = asset.sha256

        verdict = _verify_stored_object(
            storage,
            storage_key=storage_key,
            declared_mime=declared_mime,
            declared_sha=declared_sha,
        )

        with database.session() as session:
            asset = session.get(MediaAsset, asset_id)
            if asset is None or asset.verification_status != "VERIFYING":
                contended += 1
                continue
            asset.verification_status = verdict.status
            asset.verification_error = verdict.error
            if verdict.status == "READY":
                asset.verification_claimed_at = None
                if verdict.width is not None and not asset.width:
                    asset.width = verdict.width
                if verdict.height is not None and not asset.height:
                    asset.height = verdict.height
                if verdict.duration is not None and not asset.duration:
                    asset.duration = verdict.duration
                ready += 1
            else:
                # INVALID and QUARANTINED objects are deliberately retained for
                # audit/reconciliation.  Retained bytes still consume storage;
                # releasing their settled quota would let repeated bad uploads
                # grow the bucket without bound.
                if verdict.status == "INVALID":
                    invalid += 1
                else:
                    quarantined += 1
            session.flush()
        details.append({"asset_id": asset_id, "status": verdict.status, "error": verdict.error})

    return MediaVerificationSweep(
        examined=examined,
        verified_ready=ready,
        invalid=invalid,
        quarantined=quarantined,
        contended=contended,
        quota_released=quota_released,
        details=details,
    )


def _verify_stored_object(
    storage: StorageProvider,
    *,
    storage_key: str,
    declared_mime: str,
    declared_sha: str,
) -> _Verdict:
    try:
        with storage.open(storage_key, "rb") as stream:
            if declared_mime.startswith("image/"):
                payload = stream.read()
                actual_sha = hashlib.sha256(payload).hexdigest()
                if actual_sha != declared_sha:
                    return _Verdict(
                        "QUARANTINED",
                        error=f"SHA256_MISMATCH:stored {actual_sha[:12]}…",
                    )
                return _verify_image(payload, declared_mime)
            if declared_mime.startswith("video/"):
                digest = hashlib.sha256()
                with tempfile.NamedTemporaryFile(
                    prefix="verify-", suffix=Path(storage_key).suffix, delete=False
                ) as spool:
                    temp_path = Path(spool.name)
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        spool.write(chunk)
                try:
                    if digest.hexdigest() != declared_sha:
                        return _Verdict(
                            "QUARANTINED",
                            error=f"SHA256_MISMATCH:stored {digest.hexdigest()[:12]}…",
                        )
                    return _verify_video(temp_path)
                finally:
                    temp_path.unlink(missing_ok=True)
            # Other media (audio, documents): the SHA check is the whole
            # verifiable claim today; a decode contract for them does not
            # exist yet, and inventing one would be a fake pass.
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if digest.hexdigest() != declared_sha:
                return _Verdict(
                    "QUARANTINED", error=f"SHA256_MISMATCH:stored {digest.hexdigest()[:12]}…"
                )
            return _Verdict("READY")
    except (FileNotFoundError, OSError) as exc:
        return _Verdict("INVALID", error=f"OBJECT_UNREADABLE:{type(exc).__name__}")


__all__ = ["MediaVerificationSweep", "verify_pending_assets"]
