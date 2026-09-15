"""Image and video generation on a connection, as a gateway ``GenerationProvider``.

``ConnectionGenerationProvider`` wraps one of the platform's reviewed adapters,
constructed for a single connection with that connection's key and base URL
behind ``ConnectionTransport``. The gateway runs it exactly like a platform
provider - durable boundary, polling, media staging - but it is named
``byok``, carries the connection's media fetch policy (any public host, the
key presented only to the provider's own host), and is trusted at STANDARD:
a workspace's own endpoint is not a platform-vetted production provider.

``MockGenerationProvider`` is the development stand-in: it renders a labelled
placeholder image with Pillow and a short placeholder clip with FFmpeg, and
never touches a network.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from provider_sdk import (
    GenerationProvider,
    ProviderError,
    ProviderHealth,
    ProviderInlineOutput,
    ProviderJob,
    ProviderPollIdentity,
    ProviderReferenceMode,
    ProviderSubmission,
    ProviderTrustLevel,
    RemoteMediaFetchPolicy,
    RetryCategory,
)

from .protocols import USER_CONNECTION_PROVIDER

#: Per-request provider options a canvas node may set. They ride in the job's
#: request metadata and are lifted to the top level only for connection jobs,
#: where the adapter reads them; a platform job never sees them.
CONNECTION_REQUEST_OPTIONS = frozenset({"generate_audio", "seed", "watermark"})


class ConnectionGenerationProvider(GenerationProvider):
    name = USER_CONNECTION_PROVIDER
    reference_mode = ProviderReferenceMode.FETCHABLE_URL
    trust_level = ProviderTrustLevel.STANDARD

    def __init__(
        self,
        inner: GenerationProvider,
        *,
        connection_id: str,
        media_fetch_policy: RemoteMediaFetchPolicy,
    ):
        self._inner = inner
        self.connection_id = connection_id
        self.media_fetch_policy = media_fetch_policy
        self.reference_constraints = inner.reference_constraints
        self.configured = True

    @staticmethod
    def _with_options(request: dict[str, Any]) -> dict[str, Any]:
        metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
        options = (
            metadata.get("connection_options") if isinstance(metadata.get("connection_options"), dict) else {}
        )
        merged = dict(request)
        for key, value in options.items():
            if key in CONNECTION_REQUEST_OPTIONS and value is not None and merged.get(key) is None:
                merged[key] = value
        return merged

    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        return await self._inner.generate_image(
            self._with_options(request), account_id=account_id, worker_id=worker_id
        )

    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        return await self._inner.generate_video(
            self._with_options(request), account_id=account_id, worker_id=worker_id
        )

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        raise ProviderError(
            "connections take references as fetchable URLs, not uploads",
            RetryCategory.INVALID_REQUEST,
            code="CAPABILITY_NOT_SUPPORTED",
        )

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool:
        return False

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
        poll_identity: ProviderPollIdentity | None = None,
    ) -> ProviderJob:
        return await self._inner.get_job(
            provider_job_id,
            account_id=account_id,
            worker_id=worker_id,
            generation_type=generation_type,
        )

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        return await self._inner.cancel_job(provider_job_id, account_id=account_id, worker_id=worker_id)

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        return None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, "CONNECTION", {"connection_id": self.connection_id})


# --- Development mock ---------------------------------------------------------------

#: Seconds a mock video stays RUNNING, so the canvas shows progress in QA.
MOCK_VIDEO_RENDER_SECONDS = 4.0

_ASPECT_SIZES = {
    "16:9": (640, 360),
    "9:16": (360, 640),
    "1:1": (480, 480),
    "4:3": (560, 420),
    "3:4": (420, 560),
    "21:9": (672, 288),
}


def _palette(seed: str) -> tuple[int, int, int]:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return (60 + digest[0] % 150, 60 + digest[1] % 150, 60 + digest[2] % 150)


def mock_image_png(prompt: str, aspect_ratio: str | None, *, label: str = "MOCK IMAGE") -> bytes:
    from PIL import Image, ImageDraw

    width, height = _ASPECT_SIZES.get(str(aspect_ratio or ""), (512, 512))
    base = _palette(prompt or label)
    image = Image.new("RGB", (width, height), base)
    draw = ImageDraw.Draw(image)
    for row in range(height):
        shade = row / max(1, height - 1)
        color = tuple(int(channel * (1 - 0.45 * shade)) for channel in base)
        draw.line([(0, row), (width, row)], fill=color)
    draw.rectangle([12, 12, width - 12, height - 12], outline=(255, 255, 255), width=3)
    draw.text((24, 24), label, fill=(255, 255, 255))
    words = " ".join(str(prompt or "").split())[:180]
    lines = [words[index : index + 42] for index in range(0, len(words), 42)][:5]
    for offset, line in enumerate(lines):
        draw.text((24, 48 + offset * 16), line, fill=(240, 240, 240))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def mock_video_mp4(seed: str, aspect_ratio: str | None, duration_seconds: float = 2.0) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ProviderError(
            "the mock video protocol needs FFmpeg on the worker",
            RetryCategory.PERMANENT_ERROR,
            code="MOCK_FFMPEG_MISSING",
            submitted=True,
        )
    width, height = _ASPECT_SIZES.get(str(aspect_ratio or ""), (640, 360))
    red, green, blue = _palette(seed)
    color = f"0x{red:02x}{green:02x}{blue:02x}"
    seconds = max(1.0, min(float(duration_seconds or 2.0), 4.0))
    with tempfile.TemporaryDirectory(prefix="mock-video-") as directory:
        target = Path(directory) / "mock.mp4"
        filters = (
            f"drawbox=x='mod(t*{width // 2},{width})':y={height // 3}:w={width // 6}:h={height // 3}"
            ":color=white@0.8:t=fill"
        )
        last_error = ""
        for codec in (["-c:v", "libx264", "-pix_fmt", "yuv420p"], ["-c:v", "mpeg4", "-q:v", "5"]):
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s={width}x{height}:d={seconds}:r=24",
                "-vf",
                filters,
                *codec,
                "-movflags",
                "+faststart",
                str(target),
            ]
            completed = subprocess.run(command, capture_output=True, timeout=60, check=False)
            if completed.returncode == 0 and target.exists() and target.stat().st_size > 0:
                return target.read_bytes()
            last_error = completed.stderr.decode("utf-8", "replace")[-300:]
        raise ProviderError(
            f"the mock video could not be rendered: {last_error}",
            RetryCategory.PERMANENT_ERROR,
            code="MOCK_RENDER_FAILED",
            submitted=True,
        )


class MockGenerationProvider(GenerationProvider):
    name = USER_CONNECTION_PROVIDER
    reference_mode = ProviderReferenceMode.FETCHABLE_URL
    trust_level = ProviderTrustLevel.STANDARD

    def __init__(self, *, connection_id: str):
        self.connection_id = connection_id
        self.configured = True
        # Placeholders are inline bytes; there is never a URL to fetch.
        self.media_fetch_policy = RemoteMediaFetchPolicy(allowed_host_patterns=())

    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        prompt = str(request.get("prompt") or "")
        references = list(request.get("reference_urls") or [])
        label = "MOCK IMAGE" + (f" · {len(references)} REF" if references else "")
        content = await asyncio.to_thread(mock_image_png, prompt, request.get("aspect_ratio"), label=label)
        job_id = f"mock-image-{uuid.uuid4().hex}"
        return ProviderSubmission(
            job_id,
            {"mock": True},
            result=ProviderJob(
                job_id,
                "COMPLETED",
                progress=1.0,
                output_mime_type="image/png",
                outputs=[ProviderInlineOutput(content, "image/png")],
                raw={"mock": True},
            ),
        )

    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        aspect = str(request.get("aspect_ratio") or "16:9").replace(":", "x")
        duration = int(float(request.get("duration") or 2))
        job_id = f"mock-video-{int(time.time() * 1000)}-{aspect}-{duration}-{uuid.uuid4().hex[:12]}"
        return ProviderSubmission(job_id, {"mock": True})

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        raise ProviderError("mock connections take no uploads", RetryCategory.INVALID_REQUEST)

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool:
        return False

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
        poll_identity: ProviderPollIdentity | None = None,
    ) -> ProviderJob:
        parts = provider_job_id.split("-")
        if generation_type != "video" or len(parts) < 6 or parts[1] != "video":
            raise ProviderError(
                "mock image results are returned at submission",
                RetryCategory.PERMANENT_ERROR,
                code="MOCK_RESULT_NOT_RETRIEVABLE",
                submitted=True,
            )
        started = int(parts[2]) / 1000
        elapsed = time.time() - started
        if elapsed < MOCK_VIDEO_RENDER_SECONDS:
            return ProviderJob(
                provider_job_id,
                "RUNNING",
                progress=round(max(0.05, elapsed / MOCK_VIDEO_RENDER_SECONDS), 2),
                raw={"mock": True},
            )
        aspect = parts[3].replace("x", ":")
        content = await asyncio.to_thread(mock_video_mp4, provider_job_id, aspect, float(parts[4]))
        return ProviderJob(
            provider_job_id,
            "COMPLETED",
            progress=1.0,
            output_mime_type="video/mp4",
            outputs=[ProviderInlineOutput(content, "video/mp4")],
            raw={"mock": True},
        )

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        return True

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        return None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, "MOCK", {"connection_id": self.connection_id})


__all__ = [
    "CONNECTION_REQUEST_OPTIONS",
    "ConnectionGenerationProvider",
    "MockGenerationProvider",
    "mock_image_png",
    "mock_video_mp4",
]
