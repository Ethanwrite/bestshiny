from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ProviderReferenceMode(StrEnum):
    """How a provider expects local media to be handed to it.

    ``PROVIDER_MEDIA_ID`` providers ingest bytes through ``upload_asset`` and
    return a durable remote identifier. ``FETCHABLE_URL`` providers never accept
    an upload; they fetch the bytes themselves and therefore require a real
    URL. Handing such a provider a local asset ID or a provider media ID would
    submit an unresolvable reference, so the two modes must never be mixed.
    """

    PROVIDER_MEDIA_ID = "PROVIDER_MEDIA_ID"
    FETCHABLE_URL = "FETCHABLE_URL"


@dataclass(frozen=True)
class VideoConstraintViolation:
    """One specific way an observed video fails a provider's declared bounds.

    ``adaptable`` records whether transcoding can close the gap without
    changing what the video *means*. Container, codec, resolution, frame rate,
    bitrate and byte size can all be adapted by re-encoding a derived copy.
    Duration and aspect ratio cannot: trimming removes content and cropping
    reframes it, and both are semantic edits nobody asked for. Those fail
    closed until a human makes the cut explicitly.
    """

    code: str
    detail: str
    adaptable: bool

    def __str__(self) -> str:
        return f"{self.code} ({self.detail})"


def _parse_aspect_ratio(value: str) -> float:
    numerator, separator, denominator = value.partition(":")
    if not separator:
        raise ValueError(f"aspect ratio {value!r} is not of the form W:H")
    width, height = float(numerator), float(denominator)
    if width <= 0 or height <= 0:
        raise ValueError(f"aspect ratio {value!r} must have positive terms")
    return width / height


@dataclass(frozen=True)
class VideoReferenceConstraints:
    """What a provider will actually accept as a reference *video*.

    Same philosophy as the image bounds: these are transport facts about one
    provider, never a reason to touch the user's original. A video outside
    them gets a derived, revalidated copy — except where closing the gap would
    change the content itself. Trimming an over-long clip and cropping a
    mismatched aspect ratio are semantic edits, so both fail closed with the
    specific unmet constraint instead of being "fixed" silently; a human crop
    or trim selection is a separate, explicit act.

    ``accepted_containers`` and ``accepted_codecs`` are what the provider
    documents (containers as MIME types, codecs as ffprobe ``codec_name``
    values such as ``h264``/``hevc``/``vp9``). ``accepted_aspect_ratios`` of
    ``None`` accepts any shape. ``None`` on a numeric bound means the provider
    declares no limit there — the dimension is simply not checked.
    """

    accepted_containers: frozenset[str] = frozenset({"video/mp4"})
    preferred_container: str = "video/mp4"
    accepted_codecs: frozenset[str] = frozenset({"h264"})
    preferred_codec: str = "h264"
    accepted_aspect_ratios: frozenset[str] | None = None
    max_width: int | None = None
    max_height: int | None = None
    max_bitrate_bps: int | None = None
    max_frame_rate: float | None = None
    max_duration_seconds: float | None = None
    max_bytes: int | None = None

    # Two observed ratios within 2% describe the same framing; anything past
    # that is a genuinely different shape, not encoder rounding.
    ASPECT_RATIO_TOLERANCE = 0.02

    def __post_init__(self) -> None:
        if not self.accepted_containers:
            raise ValueError("a video constraint must accept at least one container")
        if self.preferred_container not in self.accepted_containers:
            raise ValueError("preferred_container must be one of accepted_containers")
        if not self.accepted_codecs:
            raise ValueError("a video constraint must accept at least one codec")
        if self.preferred_codec not in self.accepted_codecs:
            raise ValueError("preferred_codec must be one of accepted_codecs")
        if self.accepted_aspect_ratios is not None:
            if not self.accepted_aspect_ratios:
                raise ValueError("accepted_aspect_ratios cannot be empty; use None for any")
            for ratio in self.accepted_aspect_ratios:
                _parse_aspect_ratio(ratio)
        for bound_name in (
            "max_width",
            "max_height",
            "max_bitrate_bps",
            "max_frame_rate",
            "max_duration_seconds",
            "max_bytes",
        ):
            bound = getattr(self, bound_name)
            if bound is not None and bound <= 0:
                raise ValueError(f"{bound_name} must be positive when declared")

    def aspect_ratio_accepted(self, width: int, height: int) -> bool:
        if self.accepted_aspect_ratios is None:
            return True
        if width <= 0 or height <= 0:
            return False
        observed = width / height
        return any(
            abs(observed - declared) <= declared * self.ASPECT_RATIO_TOLERANCE
            for declared in map(_parse_aspect_ratio, self.accepted_aspect_ratios)
        )

    def violations(
        self,
        *,
        container_mime_type: str,
        codec: str,
        width: int,
        height: int,
        frame_rate: float,
        duration_seconds: float,
        bit_rate_bps: int,
        size_bytes: int,
        duration_slack_seconds: float = 0.0,
    ) -> tuple[VideoConstraintViolation, ...]:
        """Every declared bound the observed video fails, each named specifically.

        ``duration_slack_seconds`` exists for revalidating a transcoded copy:
        container muxing rounds duration to its timebase, so an output can
        read a few hundredths of a second longer than a source that was
        within the cap. The source itself is always checked with zero slack.
        """

        found: list[VideoConstraintViolation] = []
        if container_mime_type.lower() not in self.accepted_containers:
            found.append(
                VideoConstraintViolation(
                    code="VIDEO_CONTAINER_NOT_ACCEPTED",
                    detail=f"{container_mime_type} is not in {sorted(self.accepted_containers)}",
                    adaptable=True,
                )
            )
        if codec.lower() not in self.accepted_codecs:
            found.append(
                VideoConstraintViolation(
                    code="VIDEO_CODEC_NOT_ACCEPTED",
                    detail=f"{codec} is not in {sorted(self.accepted_codecs)}",
                    adaptable=True,
                )
            )
        if not self.aspect_ratio_accepted(width, height):
            accepted = sorted(self.accepted_aspect_ratios or ())
            found.append(
                VideoConstraintViolation(
                    code="VIDEO_ASPECT_RATIO_NOT_ACCEPTED",
                    detail=(
                        f"{width}x{height} does not match {accepted}; automatic cropping "
                        "changes what the video shows, so an explicit manual crop "
                        "selection is required"
                    ),
                    adaptable=False,
                )
            )
        if self.max_width is not None and width > self.max_width:
            found.append(
                VideoConstraintViolation(
                    code="VIDEO_WIDTH_EXCEEDS_LIMIT",
                    detail=f"{width}px wide exceeds the {self.max_width}px limit",
                    adaptable=True,
                )
            )
        if self.max_height is not None and height > self.max_height:
            found.append(
                VideoConstraintViolation(
                    code="VIDEO_HEIGHT_EXCEEDS_LIMIT",
                    detail=f"{height}px tall exceeds the {self.max_height}px limit",
                    adaptable=True,
                )
            )
        if self.max_bitrate_bps is not None and bit_rate_bps > self.max_bitrate_bps:
            found.append(
                VideoConstraintViolation(
                    code="VIDEO_BITRATE_EXCEEDS_LIMIT",
                    detail=f"{bit_rate_bps} bps exceeds the {self.max_bitrate_bps} bps limit",
                    adaptable=True,
                )
            )
        if self.max_frame_rate is not None and frame_rate > self.max_frame_rate:
            found.append(
                VideoConstraintViolation(
                    code="VIDEO_FRAME_RATE_EXCEEDS_LIMIT",
                    detail=f"{frame_rate:g} fps exceeds the {self.max_frame_rate:g} fps limit",
                    adaptable=True,
                )
            )
        if (
            self.max_duration_seconds is not None
            and duration_seconds > self.max_duration_seconds + duration_slack_seconds
        ):
            found.append(
                VideoConstraintViolation(
                    code="VIDEO_DURATION_EXCEEDS_LIMIT",
                    detail=(
                        f"{duration_seconds:g}s exceeds the {self.max_duration_seconds:g}s limit; "
                        "automatic trimming removes content, so an explicit manual trim "
                        "is required"
                    ),
                    adaptable=False,
                )
            )
        if self.max_bytes is not None and size_bytes > self.max_bytes:
            found.append(
                VideoConstraintViolation(
                    code="VIDEO_BYTES_EXCEED_LIMIT",
                    detail=f"{size_bytes} bytes exceeds the {self.max_bytes}-byte limit",
                    adaptable=True,
                )
            )
        return tuple(found)

    def key_fragment(self) -> str:
        """Stable identity of these bounds, embedded in the rendition cache key."""

        containers = "+".join(sorted(self.accepted_containers))
        codecs = "+".join(sorted(self.accepted_codecs))
        ratios = (
            "+".join(sorted(self.accepted_aspect_ratios))
            if self.accepted_aspect_ratios is not None
            else "any"
        )
        return (
            f"cont={containers};contpref={self.preferred_container};"
            f"cod={codecs};codpref={self.preferred_codec};ar={ratios};"
            f"w={self.max_width or 0};h={self.max_height or 0};"
            f"br={self.max_bitrate_bps or 0};fps={self.max_frame_rate or 0:g};"
            f"dur={self.max_duration_seconds or 0:g};bytes={self.max_bytes or 0}"
        )


@dataclass(frozen=True)
class ProviderReferenceConstraints:
    """What a provider will actually accept as a reference image.

    These are transport facts, not creative ones, and they are the reason
    derived renditions exist. A provider that caps a reference at 8 MB is not a
    reason to store the user's 38 MB original at 8 MB — it is a reason to hand
    that one provider a smaller copy.

    ``max_pixels`` bounds width x height; ``max_bytes`` bounds the encoded file.
    ``accepted_mime_types`` is the set the provider documents; an original
    outside it is re-encoded into ``preferred_mime_type`` rather than rejected.
    ``None`` on a bound means the provider declares no limit, which is not the
    same as an unlimited provider — it means we have not established one, so no
    derived copy is made and the original is sent as-is.

    ``video`` declares how the provider takes a reference *video*. ``None``
    means nobody has established that this provider takes video references at
    all, so a video asset stays unadaptable and fails closed rather than being
    transcoded against guessed limits. A declared ``video`` always validates:
    even an original inside every bound is probed before it is sent.
    """

    max_pixels: int | None = None
    max_bytes: int | None = None
    accepted_mime_types: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/webp"})
    preferred_mime_type: str = "image/jpeg"
    video: VideoReferenceConstraints | None = None

    @property
    def bounded(self) -> bool:
        return self.max_pixels is not None or self.max_bytes is not None or self.video is not None

    def accepts(self, *, mime_type: str, pixels: int | None, size_bytes: int) -> bool:
        if mime_type.lower() not in self.accepted_mime_types:
            return False
        if self.max_pixels is not None and pixels is not None and pixels > self.max_pixels:
            return False
        return not (self.max_bytes is not None and size_bytes > self.max_bytes)

    def key(self) -> str:
        """Stable identity of these bounds, so changed limits do not reuse a copy."""

        formats = "+".join(sorted(self.accepted_mime_types))
        base = (
            f"px={self.max_pixels or 0};bytes={self.max_bytes or 0};"
            f"fmt={formats};pref={self.preferred_mime_type}"
        )
        # Appended only when declared, so every image rendition cached before
        # video constraints existed keeps its key byte-for-byte.
        if self.video is not None:
            return f"{base};video[{self.video.key_fragment()}]"
        return base


@dataclass
class ProviderInlineOutput:
    """One generated artefact the provider returned in the response body itself.

    Synchronous image APIs answer with bytes rather than a fetchable URL, so
    there is nothing for the media registry to download. The bytes travel with
    the result instead, and the registry validates and stores them through the
    same path a downloaded artefact takes.
    """

    content: bytes
    mime_type: str = "image/png"

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("inline provider output cannot be empty")


@dataclass
class ProviderSubmission:
    provider_job_id: str
    raw: dict[str, Any] = field(default_factory=dict)
    # A provider whose generation call is synchronous already holds the terminal
    # result when it returns. Carrying it here lets the Gateway finish through
    # its ordinary completion path instead of polling a job that never existed.
    result: ProviderJob | None = None


@dataclass
class ProviderJob:
    provider_job_id: str
    status: str
    progress: float = 0
    output_url: str | None = None
    output_mime_type: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    # Set instead of ``output_url`` when the provider returned the bytes inline.
    # ``outputs[0]`` is the job's output asset; any further entries are extra
    # images from a batch request and are registered as siblings, never
    # discarded, because the workspace already paid for them.
    outputs: list[ProviderInlineOutput] = field(default_factory=list)

    @property
    def has_output(self) -> bool:
        return bool(self.output_url) or bool(self.outputs)


@dataclass
class ProviderHealth:
    ok: bool
    detail: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderPollIdentity:
    """Server-owned routing identity for a single remote-job poll."""

    local_generation_job_id: str
    provider_account_id: str
    provider_project_id: str
    provider_job_id: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.local_generation_job_id,
                self.provider_account_id,
                self.provider_project_id,
                self.provider_job_id,
            )
        ):
            raise ValueError("provider poll identity fields cannot be empty")


class GenerationProvider(ABC):
    name: str
    reference_mode: ProviderReferenceMode = ProviderReferenceMode.PROVIDER_MEDIA_ID
    # Declared per provider. The default declares no bounds, which means the
    # original is sent unchanged — the honest reading of "we have not
    # established this provider's limits", not a claim that it has none.
    reference_constraints: ProviderReferenceConstraints = ProviderReferenceConstraints()

    @abstractmethod
    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission: ...

    @abstractmethod
    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission: ...

    @abstractmethod
    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str: ...

    @abstractmethod
    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool: ...

    @abstractmethod
    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
        poll_identity: ProviderPollIdentity | None = None,
    ) -> ProviderJob: ...

    @abstractmethod
    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool: ...

    @abstractmethod
    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None: ...

    @abstractmethod
    async def health(self) -> ProviderHealth: ...
