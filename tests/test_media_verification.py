"""Full-content verification of direct uploads: decode everything, trust nothing.

Truncated JPEG/PNG, corrupted MP4, forged MIME, SHA mismatch, crash recovery,
quota release on rejection, and the provider gate on unverified assets.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from media_service import (
    ProviderReferenceUrlUnavailable,
    WorkspaceStorageQuota,
    verify_pending_assets,
)
from PIL import Image
from production_domain.models import MediaAsset, User, Workspace, utcnow
from sqlalchemy import select


def _png_bytes(side: int = 64) -> bytes:
    image = Image.new("RGB", (side, side), (40, 90, 160))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_bytes(side: int = 64) -> bytes:
    image = Image.new("RGB", (side, side), (200, 60, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _adopt(container, project, payload: bytes, *, mime_type: str, declared_sha: str | None = None):  # type: ignore[no-untyped-def]
    """Adopt bytes the way the direct-upload completion does: header-only."""

    stored = container.storage.put(
        io.BytesIO(payload), filename="direct-upload.bin", mime_type=mime_type
    )
    with container.database.session() as session:
        asset, _reused = container.media.adopt_stored_object_in(
            session,
            project.id,
            "PLATE",
            stored.key,
            sha256=declared_sha or hashlib.sha256(payload).hexdigest(),
            mime_type=mime_type,
            size_bytes=len(payload),
        )
        session.flush()
        return asset.id


def _verify(container, **kwargs):  # type: ignore[no-untyped-def]
    return verify_pending_assets(
        database=container.database, storage=container.storage, **kwargs
    )


def _status(container, asset_id: str) -> tuple[str, str | None]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        asset = session.get(MediaAsset, asset_id)
        return asset.verification_status, asset.verification_error


def test_direct_adoption_is_pending_and_the_provider_gate_refuses_it(container, project) -> None:  # type: ignore[no-untyped-def]
    asset_id = _adopt(container, project, _png_bytes(), mime_type="image/png")
    status, _ = _status(container, asset_id)
    assert status == "PENDING_VERIFICATION"
    with pytest.raises(ProviderReferenceUrlUnavailable, match="MEDIA_NOT_VERIFIED"):
        container.media.reference_url(
            asset_id, project_id=project.id, provider="any", require_https=False
        )


def test_a_valid_image_promotes_to_ready_and_serves(container, project) -> None:  # type: ignore[no-untyped-def]
    asset_id = _adopt(container, project, _png_bytes(), mime_type="image/png")
    result = _verify(container)
    assert result.verified_ready == 1
    status, error = _status(container, asset_id)
    assert (status, error) == ("READY", None)
    url = container.media.reference_url(
        asset_id, project_id=project.id, provider="any", require_https=False
    )
    assert url


def test_truncated_jpeg_and_png_become_invalid(container, project) -> None:  # type: ignore[no-untyped-def]
    """The header parses, the tail is missing: exactly what the 64 KB read passed."""

    truncated_png = _png_bytes(512)[: len(_png_bytes(512)) // 2]
    truncated_jpeg = _jpeg_bytes(512)[: len(_jpeg_bytes(512)) // 2]
    png_id = _adopt(container, project, truncated_png, mime_type="image/png")
    jpeg_id = _adopt(container, project, truncated_jpeg, mime_type="image/jpeg")
    result = _verify(container)
    assert result.invalid == 2
    for asset_id in (png_id, jpeg_id):
        status, error = _status(container, asset_id)
        assert status == "INVALID"
        assert error.startswith("IMAGE_DECODE_FAILED")


def test_a_corrupted_mp4_becomes_invalid(container, project, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    clip = tmp_path / "verify-source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi",
            "-i", "testsrc2=size=320x240:rate=24:duration=2",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "veryfast",
            str(clip),
        ],
        check=True,
        capture_output=True,
    )
    payload = clip.read_bytes()
    corrupted = payload[: int(len(payload) * 0.6)]
    asset_id = _adopt(container, project, corrupted, mime_type="video/mp4")
    result = _verify(container)
    assert result.invalid == 1
    status, error = _status(container, asset_id)
    assert status == "INVALID"
    assert error.startswith(("VIDEO_DECODE_FAILED", "VIDEO_PROBE_FAILED"))


def test_forged_mime_is_quarantined_not_merely_invalid(container, project) -> None:  # type: ignore[no-untyped-def]
    asset_id = _adopt(container, project, _png_bytes(), mime_type="image/jpeg")
    result = _verify(container)
    assert result.quarantined == 1
    status, error = _status(container, asset_id)
    assert status == "QUARANTINED"
    assert error.startswith("MIME_FORGED")


def test_sha_mismatch_is_quarantined(container, project) -> None:  # type: ignore[no-untyped-def]
    asset_id = _adopt(
        container,
        project,
        _png_bytes(),
        mime_type="image/png",
        declared_sha=hashlib.sha256(b"something else entirely").hexdigest(),
    )
    result = _verify(container)
    assert result.quarantined == 1
    status, error = _status(container, asset_id)
    assert status == "QUARANTINED"
    assert error.startswith("SHA256_MISMATCH")


def test_a_crashed_verifier_claim_lapses_and_the_asset_reverifies(container, project) -> None:  # type: ignore[no-untyped-def]
    asset_id = _adopt(container, project, _png_bytes(), mime_type="image/png")
    # A worker claimed the row and died mid-decode.
    with container.database.session() as session:
        asset = session.get(MediaAsset, asset_id)
        asset.verification_status = "VERIFYING"
        asset.verification_claimed_at = utcnow() - timedelta(seconds=30)
    fresh = _verify(container, lease_seconds=900)
    assert fresh.examined == 0, "a live claim is not stolen"
    with container.database.session() as session:
        asset = session.get(MediaAsset, asset_id)
        asset.verification_claimed_at = utcnow() - timedelta(seconds=3600)
    recovered = _verify(container, lease_seconds=900)
    assert recovered.verified_ready == 1
    status, _ = _status(container, asset_id)
    assert status == "READY"


def test_rejection_releases_the_settled_storage_bytes(container, project) -> None:  # type: ignore[no-untyped-def]
    payload = _png_bytes(512)[:900]  # truncated: will be INVALID
    asset_id = _adopt(container, project, payload, mime_type="image/png")
    quota = WorkspaceStorageQuota(container.database)
    with container.database.session() as session:
        user = User(email="uploader@example.com", display_name="Uploader", password_hash="unused")
        session.add(user)
        session.flush([user])
        workspace = Workspace(owner_user_id=user.id, name="Verification workspace")
        session.add(workspace)
        session.flush([workspace])
        workspace_id = workspace.id
    reservation = quota.reserve(
        workspace_id=workspace_id,
        project_id=project.id,
        byte_count=len(payload),
        idempotency_key="verify-quota-1",
    )
    with container.database.session() as session:
        asset = session.get(MediaAsset, asset_id)
        quota.settle_in(
            session,
            reservation.id,
            asset_id=asset_id,
            storage_key=asset.storage_key,
            used_bytes=len(payload),
        )
    with container.database.session() as session:
        used_before = session.get(Workspace, workspace_id).used_storage_bytes
    assert used_before == len(payload)
    result = _verify(container, quota=quota)
    assert result.invalid == 1
    assert result.quota_released == 1
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace.used_storage_bytes == 0
        from production_domain.models import StorageReservation

        row = session.scalar(select(StorageReservation))
        assert row.status == "RELEASED_INVALID"
