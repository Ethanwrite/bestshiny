from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from PIL import Image, UnidentifiedImageError

from .storage import StorageLimitExceeded


class UnsafeMediaUpload(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedMedia:
    mime_type: str
    extension: str
    media_kind: str


_IMAGE_TYPES = {
    ".png": ("image/png", "PNG"),
    ".jpg": ("image/jpeg", "JPEG"),
    ".jpeg": ("image/jpeg", "JPEG"),
    ".webp": ("image/webp", "WEBP"),
}
_VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}
_IMAGE_ONLY_ASSET_TYPES = {
    "IMAGE",
    "CHARACTER_REFERENCE",
    "LOCATION_REFERENCE",
    "PROP_REFERENCE",
    "START_FRAME",
    "END_FRAME",
    "GENERATED_FRAME",
    "CHARACTER_MASTER",
    "LOCATION_MASTER",
    "PROP_MASTER",
    "KEYFRAME",
}
_VIDEO_ONLY_ASSET_TYPES = {"VIDEO"}
_FLEXIBLE_ASSET_TYPES = {"REFERENCE"}

SAFE_INLINE_MEDIA_TYPES = frozenset(
    {definition[0] for definition in _IMAGE_TYPES.values()} | set(_VIDEO_TYPES.values())
)


def _size(stream: BinaryIO) -> int:
    try:
        stream.seek(0, 2)
        return stream.tell()
    finally:
        stream.seek(0)


def _validate_image(stream: BinaryIO, *, expected_format: str, max_image_pixels: int) -> None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(stream) as image:
                if image.width * image.height > max_image_pixels:
                    raise UnsafeMediaUpload(f"image exceeds the {max_image_pixels}-pixel safety limit")
                image.verify()
                if image.format != expected_format:
                    raise UnsafeMediaUpload("image contents do not match the filename and MIME type")
    except UnsafeMediaUpload:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise UnsafeMediaUpload("uploaded image could not be decoded safely") from exc
    finally:
        stream.seek(0)


def _detect_video(header: bytes) -> str | None:
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/quicktime" if header[8:12] == b"qt  " else "video/mp4"
    if header.startswith(b"\x1aE\xdf\xa3") and b"webm" in header.lower():
        return "video/webm"
    return None


def validate_user_media_upload(
    stream: BinaryIO,
    *,
    filename: str,
    declared_mime: str | None,
    asset_type: str,
    max_bytes: int,
    max_image_pixels: int = 50_000_000,
) -> ValidatedMedia:
    """Accept only decodable raster images or recognized video containers.

    The extension, HTTP-declared MIME, detected content, and logical asset type
    must all agree. Active formats such as SVG and HTML are never accepted.
    """

    if _size(stream) > max_bytes:
        raise StorageLimitExceeded(max_bytes)
    safe_name = Path(filename).name
    extension = Path(safe_name).suffix.lower()
    declared = (declared_mime or "").split(";", 1)[0].strip().lower()
    normalized_asset_type = asset_type.strip().upper()
    if normalized_asset_type not in (
        _IMAGE_ONLY_ASSET_TYPES | _VIDEO_ONLY_ASSET_TYPES | _FLEXIBLE_ASSET_TYPES
    ):
        raise UnsafeMediaUpload("this asset type does not support user file uploads")

    if extension in _IMAGE_TYPES:
        expected_mime, expected_format = _IMAGE_TYPES[extension]
        if declared != expected_mime:
            raise UnsafeMediaUpload("image MIME type does not match its filename")
        if normalized_asset_type in _VIDEO_ONLY_ASSET_TYPES:
            raise UnsafeMediaUpload("a video asset requires a supported video file")
        _validate_image(
            stream,
            expected_format=expected_format,
            max_image_pixels=max(1, max_image_pixels),
        )
        return ValidatedMedia(expected_mime, extension, "image")

    if extension in _VIDEO_TYPES:
        expected_mime = _VIDEO_TYPES[extension]
        if declared != expected_mime:
            raise UnsafeMediaUpload("video MIME type does not match its filename")
        if normalized_asset_type in _IMAGE_ONLY_ASSET_TYPES:
            raise UnsafeMediaUpload("an image asset requires a supported raster image")
        header = stream.read(4096)
        stream.seek(0)
        if _detect_video(header) != expected_mime:
            raise UnsafeMediaUpload("video contents do not match its filename and MIME type")
        return ValidatedMedia(expected_mime, extension, "video")

    raise UnsafeMediaUpload("only PNG, JPEG, WebP, MP4, MOV, and WebM files are accepted")
