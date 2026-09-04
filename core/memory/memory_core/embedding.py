from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import Thread
from typing import Literal, Protocol

import httpx
from entitlement_core import ModelRoleRuntime
from PIL import Image
from production_domain.models import EmbeddingEvidence, ModelExecutionRecord
from provider_sdk import LiveProviderGate, LiveProviderSettings

from .schemas import (
    EmbeddingProvenance,
    MultimodalContent,
    VideoFrameLineage,
    VideoFrameReference,
    VideoFrameStatus,
)

EmbeddingInputType = Literal["query", "document"]

# --- Bounded video-frame extraction ----------------------------------------
#
# Voyage's multimodal endpoint documents text and image content
# (https://docs.voyageai.com/reference/multimodal-embeddings-api), and 0071
# prices exactly two things for voyage-multimodal-3.5: text per 1M tokens and
# image input per 1B pixels.  There is no documented video input and no video
# price, so no path here sends a video to Voyage.  A video is embedded as a
# bounded strip of stills taken at fixed positions.

#: Fixed, never adaptive: the same video always yields the same frames, so a
#: re-index is comparable with the memory it replaces and the cost of indexing
#: one video is knowable before the call is made.
VIDEO_FRAME_POSITIONS: tuple[float, ...] = (0.05, 0.35, 0.65, 0.95)
#: Four stills describe a shot's beginning, middle and end well enough for an
#: advisory retrieval vector; more would mostly re-embed the same seconds.
MAX_FRAMES_PER_VIDEO = len(VIDEO_FRAME_POSITIONS)
#: One `MultimodalContent` may carry up to four videos, so the per-content
#: ceiling — not the per-video one — is what actually bounds a single call.
MAX_FRAMES_PER_CONTENT = 8
#: Longest edge of a frame. Beyond this a retrieval embedding gains nothing
#: while the pixel bill and the request body keep growing.
MAX_FRAME_EDGE_PIXELS = 512
#: 512 * 512 = 262_144 pixels, the upper bound for one frame after downscaling.
MAX_FRAME_PIXELS = MAX_FRAME_EDGE_PIXELS * MAX_FRAME_EDGE_PIXELS
#: The whole pixel budget for one indexed memory: 8 * 262_144 = 2_097_152
#: pixels. At 0071's official image rate (USD 0.60 per 1B pixels) that is
#: USD 0.00126 — the hard ceiling on the image side of indexing one memory.
MAX_CONTENT_FRAME_PIXELS = MAX_FRAMES_PER_CONTENT * MAX_FRAME_PIXELS
#: Bytes are bounded separately from pixels because what crosses the wire is
#: base64 of a compressed frame, whose size a pixel count does not predict.
MAX_FRAME_BYTES = 400 * 1024
MAX_CONTENT_FRAME_BYTES = 2 * 1024 * 1024
#: Quality steps tried in order until a frame fits `MAX_FRAME_BYTES`.
_FRAME_JPEG_QUALITIES = (80, 60, 40)
_FRAME_COMMAND_TIMEOUT_SECONDS = 20.0

#: A finished frame is inlined as a data URI. `voyage_provider` translates a
#: `data:` image into the vendor's `image_base64` piece.
_FRAME_MEDIA_TYPE = "image/jpeg"

CommandRunner = Callable[[list[str], float], "subprocess.CompletedProcess[bytes]"]


class MemoryEmbeddingUnavailable(RuntimeError):
    """The optional vector-memory backend could not produce trustworthy evidence."""


@dataclass(frozen=True)
class EmbeddingVector:
    values: list[float]
    provenance: EmbeddingProvenance


@dataclass(frozen=True)
class ExtractedFrame:
    """One downscaled still, ready to be sent as an image input."""

    source_video_url: str
    frame_index: int
    normalized_position: float
    timestamp_seconds: float
    width: int
    height: int
    byte_length: int
    data_uri: str

    def reference(self) -> VideoFrameReference:
        return VideoFrameReference(
            source_video_url=self.source_video_url,
            frame_index=self.frame_index,
            normalized_position=self.normalized_position,
            timestamp_seconds=self.timestamp_seconds,
            width=self.width,
            height=self.height,
            byte_length=self.byte_length,
        )


def _run_command(args: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, check=False, timeout=timeout)


class BoundedVideoFrameSampler:
    """Fixed-position stills for one video, or nothing at all.

    ``qa_core.FFmpegFrameSampler`` samples a *local file* for identity evidence
    and raises on every failure. That is right there and wrong here: memory is
    advisory, its input is an object-storage URL rather than a path, and a
    missing ffmpeg must degrade a retrieval vector rather than break the
    business call that triggered the indexing. The subprocess shape — ffprobe
    for the duration, one ffmpeg seek per position — is deliberately the same.
    """

    version = "memory-video-frame-sampler-v1"

    def __init__(
        self,
        *,
        runner: CommandRunner = _run_command,
        timeout_seconds: float = _FRAME_COMMAND_TIMEOUT_SECONDS,
        positions: tuple[float, ...] = VIDEO_FRAME_POSITIONS,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not positions or len(positions) > MAX_FRAMES_PER_VIDEO:
            raise ValueError(f"between one and {MAX_FRAMES_PER_VIDEO} frame positions are required")
        if any(not 0.0 <= position <= 1.0 for position in positions):
            raise ValueError("frame positions are normalized to [0, 1]")
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.positions = positions

    def sample(self, video_url: str) -> tuple[tuple[ExtractedFrame, ...], tuple[str, ...]]:
        """Return the frames extracted, plus reason codes for what was not."""

        url = video_url.strip()
        if not url.lower().startswith("https://"):
            # ffmpeg would happily open file:// or a plain-http redirect to an
            # internal address. This is the one place memory hands a
            # caller-supplied string to a subprocess, so it accepts exactly the
            # object-storage URLs the media registry issues.
            return (), ("UNSUPPORTED_VIDEO_SOURCE",)
        duration, reason = self._duration(url)
        if duration is None:
            return (), ((reason,) if reason else ("VIDEO_NOT_PROBEABLE",))
        frames: list[ExtractedFrame] = []
        reasons: list[str] = []
        for frame_index, position in enumerate(self.positions):
            # Never seek to the exact final timestamp: the last frame boundary
            # is where a decoder is most likely to return nothing at all.
            timestamp = min(position * duration, max(0.0, duration - 0.05))
            result = self._run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    f"{timestamp:.6f}",
                    "-i",
                    url,
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ]
            )
            if result is None:
                return tuple(frames), (*dict.fromkeys([*reasons, "FFMPEG_UNAVAILABLE"]),)
            if result.returncode or not result.stdout:
                reasons.append("FRAME_EXTRACTION_FAILED")
                continue
            encoded = _bounded_frame(result.stdout)
            if encoded is None:
                reasons.append("FRAME_TOO_LARGE")
                continue
            data_uri, width, height, byte_length = encoded
            frames.append(
                ExtractedFrame(
                    source_video_url=url,
                    frame_index=frame_index,
                    normalized_position=position,
                    timestamp_seconds=round(timestamp, 6),
                    width=width,
                    height=height,
                    byte_length=byte_length,
                    data_uri=data_uri,
                )
            )
        return tuple(frames), tuple(dict.fromkeys(reasons))

    def _duration(self, url: str) -> tuple[float | None, str | None]:
        result = self._run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                url,
            ]
        )
        if result is None:
            return None, "FFMPEG_UNAVAILABLE"
        if result.returncode:
            return None, "VIDEO_NOT_PROBEABLE"
        try:
            payload = json.loads(result.stdout or b"{}") or {}
            duration = float(payload.get("format", {}).get("duration"))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None, "VIDEO_NOT_PROBEABLE"
        if not math.isfinite(duration) or duration <= 0:
            return None, "VIDEO_NOT_PROBEABLE"
        return duration, None

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[bytes] | None:
        """Run one command, or return None when the toolchain is unusable."""

        try:
            return self.runner(args, self.timeout_seconds)
        except (OSError, subprocess.SubprocessError):
            # A missing binary, a permission error or a timeout are all the
            # same fact for an advisory vector: no frames from this host.
            return None


def _bounded_frame(png_bytes: bytes) -> tuple[str, int, int, int] | None:
    """Downscale one PNG frame into a size- and pixel-bounded data URI."""

    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            frame = image.convert("RGB")
            frame.thumbnail((MAX_FRAME_EDGE_PIXELS, MAX_FRAME_EDGE_PIXELS))
            if frame.width * frame.height > MAX_FRAME_PIXELS:  # pragma: no cover - thumbnail invariant
                return None
            for quality in _FRAME_JPEG_QUALITIES:
                buffer = io.BytesIO()
                frame.save(buffer, format="JPEG", quality=quality)
                data = buffer.getvalue()
                if len(data) <= MAX_FRAME_BYTES:
                    encoded = base64.b64encode(data).decode("ascii")
                    return (
                        f"data:{_FRAME_MEDIA_TYPE};base64,{encoded}",
                        frame.width,
                        frame.height,
                        len(data),
                    )
    except (OSError, ValueError):
        return None
    return None


def voyage_content_pieces(
    content: MultimodalContent,
    *,
    sampler: BoundedVideoFrameSampler | None = None,
) -> tuple[list[dict[str, str]], VideoFrameLineage]:
    """Translate memory content into Voyage input pieces plus frame lineage.

    Text and images pass through. Video is replaced by bounded stills; a
    ``video_url`` is never produced, on this path or any other.
    """

    pieces: list[dict[str, str]] = []
    if content.text:
        pieces.append({"type": "text", "text": content.text})
    pieces.extend({"type": "image_url", "image_url": url} for url in content.image_urls)
    if not content.video_urls:
        return pieces, VideoFrameLineage()

    frame_sampler = sampler or BoundedVideoFrameSampler()
    references: list[VideoFrameReference] = []
    reasons: list[str] = []
    total_pixels = 0
    total_bytes = 0
    for video_url in content.video_urls:
        if len(references) >= MAX_FRAMES_PER_CONTENT:
            reasons.append("FRAME_BUDGET_EXHAUSTED")
            break
        frames, frame_reasons = frame_sampler.sample(video_url)
        reasons.extend(frame_reasons)
        for frame in frames:
            pixels = frame.width * frame.height
            if (
                len(references) >= MAX_FRAMES_PER_CONTENT
                or total_pixels + pixels > MAX_CONTENT_FRAME_PIXELS
                or total_bytes + frame.byte_length > MAX_CONTENT_FRAME_BYTES
            ):
                reasons.append("FRAME_BUDGET_EXHAUSTED")
                break
            total_pixels += pixels
            total_bytes += frame.byte_length
            references.append(frame.reference())
            pieces.append({"type": "image_url", "image_url": frame.data_uri})
    lineage = VideoFrameLineage(
        sampler_version=frame_sampler.version,
        status=(VideoFrameStatus.EXTRACTED if references else VideoFrameStatus.UNAVAILABLE),
        source_video_urls=list(content.video_urls),
        frames=references,
        reason_codes=list(dict.fromkeys(reasons)),
        total_pixels=total_pixels,
        total_bytes=total_bytes,
    )
    return pieces, lineage


class EmbeddingProvider(Protocol):
    @property
    def provenance(self) -> EmbeddingProvenance: ...

    def embed(self, content: MultimodalContent, *, input_type: EmbeddingInputType) -> list[float]: ...

    def embed_with_provenance(
        self,
        content: MultimodalContent,
        *,
        input_type: EmbeddingInputType,
        project_id: str,
    ) -> EmbeddingVector: ...


class LocalTestEmbeddingProvider:
    """Deterministic lexical fallback for tests and offline development only.

    It is deliberately labelled local_test and must never be represented as a
    visual or identity judge.
    """

    def __init__(self, dimension: int = 512):
        self.dimension = dimension

    @property
    def provenance(self) -> EmbeddingProvenance:
        return EmbeddingProvenance(
            provider="local_test",
            model="deterministic-token-hash-v1",
            dimension=self.dimension,
            input_type="document",
        )

    def embed(self, content: MultimodalContent, *, input_type: EmbeddingInputType) -> list[float]:
        value = " ".join([content.text, *content.image_urls, *content.video_urls]).lower()
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", value)
        vector = [0.0] * self.dimension
        for token in tokens or ["empty"]:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = -1.0 if digest[4] & 1 else 1.0
            vector[index] += sign
        norm = math.sqrt(sum(item * item for item in vector)) or 1.0
        return [item / norm for item in vector]

    def embed_with_provenance(
        self,
        content: MultimodalContent,
        *,
        input_type: EmbeddingInputType,
        project_id: str,
    ) -> EmbeddingVector:
        del project_id
        values = self.embed(content, input_type=input_type)
        return EmbeddingVector(
            values,
            self.provenance.model_copy(
                update={
                    "input_type": input_type,
                    "evidence_purpose": content.evidence_purpose,
                    "authority_level": content.authority_level,
                }
            ),
        )


class VoyageMultimodalEmbeddingProvider:
    """Legacy direct transport, retained only for the live-gate contract.

    Business code resolves the MULTIMODAL_EMBEDDING role and reaches Voyage
    through ``ModelRoleEmbeddingProvider``; nothing in the container builds
    this class. It stays because it is the one place where a configured key
    and a direct HTTP call meet, which is exactly what the live gate has to
    refuse. It shares the video-frame path below, so it cannot drift back into
    sending a ``video_url``.
    """

    endpoint = "https://api.voyageai.com/v1/multimodalembeddings"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "voyage-multimodal-3.5",
        dimension: int = 512,
        timeout_seconds: float = 30.0,
        transport_settings: LiveProviderSettings | None = None,
        frame_sampler: BoundedVideoFrameSampler | None = None,
    ):
        if not api_key:
            raise ValueError("Voyage API key is required")
        if dimension not in {256, 512, 1024, 2048}:
            raise ValueError("Voyage Matryoshka dimension must be 256, 512, 1024, or 2048")
        self.api_key = api_key
        self.model = model
        self.dimension = dimension
        self.timeout_seconds = timeout_seconds
        self.live_gate = LiveProviderGate(transport_settings or LiveProviderSettings())
        self.frame_sampler = frame_sampler or BoundedVideoFrameSampler()

    @property
    def provenance(self) -> EmbeddingProvenance:
        return EmbeddingProvenance(
            provider="voyage",
            model=self.model,
            dimension=self.dimension,
            input_type="document",
        )

    def embed(self, content: MultimodalContent, *, input_type: EmbeddingInputType) -> list[float]:
        values, _lineage = self._embed(content, input_type=input_type)
        return values

    def _embed(
        self,
        content: MultimodalContent,
        *,
        input_type: EmbeddingInputType,
    ) -> tuple[list[float], VideoFrameLineage]:
        # A configured key alone is never authority to make a paid HTTP call.
        self.live_gate.assert_live_allowed()
        pieces, lineage = voyage_content_pieces(content, sampler=self.frame_sampler)
        _assert_embeddable(content, pieces, lineage)
        response = httpx.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "inputs": [{"content": pieces}],
                "model": self.model,
                "input_type": input_type,
                "truncation": True,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        vector = _extract_embedding(payload)
        # voyage-multimodal-3.5 is Matryoshka-compatible. The REST endpoint
        # currently returns the default vector, so retain its leading dimensions.
        if len(vector) < self.dimension:
            raise RuntimeError(
                f"Voyage returned {len(vector)} dimensions; configured dimension is {self.dimension}"
            )
        vector = vector[: self.dimension]
        norm = math.sqrt(sum(item * item for item in vector)) or 1.0
        return [item / norm for item in vector], lineage

    def embed_with_provenance(
        self,
        content: MultimodalContent,
        *,
        input_type: EmbeddingInputType,
        project_id: str,
    ) -> EmbeddingVector:
        del project_id
        values, lineage = self._embed(content, input_type=input_type)
        return EmbeddingVector(
            values,
            self.provenance.model_copy(
                update={
                    "input_type": input_type,
                    "evidence_purpose": content.evidence_purpose,
                    "authority_level": content.authority_level,
                    "video_frame_lineage": lineage if content.video_urls else None,
                }
            ),
        )


class ModelRoleEmbeddingProvider:
    """Project-scoped Narrative Memory adapter for ``ModelRoleRuntime``.

    Memory's public service is synchronous today, while provider capabilities are
    asynchronous.  The bridge runs the coroutine directly when no event loop is
    active and otherwise isolates it in a short-lived daemon thread.  Business
    code never receives or calls a Voyage/OpenRouter client directly.
    """

    def __init__(
        self,
        runtime: ModelRoleRuntime,
        *,
        dimension: int = 512,
        frame_sampler: BoundedVideoFrameSampler | None = None,
    ):
        if dimension not in {256, 512, 1024, 2048}:
            raise ValueError("embedding dimension must be 256, 512, 1024, or 2048")
        self.runtime = runtime
        self.dimension = dimension
        self.frame_sampler = frame_sampler or BoundedVideoFrameSampler()

    @property
    def provenance(self) -> EmbeddingProvenance:
        # A project is required to resolve a role binding.  This property exists
        # only for protocol compatibility; the engine uses embed_with_provenance.
        raise MemoryEmbeddingUnavailable("project-scoped embedding provenance is required")

    def embed(self, content: MultimodalContent, *, input_type: EmbeddingInputType) -> list[float]:
        del content, input_type
        raise MemoryEmbeddingUnavailable("project_id is required for ModelRole embeddings")

    def embed_with_provenance(
        self,
        content: MultimodalContent,
        *,
        input_type: EmbeddingInputType,
        project_id: str,
    ) -> EmbeddingVector:
        pieces, lineage = voyage_content_pieces(content, sampler=self.frame_sampler)
        _assert_embeddable(content, pieces, lineage)
        try:
            execution = _run_blocking(
                self.runtime.execute_embeddings(
                    project_id,
                    inputs=[{"content": pieces}],
                    parameters={"dimensions": self.dimension, "input_type": input_type},
                )
            )
            vector = _extract_embedding(execution.response)
        except Exception as exc:
            raise MemoryEmbeddingUnavailable("model-role embedding execution failed") from exc
        if len(vector) < self.dimension:
            raise MemoryEmbeddingUnavailable(
                f"embedding response has {len(vector)} dimensions; expected at least {self.dimension}"
            )
        normalized = vector[: self.dimension]
        norm = math.sqrt(sum(item * item for item in normalized)) or 1.0
        values = [item / norm for item in normalized]
        provenance = EmbeddingProvenance(
            provider=execution.resolved_model.provider,
            model=execution.resolved_model.provider_model_id,
            dimension=len(values),
            input_type=input_type,
            evidence_purpose=content.evidence_purpose,
            authority_level=content.authority_level,
            video_frame_lineage=lineage if content.video_urls else None,
        )
        # The frames are part of what was embedded, so they are part of what
        # the input hash identifies. Their positions and sizes stand in for the
        # bytes: two indexings of the same video agree, and an indexing that
        # got fewer frames is not mistaken for one that got them all.
        input_hash = hashlib.sha256(
            json.dumps(
                {
                    "content": content.model_dump(mode="json"),
                    "video_frames": lineage.model_dump(mode="json"),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        embedding_hash = hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("utf-8")).hexdigest()
        with self.runtime.database.session() as session:
            record = session.get(ModelExecutionRecord, execution.execution_record_id)
            if record is None:
                raise MemoryEmbeddingUnavailable("model execution evidence was not persisted")
            session.add(
                EmbeddingEvidence(
                    project_id=project_id,
                    model_definition_id=execution.resolved_model.definition_id,
                    model_execution_record_id=record.id,
                    input_hash=input_hash,
                    embedding_dimension=len(values),
                    embedding_hash=embedding_hash,
                    latency_ms=record.latency_ms,
                    cost_usd=record.actual_cost_usd,
                )
            )
        return EmbeddingVector(
            values,
            provenance,
        )


def _assert_embeddable(
    content: MultimodalContent,
    pieces: list[dict[str, str]],
    lineage: VideoFrameLineage,
) -> None:
    """Refuse a call that would misrepresent what it embedded.

    A video memory whose frames could not be taken is not a text memory with a
    footnote: nothing of the video reached the vector. Saying so here lets the
    engine record a degradation instead of storing a vector that claims to
    stand for a clip it never saw.
    """

    if content.video_urls and lineage.status is not VideoFrameStatus.EXTRACTED:
        codes = ", ".join(lineage.reason_codes) or "NO_FRAMES"
        raise MemoryEmbeddingUnavailable(
            "video memory requires extracted image frames and none were available "
            f"({codes}); Voyage is never sent a video_url"
        )
    if not pieces:
        raise MemoryEmbeddingUnavailable("embedding content carried nothing embeddable")


def _run_blocking(coroutine):  # type: ignore[no-untyped-def]
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: list[object] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:  # pragma: no cover - re-raised on caller thread
            errors.append(exc)

    thread = Thread(target=run, name="model-role-embedding", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    if not result:  # pragma: no cover - defensive invariant
        raise RuntimeError("embedding execution returned no result")
    return result[0]


def _extract_embedding(payload: dict) -> list[float]:  # type: ignore[type-arg]
    data: Sequence | None = payload.get("data")
    if data and isinstance(data[0], dict):
        value = data[0].get("embedding")
        if isinstance(value, list):
            return [float(item) for item in value]
    embeddings = payload.get("embeddings")
    if isinstance(embeddings, list) and embeddings and isinstance(embeddings[0], list):
        return [float(item) for item in embeddings[0]]
    raise RuntimeError("Voyage response did not contain an embedding")
