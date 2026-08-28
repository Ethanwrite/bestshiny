"""Choosing which encoding of an asset a consumer gets, and deriving it when none fits.

The rule this module exists to enforce: **the user's original bytes are never
re-encoded, downscaled or replaced.** A character's face, a product label and a
fabric weave are only ever as good as the file that arrived, and a provider's
current upload cap is a fact about that provider, not about the asset.

So a size- or format-constrained consumer never reads the original directly. It
asks for a rendition that satisfies its declared bounds; if none exists yet one
is derived, stored beside the original and reused from then on. Derivation is
lazy because most assets are never sent anywhere, and durable because the ones
that are get sent repeatedly.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from PIL.Image import Resampling
from platform_shared import StorageProvider
from production_domain.models import MediaAsset, MediaRendition, MediaRenditionKind, new_id
from provider_sdk import ProviderReferenceConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .video_renditions import (
    VideoAdaptationFailed,
    VideoReferenceTranscoder,
    VideoStreamFacts,
)

# Derivation floor. Below this a "reference" has stopped carrying the identity
# information it exists to carry, so failing is more useful than sending it.
MINIMUM_REFERENCE_PIXELS = 256 * 256


class RenditionDerivationFailed(RuntimeError):
    """No encoding of this asset can satisfy the consumer's declared bounds."""


class VideoReferenceUnadaptable(RenditionDerivationFailed):
    """The video fails the consumer's bounds in ways transcoding must not fix.

    ``violations`` names each unmet constraint with a machine-readable code, so
    the refusal that reaches the caller says exactly which bound failed —
    "duration exceeds the limit" or "aspect ratio requires a manual crop" —
    rather than a generic derivation failure.
    """

    def __init__(self, message: str, *, violations: tuple[str, ...] = ()):
        super().__init__(message)
        self.violations = violations


@dataclass(frozen=True)
class ResolvedRendition:
    """The encoding a consumer should read, and whether it is the original."""

    storage_key: str
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None
    kind: str
    derived: bool

    @property
    def is_original(self) -> bool:
        return self.kind == MediaRenditionKind.ORIGINAL.value


def _pixels(width: int | None, height: int | None) -> int | None:
    return width * height if width and height else None


class RenditionResolver:
    """Resolves the best stored encoding of an asset for a set of constraints."""

    version = "rendition-resolver-v1"

    def __init__(
        self,
        storage: StorageProvider,
        *,
        max_derived_bytes: int = 100 * 1024 * 1024,
        video_transcoder: VideoReferenceTranscoder | None = None,
    ):
        self.storage = storage
        self.max_derived_bytes = max_derived_bytes
        self.video_transcoder = video_transcoder or VideoReferenceTranscoder()

    def resolve(
        self,
        session: Session,
        asset: MediaAsset,
        constraints: ProviderReferenceConstraints,
    ) -> ResolvedRendition:
        """Return the encoding satisfying ``constraints``, deriving one if needed."""

        original = ResolvedRendition(
            storage_key=asset.storage_key,
            mime_type=asset.mime_type,
            size_bytes=asset.size_bytes,
            width=asset.width,
            height=asset.height,
            kind=MediaRenditionKind.ORIGINAL.value,
            derived=False,
        )
        # A consumer that declares video bounds always validates video: even an
        # original inside every bound is probed before it is sent, because the
        # provider call may only ever use validated encodings.
        if constraints.video is not None and asset.mime_type.lower().startswith("video/"):
            return self._resolve_video(session, asset, constraints, original)
        # An unbounded consumer is not an unlimited one — it is one whose limits
        # nobody has established. Deriving a copy for it would be guessing, so
        # the original goes as it is.
        if not constraints.bounded and asset.mime_type.lower() in constraints.accepted_mime_types:
            return original
        if constraints.accepts(
            mime_type=asset.mime_type,
            pixels=_pixels(asset.width, asset.height),
            size_bytes=asset.size_bytes,
        ):
            return original
        if not asset.mime_type.startswith("image/"):
            # A consumer that declared no video bounds has not established that
            # it takes video at all; transcoding against guessed limits would
            # ship a reference the provider may reject after it has been billed.
            raise RenditionDerivationFailed(
                f"media asset {asset.id} is {asset.mime_type} and cannot be adapted to "
                f"the consumer's reference constraints"
            )

        constraint_key = constraints.key()
        existing = session.scalar(
            select(MediaRendition).where(
                MediaRendition.media_asset_id == asset.id,
                MediaRendition.kind == MediaRenditionKind.PROVIDER_REFERENCE.value,
                MediaRendition.constraint_key == constraint_key,
            )
        )
        if existing is not None:
            return ResolvedRendition(
                storage_key=existing.storage_key,
                mime_type=existing.mime_type,
                size_bytes=existing.size_bytes,
                width=existing.width,
                height=existing.height,
                kind=existing.kind,
                derived=True,
            )
        return self._derive(session, asset, constraints, constraint_key)

    def _derive(
        self,
        session: Session,
        asset: MediaAsset,
        constraints: ProviderReferenceConstraints,
        constraint_key: str,
    ) -> ResolvedRendition:
        target_mime = self._target_mime(asset.mime_type.lower(), constraints)
        encoding = _ENCODINGS.get(target_mime)
        if encoding is None:
            raise RenditionDerivationFailed(
                f"cannot encode media asset {asset.id} as {target_mime}"
            )
        image_format, extension, supports_quality = encoding

        try:
            with self.storage.open(asset.storage_key, "rb") as stream:
                source = stream.read()
        except (FileNotFoundError, OSError) as exc:
            raise RenditionDerivationFailed(f"original bytes for {asset.id} are unreadable") from exc

        payload, width, height = self._encode_within(
            source,
            constraints,
            image_format=image_format,
            supports_quality=supports_quality,
        )
        if len(payload) > self.max_derived_bytes:
            raise RenditionDerivationFailed(
                f"derived reference for {asset.id} exceeds the derived-object limit"
            )
        stored = self.storage.put(
            io.BytesIO(payload),
            filename=f"{asset.sha256}-ref.{extension}",
            mime_type=target_mime,
        )
        rendition = MediaRendition(
            id=new_id(),
            media_asset_id=asset.id,
            kind=MediaRenditionKind.PROVIDER_REFERENCE.value,
            constraint_key=constraint_key,
            storage_key=stored.key,
            local_path=stored.local_path,
            mime_type=target_mime,
            sha256=stored.sha256,
            size_bytes=stored.size,
            width=width,
            height=height,
            metadata_json={
                "derived_from_sha256": asset.sha256,
                "original_width": asset.width,
                "original_height": asset.height,
                "original_size_bytes": asset.size_bytes,
                "resolver_version": self.version,
            },
        )
        return self._insert_rendition(session, rendition)

    @staticmethod
    def _derived_resolution(row: MediaRendition) -> ResolvedRendition:
        return ResolvedRendition(
            storage_key=row.storage_key,
            mime_type=row.mime_type,
            size_bytes=row.size_bytes,
            width=row.width,
            height=row.height,
            kind=row.kind,
            derived=True,
        )

    def _insert_rendition(self, session: Session, rendition: MediaRendition) -> ResolvedRendition:
        try:
            with session.begin_nested():
                session.add(rendition)
                session.flush()
        except IntegrityError:
            # Another worker derived the same rendition concurrently. Both wrote
            # identical bounds, so either is correct; keep the committed one.
            winner = session.scalar(
                select(MediaRendition).where(
                    MediaRendition.media_asset_id == rendition.media_asset_id,
                    MediaRendition.kind == rendition.kind,
                    MediaRendition.constraint_key == rendition.constraint_key,
                )
            )
            if winner is None:  # pragma: no cover - the conflict implies a winner.
                raise
            return self._derived_resolution(winner)
        return self._derived_resolution(rendition)

    # -- video ----------------------------------------------------------------

    def _video_constraint_key(self, asset: MediaAsset, constraints: ProviderReferenceConstraints) -> str:
        """Cache identity of one adapted video: source bytes, bounds, transcoder.

        Any of the three changing must produce a new rendition — new original
        bytes are a different video, changed bounds are a different target, and
        a new transcoder version means the same input would encode differently.
        The digest keeps the key inside its column whatever the constraint set
        grows to; the readable facts live in the rendition's metadata.
        """

        digest = hashlib.sha256(
            f"{asset.sha256};{constraints.key()};{self.video_transcoder.version}".encode()
        ).hexdigest()
        return f"video:{self.video_transcoder.version}:{digest[:40]}"

    @contextmanager
    def _source_file(self, asset: MediaAsset) -> Iterator[Path]:
        """The original bytes as a real local file, however storage holds them."""

        if asset.local_path and os.path.isfile(asset.local_path):
            yield Path(asset.local_path)
            return
        suffix = {
            "video/mp4": ".mp4",
            "video/quicktime": ".mov",
            "video/webm": ".webm",
        }.get(asset.mime_type.lower(), "")
        temp_path: Path | None = None
        try:
            with self.storage.open(asset.storage_key, "rb") as stream:
                with tempfile.NamedTemporaryFile(
                    prefix="video-original-", suffix=suffix, delete=False
                ) as spool:
                    temp_path = Path(spool.name)
                    shutil.copyfileobj(stream, spool)
        except (FileNotFoundError, OSError) as exc:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise RenditionDerivationFailed(
                f"original bytes for {asset.id} are unreadable"
            ) from exc
        try:
            yield temp_path
        finally:
            temp_path.unlink(missing_ok=True)

    def _resolve_video(
        self,
        session: Session,
        asset: MediaAsset,
        constraints: ProviderReferenceConstraints,
        original: ResolvedRendition,
    ) -> ResolvedRendition:
        """Validate the original against the video bounds, or derive a copy that passes.

        The original is only ever *sent*, never touched: when it fails an
        adaptable bound, ffmpeg writes a separate rendition which is re-probed
        against the full constraint set before it is stored. A gap that only a
        semantic edit could close — trimming duration, cropping to another
        aspect ratio — refuses with the specific unmet constraint instead.
        """

        video = constraints.video
        assert video is not None  # dispatched only when declared
        constraint_key = self._video_constraint_key(asset, constraints)
        existing = session.scalar(
            select(MediaRendition).where(
                MediaRendition.media_asset_id == asset.id,
                MediaRendition.kind == MediaRenditionKind.PROVIDER_REFERENCE.value,
                MediaRendition.constraint_key == constraint_key,
            )
        )
        if existing is not None:
            # Revalidated when it was derived; the row exists only because it
            # passed the same bounds this call would check.
            return self._derived_resolution(existing)

        try:
            with self._source_file(asset) as source_path:
                facts = self.video_transcoder.probe(source_path)
                violations = self.video_transcoder.violations(
                    facts, container_mime_type=asset.mime_type, constraints=video
                )
                if not violations:
                    return original
                unadaptable = tuple(v for v in violations if not v.adaptable)
                if unadaptable:
                    raise VideoReferenceUnadaptable(
                        f"media asset {asset.id} cannot be adapted without changing "
                        f"its content: {'; '.join(str(v) for v in unadaptable)}",
                        violations=tuple(v.code for v in unadaptable),
                    )
                with tempfile.TemporaryDirectory(prefix="video-rendition-") as workdir:
                    result = self.video_transcoder.derive(
                        source_path,
                        facts,
                        source_mime_type=asset.mime_type,
                        constraints=video,
                        violations=violations,
                        workdir=Path(workdir),
                    )
                    if result.facts.size_bytes > self.max_derived_bytes:
                        raise RenditionDerivationFailed(
                            f"derived reference for {asset.id} exceeds the derived-object limit"
                        )
                    with result.path.open("rb") as stream:
                        stored = self.storage.put(
                            stream,
                            filename=f"{asset.sha256}-ref{result.path.suffix}",
                            mime_type=result.mime_type,
                        )
        except VideoAdaptationFailed as exc:
            raise VideoReferenceUnadaptable(
                f"media asset {asset.id} cannot be given a validated video "
                f"reference: {exc}",
                violations=exc.violations,
            ) from exc

        rendition = MediaRendition(
            id=new_id(),
            media_asset_id=asset.id,
            kind=MediaRenditionKind.PROVIDER_REFERENCE.value,
            constraint_key=constraint_key,
            storage_key=stored.key,
            local_path=stored.local_path,
            mime_type=result.mime_type,
            sha256=stored.sha256,
            size_bytes=stored.size,
            width=result.facts.width,
            height=result.facts.height,
            metadata_json={
                "derived_from_sha256": asset.sha256,
                "original_size_bytes": asset.size_bytes,
                "resolver_version": self.version,
                "transcoder_version": self.video_transcoder.version,
                "constraint_profile": constraints.key(),
                "source_probe": _probe_facts(facts),
                "output_probe": _probe_facts(result.facts),
                "adapted_violations": [violation.code for violation in violations],
                "transcode_attempts": result.attempts,
                "remuxed": result.remuxed,
            },
        )
        return self._insert_rendition(session, rendition)

    @staticmethod
    def _target_mime(source_mime: str, constraints: ProviderReferenceConstraints) -> str:
        """Pick the encoding that keeps the most *reference* value inside the bounds.

        Keeping the original's format is the obvious choice and the wrong one
        under a byte cap when that format is lossless: PNG can only shrink by
        losing pixels, so a hard cap turns into a much smaller image. A lossy
        encoder holds far more resolution at the same file size, and resolution
        is what a reference is for — a face at 2048px with mild compression
        carries identity that a pristine 400px face does not.
        """

        preferred = constraints.preferred_mime_type
        if (
            constraints.max_bytes is not None
            and source_mime in _LOSSLESS_MIME_TYPES
            and preferred in constraints.accepted_mime_types
            and preferred not in _LOSSLESS_MIME_TYPES
        ):
            return preferred
        if source_mime in constraints.accepted_mime_types:
            return source_mime
        return preferred

    def _encode_within(
        self,
        source: bytes,
        constraints: ProviderReferenceConstraints,
        *,
        image_format: str,
        supports_quality: bool,
    ) -> tuple[bytes, int, int]:
        """Scale then compress until the payload fits, or fail saying it cannot."""

        try:
            with Image.open(io.BytesIO(source)) as opened:
                opened.load()
                image = opened.convert("RGB") if image_format == "JPEG" else opened.convert("RGBA")
                if image_format == "JPEG" and image.mode != "RGB":  # pragma: no cover - defensive
                    image = image.convert("RGB")
        except Exception as exc:
            raise RenditionDerivationFailed("original bytes are not a decodable image") from exc

        if constraints.max_pixels is not None and image.width * image.height > constraints.max_pixels:
            scale = math.sqrt(constraints.max_pixels / (image.width * image.height))
            width = max(1, int(image.width * scale))
            height = max(1, int(image.height * scale))
            image = image.resize((width, height), Resampling.LANCZOS)

        payload = _encode(image, image_format, quality=92 if supports_quality else None)
        if constraints.max_bytes is None or len(payload) <= constraints.max_bytes:
            return payload, image.width, image.height

        # Compression first: it costs no resolution. Only when quality is
        # exhausted does the reference start losing the detail it carries.
        if supports_quality:
            for quality in (82, 72, 62, 52):
                payload = _encode(image, image_format, quality=quality)
                if len(payload) <= constraints.max_bytes:
                    return payload, image.width, image.height

        while image.width * image.height > MINIMUM_REFERENCE_PIXELS:
            image = image.resize(
                (max(1, image.width * 3 // 4), max(1, image.height * 3 // 4)),
                Resampling.LANCZOS,
            )
            payload = _encode(image, image_format, quality=72 if supports_quality else None)
            if len(payload) <= constraints.max_bytes:
                return payload, image.width, image.height

        raise RenditionDerivationFailed(
            "no encoding above the minimum useful reference resolution fits the "
            f"consumer's {constraints.max_bytes}-byte limit"
        )


def _probe_facts(facts: VideoStreamFacts) -> dict[str, float | int | str | bool]:
    """The observed stream facts, shaped for the rendition's JSON evidence."""

    return {
        "codec": facts.codec,
        "width": facts.width,
        "height": facts.height,
        "frame_rate": round(facts.frame_rate, 3),
        "duration_seconds": round(facts.duration_seconds, 3),
        "bit_rate_bps": facts.bit_rate_bps,
        "size_bytes": facts.size_bytes,
        "has_audio": facts.has_audio,
    }


def _encode(image: Image.Image, image_format: str, *, quality: int | None) -> bytes:
    buffer = io.BytesIO()
    options: dict[str, object] = {}
    if quality is not None:
        options["quality"] = quality
        options["optimize"] = True
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


_LOSSLESS_MIME_TYPES = frozenset({"image/png"})

# mime -> (Pillow format, extension, lossy quality is meaningful)
_ENCODINGS: dict[str, tuple[str, str, bool]] = {
    "image/jpeg": ("JPEG", "jpg", True),
    "image/webp": ("WEBP", "webp", True),
    "image/png": ("PNG", "png", False),
}


__all__ = [
    "MINIMUM_REFERENCE_PIXELS",
    "RenditionDerivationFailed",
    "RenditionResolver",
    "ResolvedRendition",
    "VideoReferenceUnadaptable",
]
