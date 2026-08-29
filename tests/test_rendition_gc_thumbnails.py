"""Rendition lifecycle: access tracking, leased GC, tombstone revival, thumbnails."""

from __future__ import annotations

import io
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from media_service import (
    RenditionResolver,
    ThumbnailService,
    ThumbnailUnavailable,
    sweep_rendition_gc,
)
from media_service.thumbnails import THUMBNAIL_CONSTRAINT_KEY
from PIL import Image
from production_domain.models import MediaAsset, MediaRendition, utcnow
from provider_sdk import ProviderReferenceConstraints
from sqlalchemy import select

#: Two provider constraint generations: deriving under OLD then switching the
#: active set to NEW is exactly the "provider changed its limits" scenario the
#: GC exists for.
OLD_BOUNDS = ProviderReferenceConstraints(max_pixels=64 * 64, max_bytes=900)
NEW_BOUNDS = ProviderReferenceConstraints(max_pixels=128 * 128, max_bytes=64 * 1024)


def _register_image(container, project_id: str, *, side: int = 256, seed: int = 0):  # type: ignore[no-untyped-def]
    image = Image.new("RGB", (side, side), (seed % 255, 120, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return container.media.register(
        project_id,
        "PLATE",
        buffer,
        filename=f"plate-{seed}.png",
        mime_type="image/png",
    )[0]


def _derive(container, asset_id: str, constraints: ProviderReferenceConstraints):  # type: ignore[no-untyped-def]
    resolver = RenditionResolver(container.storage)
    with container.database.session() as session:
        return resolver.resolve(session, session.get(MediaAsset, asset_id), constraints)


def _age_rendition(container, rendition_storage_key: str, *, days: int) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        row = session.scalar(
            select(MediaRendition).where(MediaRendition.storage_key == rendition_storage_key)
        )
        aged = utcnow() - timedelta(days=days)
        row.last_accessed_at = aged
        row.created_at = aged
        return row.id


def test_reuse_touches_last_accessed_at(container, project) -> None:  # type: ignore[no-untyped-def]
    asset = _register_image(container, project.id, seed=1)
    first = _derive(container, asset.id, OLD_BOUNDS)
    assert first.derived
    with container.database.session() as session:
        row = session.scalar(select(MediaRendition))
        row.last_accessed_at = None
    again = _derive(container, asset.id, OLD_BOUNDS)
    assert again.storage_key == first.storage_key
    with container.database.session() as session:
        row = session.scalar(select(MediaRendition))
        assert row.last_accessed_at is not None


def test_gc_reclaims_stale_profile_keeps_current_and_never_originals(  # type: ignore[no-untyped-def]
    container, project
) -> None:
    """The required scenario: constraint change strands old copies; GC takes them."""

    asset = _register_image(container, project.id, seed=2)
    old = _derive(container, asset.id, OLD_BOUNDS)
    new = _derive(container, asset.id, NEW_BOUNDS)
    assert old.storage_key != new.storage_key
    _age_rendition(container, old.storage_key, days=30)
    _age_rendition(container, new.storage_key, days=30)

    result = sweep_rendition_gc(
        database=container.database,
        storage=container.storage,
        active_constraint_profiles=frozenset({NEW_BOUNDS.key()}),
        min_idle_seconds=7 * 24 * 3600,
        limit=10,
    )
    assert len(result.deleted_rows) == 1
    assert result.deleted_rows[0]["constraint_key"] == OLD_BOUNDS.key()
    assert result.kept_current_profile >= 1
    with container.database.session() as session:
        rows = {row.constraint_key: row for row in session.scalars(select(MediaRendition))}
        tombstone = rows[OLD_BOUNDS.key()]
        assert tombstone.lifecycle_status == "DELETED"
        assert tombstone.deleted_at is not None
        assert tombstone.metadata_json["gc"]["sha256"], "the tombstone records what was removed"
        assert rows[NEW_BOUNDS.key()].lifecycle_status == "ACTIVE"
        # The original asset's object is untouched.
        original = session.get(MediaAsset, asset.id)
        assert container.storage.path_for(original.storage_key).is_file()
    # The collected object really is gone from storage.
    assert not container.storage.path_for(old.storage_key).is_file()


def test_gc_respects_the_idle_window(container, project) -> None:  # type: ignore[no-untyped-def]
    asset = _register_image(container, project.id, seed=3)
    _derive(container, asset.id, OLD_BOUNDS)
    result = sweep_rendition_gc(
        database=container.database,
        storage=container.storage,
        active_constraint_profiles=frozenset(),
        min_idle_seconds=7 * 24 * 3600,
        limit=10,
    )
    assert result.deleted_rows == []


def test_a_live_claim_blocks_a_second_sweeper_and_an_expired_one_does_not(  # type: ignore[no-untyped-def]
    container, project
) -> None:
    asset = _register_image(container, project.id, seed=4)
    derived = _derive(container, asset.id, OLD_BOUNDS)
    rendition_id = _age_rendition(container, derived.storage_key, days=30)
    # A competitor holds a live claim.
    with container.database.session() as session:
        row = session.get(MediaRendition, rendition_id)
        row.lifecycle_status = "GC_CLAIMED"
        row.gc_claim_id = "competitor"
        row.gc_claimed_at = utcnow()
    blocked = sweep_rendition_gc(
        database=container.database,
        storage=container.storage,
        active_constraint_profiles=frozenset(),
        min_idle_seconds=60,
        lease_seconds=600,
        limit=10,
    )
    assert blocked.deleted_rows == []
    assert blocked.contended == 1
    # The competitor died: its lease lapses and the row becomes reclaimable.
    with container.database.session() as session:
        row = session.get(MediaRendition, rendition_id)
        row.gc_claimed_at = utcnow() - timedelta(seconds=3600)
    reclaimed = sweep_rendition_gc(
        database=container.database,
        storage=container.storage,
        active_constraint_profiles=frozenset(),
        min_idle_seconds=60,
        lease_seconds=600,
        limit=10,
    )
    assert len(reclaimed.deleted_rows) == 1


def test_a_shared_object_is_never_deleted_from_storage(container, project) -> None:  # type: ignore[no-untyped-def]
    asset = _register_image(container, project.id, seed=5)
    derived = _derive(container, asset.id, OLD_BOUNDS)
    rendition_id = _age_rendition(container, derived.storage_key, days=30)
    # Simulate content-address sharing: an asset row adopts the same key.
    with container.database.session() as session:
        row = session.get(MediaRendition, rendition_id)
        original = session.get(MediaAsset, asset.id)
        original.storage_key = row.storage_key
    result = sweep_rendition_gc(
        database=container.database,
        storage=container.storage,
        active_constraint_profiles=frozenset(),
        min_idle_seconds=60,
        limit=10,
    )
    assert len(result.deleted_rows) == 1
    assert result.objects_kept_shared == 1
    assert container.storage.path_for(derived.storage_key).is_file()


def test_a_collected_rendition_revives_in_place_on_demand(container, project) -> None:  # type: ignore[no-untyped-def]
    asset = _register_image(container, project.id, seed=6)
    derived = _derive(container, asset.id, OLD_BOUNDS)
    _age_rendition(container, derived.storage_key, days=30)
    sweep_rendition_gc(
        database=container.database,
        storage=container.storage,
        active_constraint_profiles=frozenset(),
        min_idle_seconds=60,
        limit=10,
    )
    revived = _derive(container, asset.id, OLD_BOUNDS)
    assert revived.derived
    assert container.storage.path_for(revived.storage_key).is_file()
    with container.database.session() as session:
        rows = list(session.scalars(select(MediaRendition)))
        assert len(rows) == 1, "the tombstone row was revived, not duplicated"
        assert rows[0].lifecycle_status == "ACTIVE"
        assert rows[0].metadata_json.get("revived_from") == "DELETED"


def test_image_thumbnail_is_derived_cached_and_bounded(container, project) -> None:  # type: ignore[no-untyped-def]
    asset = _register_image(container, project.id, side=2048, seed=7)
    thumbnails = ThumbnailService(container.database, container.storage)
    first = thumbnails.ensure_thumbnail(asset.id)
    assert first.mime_type == "image/jpeg"
    assert max(first.width or 0, first.height or 0) <= 512
    with Image.open(container.storage.path_for(first.storage_key)) as image:
        assert max(image.size) <= 512
    again = thumbnails.ensure_thumbnail(asset.id)
    assert again.storage_key == first.storage_key
    with container.database.session() as session:
        rows = list(
            session.scalars(
                select(MediaRendition).where(
                    MediaRendition.constraint_key == THUMBNAIL_CONSTRAINT_KEY
                )
            )
        )
        assert len(rows) == 1
        # The original was not replaced by the small copy.
        original = session.get(MediaAsset, asset.id)
        assert original.width == 2048


def test_video_thumbnail_extracts_a_bounded_frame(container, project, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    clip = tmp_path / "thumb-source.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi",
            "-i", "testsrc2=size=1280x720:rate=24:duration=1",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-preset", "veryfast",
            str(clip),
        ],
        check=True,
        capture_output=True,
    )
    with clip.open("rb") as stream:
        asset = container.media.register(
            project.id, "VIDEO", stream, filename=clip.name, mime_type="video/mp4"
        )[0]
    thumbnails = ThumbnailService(container.database, container.storage)
    thumbnail = thumbnails.ensure_thumbnail(asset.id)
    assert thumbnail.mime_type == "image/jpeg"
    assert max(thumbnail.width or 0, thumbnail.height or 0) <= 512
    with Image.open(container.storage.path_for(thumbnail.storage_key)) as image:
        assert image.format == "JPEG"


def test_unsupported_media_has_no_thumbnail(container, project) -> None:  # type: ignore[no-untyped-def]
    audio = container.media.register(
        project.id,
        "AUDIO",
        io.BytesIO(b"RIFF....WAVEfmt "),
        filename="voice.wav",
        mime_type="audio/wav",
    )[0]
    thumbnails = ThumbnailService(container.database, container.storage)
    with pytest.raises(ThumbnailUnavailable):
        thumbnails.ensure_thumbnail(audio.id)
