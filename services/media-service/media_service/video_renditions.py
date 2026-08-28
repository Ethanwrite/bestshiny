"""Probing and adapting video for one provider's declared reference bounds.

The image rendition path re-encodes with Pillow in-process. Video cannot be
adapted that way, so this module shells out to ffprobe to establish what a
file actually is, and to ffmpeg to derive a bounded copy when the original
does not fit. The same two rules as the image path apply:

1. The original is never touched; a derived copy is written beside it.
2. Nothing is guessed. A file that cannot be probed, a bound that cannot be
   met, and a gap that only a semantic edit (trim, crop) could close all fail
   with the specific unmet constraint instead of shipping something else.

Every derived copy is re-probed after transcoding and accepted only when it
passes the same constraint check the original failed — a provider is never
handed a rendition whose facts were assumed rather than observed.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from provider_sdk import VideoConstraintViolation, VideoReferenceConstraints

# Part of the rendition cache key. Bump whenever the ffmpeg invocation changes
# in a way that alters output bytes for the same input and constraints, so a
# cached copy from the old invocation is not mistaken for the new one.
VIDEO_TRANSCODER_VERSION = "video-reference-transcoder-v1"

# Muxing rounds duration to the container timebase, so a transcoded copy of an
# in-bounds source can read a few hundredths of a second longer than the
# source did. Revalidation allows exactly that much; the source gets none.
_REVALIDATION_DURATION_SLACK_SECONDS = 0.1

# Below this a target bitrate cannot carry a legible moving image at any
# resolution worth calling a reference; the byte or bitrate bound is unmeetable.
_MINIMUM_TARGET_BITRATE_BPS = 100_000

# container mime -> (ffmpeg muxer, file extension)
_CONTAINERS: dict[str, tuple[str, str]] = {
    "video/mp4": ("mp4", "mp4"),
    "video/quicktime": ("mov", "mov"),
    "video/webm": ("webm", "webm"),
}

# codec_name (as ffprobe reports it) -> ffmpeg encoder
_ENCODERS: dict[str, str] = {
    "h264": "libx264",
    "hevc": "libx265",
    "vp9": "libvpx-vp9",
}

# Which codecs each container can legally carry, for remux and encode planning.
_CONTAINER_CODECS: dict[str, frozenset[str]] = {
    "video/mp4": frozenset({"h264", "hevc", "av1"}),
    "video/quicktime": frozenset({"h264", "hevc"}),
    "video/webm": frozenset({"vp8", "vp9", "av1"}),
}

# container mime -> (audio encoder, audio bitrate flag value, audio bits/sec)
_AUDIO: dict[str, tuple[str, str, int]] = {
    "video/mp4": ("aac", "128k", 128_000),
    "video/quicktime": ("aac", "128k", 128_000),
    "video/webm": ("libopus", "96k", 96_000),
}


class VideoAdaptationFailed(RuntimeError):
    """The video cannot be validated or brought inside the declared bounds.

    ``violations`` carries the machine-readable codes of the constraints that
    remain unmet, so the refusal names exactly what the provider would have
    rejected instead of a generic "video failed".
    """

    def __init__(self, message: str, *, violations: tuple[str, ...] = ()):
        super().__init__(message)
        self.violations = violations


@dataclass(frozen=True)
class VideoStreamFacts:
    """What ffprobe observed about one video file. Facts, never declarations."""

    codec: str
    width: int
    height: int
    frame_rate: float
    duration_seconds: float
    bit_rate_bps: int
    size_bytes: int
    has_audio: bool


@dataclass(frozen=True)
class VideoTranscodeResult:
    """One derived, already revalidated video rendition on local disk."""

    path: Path
    mime_type: str
    facts: VideoStreamFacts
    attempts: int
    remuxed: bool


def _parse_rate(value: object) -> float:
    numerator, separator, denominator = str(value or "").partition("/")
    try:
        rate = float(numerator) / float(denominator) if separator else float(numerator)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    return rate if math.isfinite(rate) and rate > 0 else 0.0


def _stderr_excerpt(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").strip()[-400:]


class VideoReferenceTranscoder:
    """Probes originals and derives constraint-bounded copies with ffmpeg."""

    version = VIDEO_TRANSCODER_VERSION

    def __init__(
        self,
        *,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
        probe_timeout_seconds: float = 30.0,
        transcode_timeout_seconds: float = 600.0,
    ):
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.probe_timeout_seconds = probe_timeout_seconds
        self.transcode_timeout_seconds = transcode_timeout_seconds

    # -- probing ---------------------------------------------------------------

    def probe(self, path: Path) -> VideoStreamFacts:
        """Establish what the file at ``path`` actually contains, or refuse."""

        if shutil.which(self.ffprobe_path) is None:
            raise VideoAdaptationFailed(
                f"video reference validation is unavailable: {self.ffprobe_path} is not installed"
            )
        try:
            completed = subprocess.run(
                [
                    self.ffprobe_path,
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_format",
                    "-show_streams",
                    str(path),
                ],
                capture_output=True,
                check=False,
                timeout=self.probe_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VideoAdaptationFailed(f"video could not be probed: {exc}") from exc
        if completed.returncode:
            raise VideoAdaptationFailed(
                f"video could not be probed: {_stderr_excerpt(completed.stderr)}"
            )
        try:
            payload = json.loads(completed.stdout or b"{}") or {}
        except json.JSONDecodeError as exc:
            raise VideoAdaptationFailed("ffprobe returned unparseable output") from exc

        streams = [entry for entry in payload.get("streams") or [] if isinstance(entry, dict)]
        video = next(
            (
                entry
                for entry in streams
                if entry.get("codec_type") == "video"
                # Cover art is a still image riding in a video stream slot.
                and not (entry.get("disposition") or {}).get("attached_pic")
            ),
            None,
        )
        if video is None:
            raise VideoAdaptationFailed("file contains no video stream")

        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        codec = str(video.get("codec_name") or "").lower()
        if width <= 0 or height <= 0 or not codec:
            raise VideoAdaptationFailed("video stream has no observable codec or dimensions")

        frame_rate = _parse_rate(video.get("avg_frame_rate"))
        if frame_rate == 0.0:
            frame_rate = _parse_rate(video.get("r_frame_rate"))

        media_format = payload.get("format") or {}
        try:
            duration = float(media_format.get("duration"))
        except (TypeError, ValueError):
            duration = 0.0
        if not math.isfinite(duration) or duration <= 0:
            raise VideoAdaptationFailed("video has no observable duration")

        size_bytes = int(path.stat().st_size)
        try:
            bit_rate = int(media_format.get("bit_rate"))
        except (TypeError, ValueError):
            # WebM frequently omits format bit_rate; the observable rate is
            # the file's own bytes over its own duration.
            bit_rate = int(size_bytes * 8 / duration)

        return VideoStreamFacts(
            codec=codec,
            width=width,
            height=height,
            frame_rate=frame_rate,
            duration_seconds=duration,
            bit_rate_bps=bit_rate,
            size_bytes=size_bytes,
            has_audio=any(entry.get("codec_type") == "audio" for entry in streams),
        )

    @staticmethod
    def violations(
        facts: VideoStreamFacts,
        *,
        container_mime_type: str,
        constraints: VideoReferenceConstraints,
        duration_slack_seconds: float = 0.0,
    ) -> tuple[VideoConstraintViolation, ...]:
        return constraints.violations(
            container_mime_type=container_mime_type,
            codec=facts.codec,
            width=facts.width,
            height=facts.height,
            frame_rate=facts.frame_rate,
            duration_seconds=facts.duration_seconds,
            bit_rate_bps=facts.bit_rate_bps,
            size_bytes=facts.size_bytes,
            duration_slack_seconds=duration_slack_seconds,
        )

    # -- derivation ------------------------------------------------------------

    def derive(
        self,
        source_path: Path,
        source_facts: VideoStreamFacts,
        *,
        source_mime_type: str,
        constraints: VideoReferenceConstraints,
        violations: tuple[VideoConstraintViolation, ...],
        workdir: Path,
    ) -> VideoTranscodeResult:
        """Bring the source inside the bounds it failed, and prove that it is.

        The caller has already refused unadaptable violations; everything left
        here can be closed by re-encoding. Every attempt's output is re-probed
        and checked against the full constraint set — only a copy that passes
        is returned.
        """

        if shutil.which(self.ffmpeg_path) is None:
            raise VideoAdaptationFailed(
                f"video reference adaptation is unavailable: {self.ffmpeg_path} is not installed"
            )

        target_mime = self._target_container(source_mime_type, constraints)
        target_codec = self._target_codec(source_facts.codec, target_mime, constraints)
        _, extension = _CONTAINERS[target_mime]
        codes = frozenset(violation.code for violation in violations)

        attempts = 0
        # A container-only gap needs no re-encode: the streams are already
        # acceptable, so rewrapping them loses nothing and costs almost nothing.
        if codes == {"VIDEO_CONTAINER_NOT_ACCEPTED"} and source_facts.codec in (
            _CONTAINER_CODECS.get(target_mime) or frozenset()
        ):
            attempts += 1
            out_path = workdir / f"remux-{attempts}.{extension}"
            if self._run_ffmpeg(self._remux_command(source_path, out_path, target_mime), out_path):
                # A remux that fails revalidation falls through to a real re-encode.
                result, _, _ = self._revalidate(
                    out_path, target_mime, constraints, attempts=attempts, remuxed=True
                )
                if result is not None:
                    return result

        target_bitrate = self._target_bitrate(source_facts, constraints)
        width, height = self._target_dimensions(source_facts, constraints)
        exhausted_codes: tuple[str, ...] = ("VIDEO_BYTES_EXCEED_LIMIT",)
        for _ in range(3):
            attempts += 1
            out_path = workdir / f"transcode-{attempts}.{extension}"
            command = self._transcode_command(
                source_path,
                out_path,
                source_facts,
                target_mime=target_mime,
                target_codec=target_codec,
                width=width,
                height=height,
                frame_rate=constraints.max_frame_rate,
                bitrate_bps=target_bitrate,
            )
            if not self._run_ffmpeg(command, out_path):
                raise VideoAdaptationFailed(
                    f"ffmpeg could not transcode the reference video to {target_mime}"
                )
            result, observed, remaining = self._revalidate(
                out_path, target_mime, constraints, attempts=attempts, remuxed=False
            )
            if result is not None:
                return result

            # Revalidation failed. The only bounds an accurately planned encode
            # can still overshoot are the byte and bitrate caps (rate control
            # tracks a target imperfectly), so tighten the rate toward the
            # observed overshoot and try again; anything else is not retryable.
            remaining_codes = tuple(violation.code for violation in remaining)
            if set(remaining_codes) - {"VIDEO_BYTES_EXCEED_LIMIT", "VIDEO_BITRATE_EXCEEDS_LIMIT"}:
                raise VideoAdaptationFailed(
                    "transcoded video still fails constraints that re-encoding "
                    f"should have met: {'; '.join(str(v) for v in remaining)}",
                    violations=remaining_codes,
                )
            exhausted_codes = remaining_codes
            overshoot = 1.0
            if constraints.max_bytes is not None and observed.size_bytes > constraints.max_bytes:
                overshoot = observed.size_bytes / constraints.max_bytes
            elif constraints.max_bitrate_bps is not None:
                overshoot = observed.bit_rate_bps / constraints.max_bitrate_bps
            current = target_bitrate if target_bitrate is not None else observed.bit_rate_bps
            target_bitrate = int(current / max(overshoot, 1.0) * 0.85)
            if target_bitrate < _MINIMUM_TARGET_BITRATE_BPS:
                break

        raise VideoAdaptationFailed(
            "no encoding at a usable bitrate fits the consumer's byte and bitrate "
            f"limits for this {source_facts.duration_seconds:g}s video",
            violations=exhausted_codes,
        )

    # -- planning --------------------------------------------------------------

    @staticmethod
    def _target_container(source_mime_type: str, constraints: VideoReferenceConstraints) -> str:
        target = (
            source_mime_type.lower()
            if source_mime_type.lower() in constraints.accepted_containers
            else constraints.preferred_container
        )
        if target not in _CONTAINERS:
            raise VideoAdaptationFailed(f"cannot mux a reference video as {target}")
        return target

    @staticmethod
    def _target_codec(
        source_codec: str, target_mime: str, constraints: VideoReferenceConstraints
    ) -> str:
        compatible = _CONTAINER_CODECS.get(target_mime) or frozenset()
        # Keeping the source codec is only worth anything if this host can
        # actually encode it; an accepted codec with no encoder falls back to
        # the preferred one rather than failing a gap it could have closed.
        target = (
            source_codec
            if source_codec in constraints.accepted_codecs
            and source_codec in compatible
            and source_codec in _ENCODERS
            else constraints.preferred_codec
        )
        if target not in _ENCODERS:
            raise VideoAdaptationFailed(f"cannot encode a reference video as {target}")
        if target not in compatible:
            raise VideoAdaptationFailed(
                f"the declared preferred codec {target} cannot be carried by the "
                f"declared container {target_mime}"
            )
        return target

    @staticmethod
    def _target_dimensions(
        facts: VideoStreamFacts, constraints: VideoReferenceConstraints
    ) -> tuple[int, int]:
        """Fit inside the declared box by uniform scaling only — never cropping.

        A declared frame-size cap is honoured however small it is, exactly as
        the image path honours a declared ``max_pixels``: the bound is the
        provider's own statement about itself, not a quality choice this
        resolver is making. Resolution is never shrunk to chase the byte cap —
        bitrate carries that — so no identity-floor guard belongs here.
        """

        scale = min(
            constraints.max_width / facts.width if constraints.max_width else 1.0,
            constraints.max_height / facts.height if constraints.max_height else 1.0,
            1.0,
        )
        # Encoders producing yuv420p need even dimensions; rounding down via
        # resampling preserves the frame's content, unlike cropping a row off.
        width = max(2, int(facts.width * scale) // 2 * 2)
        height = max(2, int(facts.height * scale) // 2 * 2)
        return width, height

    @staticmethod
    def _target_bitrate(
        facts: VideoStreamFacts, constraints: VideoReferenceConstraints
    ) -> int | None:
        """The video bitrate to encode at, or None to let quality-mode decide.

        A byte cap is a bitrate cap in disguise: the file's own duration is
        fixed (trimming is refused), so bytes/duration is the whole budget,
        and audio and container framing spend part of it before the picture
        gets any.
        """

        candidates: list[float] = []
        if constraints.max_bitrate_bps is not None:
            candidates.append(constraints.max_bitrate_bps * 0.92)
        if constraints.max_bytes is not None:
            budget = constraints.max_bytes * 8 / facts.duration_seconds * 0.90
            if facts.has_audio:
                budget -= _AUDIO[
                    constraints.preferred_container
                    if constraints.preferred_container in _AUDIO
                    else "video/mp4"
                ][2]
            candidates.append(budget)
        if not candidates:
            return None
        target = int(min(candidates))
        if target < _MINIMUM_TARGET_BITRATE_BPS:
            raise VideoAdaptationFailed(
                "the consumer's byte and bitrate limits leave no usable video "
                f"bitrate for this {facts.duration_seconds:g}s video",
                violations=(
                    "VIDEO_BYTES_EXCEED_LIMIT"
                    if constraints.max_bytes is not None
                    else "VIDEO_BITRATE_EXCEEDS_LIMIT",
                ),
            )
        return target

    # -- command assembly and execution ---------------------------------------

    @staticmethod
    def _remux_command(source: Path, out_path: Path, target_mime: str) -> list[str]:
        muxer, _ = _CONTAINERS[target_mime]
        command = ["-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"]
        if muxer in {"mp4", "mov"}:
            command += ["-movflags", "+faststart"]
        return command + ["-f", muxer, str(out_path)]

    def _transcode_command(
        self,
        source: Path,
        out_path: Path,
        facts: VideoStreamFacts,
        *,
        target_mime: str,
        target_codec: str,
        width: int,
        height: int,
        frame_rate: float | None,
        bitrate_bps: int | None,
    ) -> list[str]:
        muxer, _ = _CONTAINERS[target_mime]
        encoder = _ENCODERS[target_codec]
        filters: list[str] = []
        if frame_rate is not None and facts.frame_rate > frame_rate:
            filters.append(f"fps={frame_rate:g}")
        if (width, height) != (facts.width, facts.height):
            filters.append(f"scale={width}:{height}:flags=lanczos")

        command = ["-i", str(source), "-map", "0:v:0", "-c:v", encoder, "-pix_fmt", "yuv420p"]
        if filters:
            command += ["-vf", ",".join(filters)]
        if encoder in {"libx264", "libx265"}:
            command += ["-preset", "veryfast"]
        if bitrate_bps is not None:
            command += [
                "-b:v",
                str(bitrate_bps),
                "-maxrate",
                str(bitrate_bps),
                "-bufsize",
                str(bitrate_bps * 2),
            ]
        elif encoder == "libvpx-vp9":
            command += ["-crf", "32", "-b:v", "0"]
        else:
            command += ["-crf", "23"]
        if facts.has_audio:
            audio_codec, audio_bitrate, _ = _AUDIO[target_mime]
            command += ["-map", "0:a:0", "-c:a", audio_codec, "-b:a", audio_bitrate]
        else:
            command += ["-an"]
        if muxer in {"mp4", "mov"}:
            command += ["-movflags", "+faststart"]
        return command + ["-f", muxer, str(out_path)]

    def _run_ffmpeg(self, arguments: list[str], out_path: Path) -> bool:
        try:
            completed = subprocess.run(
                [self.ffmpeg_path, "-nostdin", "-v", "error", "-y", *arguments],
                capture_output=True,
                check=False,
                timeout=self.transcode_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise VideoAdaptationFailed(
                f"video transcode exceeded {self.transcode_timeout_seconds:g}s and was stopped"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise VideoAdaptationFailed(f"video transcode could not run: {exc}") from exc
        return completed.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0

    def _revalidate(
        self,
        out_path: Path,
        target_mime: str,
        constraints: VideoReferenceConstraints,
        *,
        attempts: int,
        remuxed: bool,
    ) -> tuple[
        VideoTranscodeResult | None,
        VideoStreamFacts,
        tuple[VideoConstraintViolation, ...],
    ]:
        """Accept the output only if it observably passes the full constraint set."""

        facts = self.probe(out_path)
        remaining = self.violations(
            facts,
            container_mime_type=target_mime,
            constraints=constraints,
            duration_slack_seconds=_REVALIDATION_DURATION_SLACK_SECONDS,
        )
        if remaining:
            return None, facts, remaining
        result = VideoTranscodeResult(
            path=out_path,
            mime_type=target_mime,
            facts=facts,
            attempts=attempts,
            remuxed=remuxed,
        )
        return result, facts, ()


__all__ = [
    "VIDEO_TRANSCODER_VERSION",
    "VideoAdaptationFailed",
    "VideoReferenceTranscoder",
    "VideoStreamFacts",
    "VideoTranscodeResult",
]
