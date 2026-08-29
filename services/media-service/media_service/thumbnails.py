"""Thumbnails: the small copy a gallery reads instead of the 4K original.

`MediaRenditionKind.THUMBNAIL` existed in the schema with nothing generating
one (OPEN_ISSUES 2.6), so the Web UI's galleries downloaded originals. This
module is the generation chain: lazy, cached as a rendition (same lifecycle,
revival and GC protections as every derived copy), and derived without ever
touching the original — images through Pillow, videos through an ffmpeg
first-frame extraction bounded to the same box.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from PIL.Image import Resampling
from platform_database import Database
from platform_shared import StorageProvider
from production_domain.models import MediaAsset, MediaRendition, MediaRenditionKind, new_id
from sqlalchemy import select

from .renditions import insert_or_revive_rendition, touch_rendition_access

#: One thumbnail per asset; bump the key to regenerate the fleet lazily.
THUMBNAIL_CONSTRAINT_KEY = "thumbnail-v1-512"
THUMBNAIL_BOX = 512
THUMBNAIL_JPEG_QUALITY = 80


class ThumbnailUnavailable(RuntimeError):
    """This asset has no thumbnail and one cannot be derived from it."""


@dataclass(frozen=True)
class ResolvedThumbnail:
    storage_key: str
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None


class ThumbnailService:
    version = "thumbnail-v1"

    def __init__(self, database: Database, storage: StorageProvider):
        self.database = database
        self.storage = storage

    def ensure_thumbnail(self, asset_id: str) -> ResolvedThumbnail:
        """The asset's thumbnail, derived on first request and cached after."""

        with self.database.session() as session:
            asset = session.get(MediaAsset, asset_id)
            if asset is None:
                raise LookupError("media asset not found")
            existing = session.scalar(
                select(MediaRendition).where(
                    MediaRendition.media_asset_id == asset_id,
                    MediaRendition.kind == MediaRenditionKind.THUMBNAIL.value,
                    MediaRendition.constraint_key == THUMBNAIL_CONSTRAINT_KEY,
                ).with_for_update()
            )
            if existing is not None and existing.lifecycle_status == "ACTIVE":
                touch_rendition_access(existing)
                return ResolvedThumbnail(
                    storage_key=existing.storage_key,
                    mime_type=existing.mime_type,
                    size_bytes=existing.size_bytes,
                    width=existing.width,
                    height=existing.height,
                )
            mime = asset.mime_type.lower()
            if mime.startswith("image/"):
                payload, width, height = self._image_thumbnail(asset)
            elif mime.startswith("video/"):
                payload, width, height = self._video_thumbnail(asset)
            else:
                raise ThumbnailUnavailable(f"no thumbnail can be derived from {asset.mime_type}")
            stored = self.storage.put(
                io.BytesIO(payload),
                filename=f"{asset.sha256}-thumb.jpg",
                mime_type="image/jpeg",
            )
            rendition = insert_or_revive_rendition(
                session,
                MediaRendition(
                    id=new_id(),
                    media_asset_id=asset.id,
                    kind=MediaRenditionKind.THUMBNAIL.value,
                    constraint_key=THUMBNAIL_CONSTRAINT_KEY,
                    storage_key=stored.key,
                    local_path=stored.local_path,
                    mime_type="image/jpeg",
                    sha256=stored.sha256,
                    size_bytes=stored.size,
                    width=width,
                    height=height,
                    metadata_json={
                        "derived_from_sha256": asset.sha256,
                        "thumbnail_version": self.version,
                        "box": THUMBNAIL_BOX,
                    },
                ),
            )
            return ResolvedThumbnail(
                storage_key=rendition.storage_key,
                mime_type=rendition.mime_type,
                size_bytes=rendition.size_bytes,
                width=rendition.width,
                height=rendition.height,
            )

    # ------------------------------------------------------------------ image
    def _image_thumbnail(self, asset: MediaAsset) -> tuple[bytes, int, int]:
        try:
            with self.storage.open(asset.storage_key, "rb") as stream:
                source = stream.read()
        except (FileNotFoundError, OSError) as exc:
            raise ThumbnailUnavailable(f"original bytes for {asset.id} are unreadable") from exc
        try:
            with Image.open(io.BytesIO(source)) as opened:
                converted = opened.convert("RGB")
                converted.thumbnail((THUMBNAIL_BOX, THUMBNAIL_BOX), Resampling.LANCZOS)
                buffer = io.BytesIO()
                converted.save(buffer, format="JPEG", quality=THUMBNAIL_JPEG_QUALITY)
                return buffer.getvalue(), converted.width, converted.height
        except OSError as exc:
            raise ThumbnailUnavailable(f"original for {asset.id} does not decode") from exc

    # ------------------------------------------------------------------ video
    def _video_thumbnail(self, asset: MediaAsset) -> tuple[bytes, int, int]:
        with tempfile.TemporaryDirectory(prefix="thumbnail-") as workdir:
            source_path = Path(workdir) / "source"
            try:
                with self.storage.open(asset.storage_key, "rb") as stream:
                    with source_path.open("wb") as spool:
                        shutil.copyfileobj(stream, spool)
            except (FileNotFoundError, OSError) as exc:
                raise ThumbnailUnavailable(
                    f"original bytes for {asset.id} are unreadable"
                ) from exc
            frame_path = Path(workdir) / "frame.jpg"
            extract = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(source_path),
                    "-vf",
                    (
                        f"scale=w={THUMBNAIL_BOX}:h={THUMBNAIL_BOX}:"
                        "force_original_aspect_ratio=decrease"
                    ),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "4",
                    str(frame_path),
                ],
                capture_output=True,
            )
            if extract.returncode != 0 or not frame_path.is_file():
                raise ThumbnailUnavailable(
                    f"video frame extraction failed for {asset.id}: "
                    + extract.stderr.decode("utf-8", "replace")[-200:]
                )
            payload = frame_path.read_bytes()
            with Image.open(io.BytesIO(payload)) as image:
                return payload, image.width, image.height


__all__ = [
    "THUMBNAIL_BOX",
    "THUMBNAIL_CONSTRAINT_KEY",
    "ResolvedThumbnail",
    "ThumbnailService",
    "ThumbnailUnavailable",
]
