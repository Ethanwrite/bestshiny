from __future__ import annotations

import io
import json
import math
import subprocess
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from statistics import mean, median
from typing import Any, Protocol

from PIL import Image

TRACKED = "TRACKED"
TRACKING_UNCERTAIN = "TRACKING_UNCERTAIN"
VLM_REVIEW_REQUIRED = "VLM_REVIEW_REQUIRED"
UNAVAILABLE = "UNAVAILABLE"
PASS = "PASS"
FAIL = "FAIL"
ABSTAIN = "ABSTAIN"
SHADOW = "SHADOW"

FRONT = "FRONT"
THREE_QUARTER_LEFT = "THREE_QUARTER_LEFT"
THREE_QUARTER_RIGHT = "THREE_QUARTER_RIGHT"
LEFT_PROFILE = "LEFT_PROFILE"
RIGHT_PROFILE = "RIGHT_PROFILE"
ANY_VIEW = "ANY"

_VIEW_YAWS = {
    LEFT_PROFILE: -90.0,
    THREE_QUARTER_LEFT: -35.0,
    FRONT: 0.0,
    THREE_QUARTER_RIGHT: 35.0,
    RIGHT_PROFILE: 90.0,
}
_VIEW_ALIASES = {
    "front": FRONT,
    "three-quarter-left": THREE_QUARTER_LEFT,
    "three_quarter_left": THREE_QUARTER_LEFT,
    "three-quarter-right": THREE_QUARTER_RIGHT,
    "three_quarter_right": THREE_QUARTER_RIGHT,
    "left-profile": LEFT_PROFILE,
    "left_profile": LEFT_PROFILE,
    "right-profile": RIGHT_PROFILE,
    "right_profile": RIGHT_PROFILE,
}


def _probability(value: float, name: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not 0 <= parsed <= 1:
        raise ValueError(f"{name} must be finite and between zero and one")
    return parsed


def _finite(value: float, name: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


@dataclass(frozen=True)
class BoundingBox:
    """Normalized image-space bounds."""

    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        values = tuple(_probability(value, "bounding box coordinate") for value in asdict(self).values())
        if values[0] >= values[2] or values[1] >= values[3]:
            raise ValueError("bounding box must have positive width and height")


@dataclass(frozen=True)
class SampledFrame:
    frame_index: int
    normalized_position: float
    sample_time: float
    image_png: bytes
    width: int
    height: int


class FrameSampler(Protocol):
    version: str

    def sample(self, video_path: Path, positions: tuple[float, ...]) -> tuple[SampledFrame, ...]: ...


class FFmpegFrameSampler:
    """CPU-capable local frame sampler with no network or model dependency."""

    version = "ffmpeg-frame-sampler-v1"

    def __init__(self, *, command_timeout_seconds: float = 30.0):
        if command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be positive")
        self.command_timeout_seconds = command_timeout_seconds

    def _metadata(self, video_path: Path) -> tuple[float, float]:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=avg_frame_rate",
                "-of",
                "json",
                str(video_path),
            ],
            capture_output=True,
            check=False,
            timeout=self.command_timeout_seconds,
        )
        if result.returncode:
            raise RuntimeError("fixture video could not be probed")
        try:
            payload = json.loads(result.stdout or b"{}") or {}
            duration = float(payload.get("format", {}).get("duration"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("fixture video has no valid duration") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise RuntimeError("fixture video has no positive duration")
        frame_interval = min(0.1, duration * 0.05)
        streams = payload.get("streams") or []
        if streams and isinstance(streams[0], dict):
            numerator, separator, denominator = str(streams[0].get("avg_frame_rate") or "").partition("/")
            try:
                frame_rate = float(numerator) / float(denominator) if separator else float(numerator)
            except (TypeError, ValueError, ZeroDivisionError):
                frame_rate = 0.0
            if math.isfinite(frame_rate) and frame_rate > 0:
                frame_interval = 1.0 / frame_rate
        return duration, frame_interval

    def sample(self, video_path: Path, positions: tuple[float, ...]) -> tuple[SampledFrame, ...]:
        path = Path(video_path)
        if not path.is_file():
            raise FileNotFoundError(f"video evidence file not found: {path}")
        if not positions:
            raise ValueError("at least one frame position is required")
        normalized = tuple(_probability(position, "sample position") for position in positions)
        duration, frame_interval = self._metadata(path)
        frames: list[SampledFrame] = []
        for frame_index, position in enumerate(normalized):
            sample_time = min(
                position * duration,
                max(0.0, duration - frame_interval - 0.001),
            )
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    f"{sample_time:.6f}",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "pipe:1",
                ],
                capture_output=True,
                check=False,
                timeout=self.command_timeout_seconds,
            )
            if result.returncode or not result.stdout:
                raise RuntimeError(f"video frame extraction failed at {sample_time:.6f}s")
            with Image.open(io.BytesIO(result.stdout)) as image:
                width, height = image.size
                image.verify()
            frames.append(
                SampledFrame(
                    frame_index=frame_index,
                    normalized_position=position,
                    sample_time=round(sample_time, 6),
                    image_png=result.stdout,
                    width=width,
                    height=height,
                )
            )
        return tuple(frames)


@dataclass(frozen=True)
class VisualDetection:
    detection_id: str
    person_box: BoundingBox
    face_box: BoundingBox | None
    detection_confidence: float
    face_visibility: float
    pose_yaw: float
    blur_score: float


@dataclass(frozen=True)
class FrameDetections:
    frame: SampledFrame
    detections: tuple[VisualDetection, ...]


class PersonFaceDetector(Protocol):
    version: str

    def detect(self, frame: SampledFrame) -> tuple[VisualDetection, ...]: ...


@dataclass(frozen=True)
class TrackAssignment:
    frame_index: int
    detection_id: str
    track_id: str
    track_confidence: float


@dataclass(frozen=True)
class TrackingResult:
    status: str
    assignments: tuple[TrackAssignment, ...]
    reason_codes: tuple[str, ...] = ()


class CharacterTracker(Protocol):
    version: str

    def associate(
        self,
        detections: tuple[FrameDetections, ...],
        *,
        character_id: str,
    ) -> TrackingResult: ...


class FaceIdentityEncoder(Protocol):
    version: str

    def encode(self, face_image: Image.Image) -> Sequence[float]: ...


class AppearanceEncoder(Protocol):
    version: str

    def encode(self, person_image: Image.Image) -> Sequence[float]: ...


@dataclass(frozen=True)
class CanonicalIdentityReference:
    reference_asset_id: str
    view: str
    image_bytes: bytes
    face_box: BoundingBox | None = None
    appearance_box: BoundingBox | None = None
    reference_confidence: float = 1.0
    # MediaAsset rows are immutable and content addressed, so callers that do
    # not have a logical AssetVersion may use the media SHA-256 as this value.
    reference_asset_version: str = "UNVERSIONED"
    # Production inference consumes short-lived HTTPS object-storage URLs.
    # Bytes remain on this contract for the deterministic local fixture stack.
    source_url: str | None = None


@dataclass(frozen=True)
class CharacterEvidence:
    candidate_id: str
    character_id: str
    sample_time: float
    face_similarity: float | None
    appearance_similarity: float | None
    face_visibility: float
    detection_confidence: float
    track_confidence: float
    pose_yaw: float
    blur_score: float
    reference_asset_id: str
    reference_view: str
    evidence_quality: float
    track_id: str
    face_encoder_version: str
    appearance_encoder_version: str
    hair_similarity: str = UNAVAILABLE
    costume_similarity: str = UNAVAILABLE
    detector_model_name: str = "UNSPECIFIED"
    detector_model_version: str = "UNSPECIFIED"
    tracker_name: str = "UNSPECIFIED"
    tracker_version: str = "UNSPECIFIED"
    face_detector_model: str = "UNSPECIFIED"
    face_detector_version: str = "UNSPECIFIED"
    face_identity_model: str = "UNSPECIFIED"
    face_identity_version: str = "UNSPECIFIED"
    appearance_model: str = "UNSPECIFIED"
    appearance_model_version: str = "UNSPECIFIED"
    threshold_version: str = "UNSPECIFIED"
    reference_asset_version: str = "UNVERSIONED"
    pipeline_version: str = "UNSPECIFIED"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QAThresholdProfile:
    profile_id: str
    version: str
    shot_type: str
    face_view: str
    visibility_range: tuple[float, float]
    identity_pass: float
    identity_hard_fail: float
    drift_limit: float
    minimum_required_samples: int
    minimum_evidence_quality: float = 0.15

    def __post_init__(self) -> None:
        low, high = self.visibility_range
        _probability(low, "visibility range minimum")
        _probability(high, "visibility range maximum")
        if low > high:
            raise ValueError("visibility range minimum cannot exceed maximum")
        _probability(self.identity_pass, "identity pass threshold")
        _probability(self.identity_hard_fail, "identity hard-fail threshold")
        _probability(self.minimum_evidence_quality, "minimum evidence quality")
        if self.identity_hard_fail > self.identity_pass:
            raise ValueError("identity hard-fail threshold cannot exceed pass threshold")
        if not math.isfinite(self.drift_limit) or self.drift_limit <= 0:
            raise ValueError("drift_limit must be positive")
        if self.minimum_required_samples <= 0:
            raise ValueError("minimum_required_samples must be positive")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["visibility_range"] = list(self.visibility_range)
        return value


class QAThresholdRegistry:
    version = "qa-threshold-registry-v1"

    def __init__(self, profiles: Sequence[QAThresholdProfile] | None = None):
        self.profiles = tuple(profiles or self.default_profiles())
        if not self.profiles:
            raise ValueError("at least one QA threshold profile is required")
        identities = [profile.profile_id for profile in self.profiles]
        if len(identities) != len(set(identities)):
            raise ValueError("QA threshold profile IDs must be unique")

    @staticmethod
    def default_profiles() -> tuple[QAThresholdProfile, ...]:
        def profile(
            profile_id: str,
            shot_type: str,
            face_view: str,
            identity_pass: float,
            identity_hard_fail: float,
        ) -> QAThresholdProfile:
            return QAThresholdProfile(
                profile_id=profile_id,
                version="character-identity-thresholds-2026-08-21-v1",
                shot_type=shot_type,
                face_view=face_view,
                visibility_range=(0.0, 1.0),
                identity_pass=identity_pass,
                identity_hard_fail=identity_hard_fail,
                drift_limit=0.055,
                minimum_required_samples=4,
            )

        return (
            profile("dialogue-front-v1", "DIALOGUE", FRONT, 0.78, 0.55),
            profile("close-up-front-v1", "CLOSE_UP_CHARACTER", FRONT, 0.82, 0.58),
            profile("profile-view-v1", "ANY", LEFT_PROFILE, 0.70, 0.45),
            profile("right-profile-view-v1", "ANY", RIGHT_PROFILE, 0.70, 0.45),
            profile("character-default-v1", "ANY", ANY_VIEW, 0.75, 0.50),
        )

    def resolve(self, *, shot_type: str, face_view: str, face_visibility: float) -> QAThresholdProfile:
        visibility = _probability(face_visibility, "face visibility")
        normalized_shot = shot_type.strip().upper() or "DIALOGUE"
        normalized_view = normalize_reference_view(face_view)
        matches = [
            profile
            for profile in self.profiles
            if profile.shot_type in {"ANY", normalized_shot}
            and profile.face_view in {ANY_VIEW, normalized_view}
            and profile.visibility_range[0] <= visibility <= profile.visibility_range[1]
        ]
        if not matches:
            raise LookupError("no QA threshold profile matches the evidence")
        return max(
            matches,
            key=lambda profile: (
                profile.shot_type == normalized_shot,
                profile.face_view == normalized_view,
                -(profile.visibility_range[1] - profile.visibility_range[0]),
                profile.version,
                profile.profile_id,
            ),
        )


@dataclass(frozen=True)
class CharacterEvidenceAggregate:
    average_identity: float | None
    minimum_identity: float | None
    identity_p10: float | None
    drift_slope: float | None
    low_score_duration: float
    appearance_similarity: float | None
    hair_similarity: str
    costume_similarity: str
    reacquisition_score: float | None
    usable_samples: int
    total_samples: int
    dominant_face_view: str
    average_face_visibility: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CharacterEvidenceReport:
    producer_run_id: str
    producer_version: str
    candidate_id: str
    character_id: str
    tracking_status: str
    tracking_reason_codes: tuple[str, ...]
    review_requirements: tuple[str, ...]
    samples: tuple[CharacterEvidence, ...]
    aggregate: CharacterEvidenceAggregate
    threshold_profile: QAThresholdProfile
    pipeline_versions: dict[str, str]
    decision: str = ABSTAIN
    operating_mode: str = SHADOW
    model_manifest_version: str = "UNSPECIFIED"
    model_provenance: dict[str, dict[str, Any]] | None = None

    @property
    def semantic_review_required(self) -> bool:
        return VLM_REVIEW_REQUIRED in self.review_requirements

    def identity_samples(self) -> list[dict[str, Any]]:
        return [sample.to_dict() for sample in self.samples]

    def to_dict(self) -> dict[str, Any]:
        return {
            "producer_run_id": self.producer_run_id,
            "producer_version": self.producer_version,
            "candidate_id": self.candidate_id,
            "character_id": self.character_id,
            "tracking_status": self.tracking_status,
            "tracking_reason_codes": list(self.tracking_reason_codes),
            "review_requirements": list(self.review_requirements),
            "samples": self.identity_samples(),
            "aggregate": self.aggregate.to_dict(),
            "threshold_profile": self.threshold_profile.to_dict(),
            "pipeline_versions": dict(self.pipeline_versions),
            "decision": self.decision,
            "operating_mode": self.operating_mode,
            "model_manifest_version": self.model_manifest_version,
            "model_provenance": dict(self.model_provenance or {}),
        }


@dataclass(frozen=True)
class CharacterEvidenceSubmission:
    """Accepted asynchronous work; acceptance is never identity evidence."""

    job_id: str
    candidate_id: str
    status: str
    submitted_at: str


@dataclass(frozen=True)
class CharacterSubmissionTarget:
    """One character in a shadow analysis, and what it is compared against.

    A candidate can bind several characters; one remote job still analyses them
    together, so the request carries a list of these rather than a single id.
    """

    character_id: str
    references: tuple[CanonicalIdentityReference, ...]


class AsyncCharacterEvidenceProducer(Protocol):
    version: str

    def submit(
        self,
        video_path: Path,
        *,
        candidate_id: str,
        character_id: str | None = None,
        references: Sequence[CanonicalIdentityReference] = (),
        characters: Sequence[CharacterSubmissionTarget] | None = None,
        shot_type: str = "DIALOGUE",
        sample_positions: tuple[float, ...] | None = None,
    ) -> CharacterEvidenceSubmission: ...


def normalize_reference_view(view: str) -> str:
    normalized = view.strip()
    if not normalized:
        raise ValueError("canonical reference view is required")
    upper = normalized.upper().replace("-", "_")
    result = _VIEW_ALIASES.get(normalized.lower(), upper)
    if result not in {*_VIEW_YAWS, ANY_VIEW}:
        raise ValueError(f"unsupported canonical reference view: {view}")
    return result


def reference_view_for_yaw(pose_yaw: float) -> str:
    yaw = _finite(pose_yaw, "pose_yaw")
    if yaw <= -62.5:
        return LEFT_PROFILE
    if yaw < -15.0:
        return THREE_QUARTER_LEFT
    if yaw <= 15.0:
        return FRONT
    if yaw < 62.5:
        return THREE_QUARTER_RIGHT
    return RIGHT_PROFILE


def select_identity_reference(
    pose_yaw: float,
    references: Sequence[CanonicalIdentityReference],
) -> CanonicalIdentityReference:
    if not references:
        raise ValueError("at least one canonical identity reference is required")
    yaw = _finite(pose_yaw, "pose_yaw")
    target_view = reference_view_for_yaw(yaw)
    normalized = [(reference, normalize_reference_view(reference.view)) for reference in references]
    if any(view == ANY_VIEW for _, view in normalized):
        raise ValueError("canonical identity references require a concrete face view")
    exact = [item for item in normalized if item[1] == target_view]
    candidates = exact or normalized
    return min(
        candidates,
        key=lambda item: (
            abs(_VIEW_YAWS[item[1]] - yaw),
            -_probability(item[0].reference_confidence, "reference confidence"),
            item[0].reference_asset_id,
        ),
    )[0]


def _decode_image(image_bytes: bytes) -> Image.Image:
    if not image_bytes:
        raise ValueError("image evidence bytes cannot be empty")
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.load()
        return image.convert("RGB")


def _crop(image: Image.Image, bounds: BoundingBox | None) -> Image.Image:
    if bounds is None:
        return image.copy()
    width, height = image.size
    left = max(0, min(width - 1, round(bounds.left * width)))
    top = max(0, min(height - 1, round(bounds.top * height)))
    right = max(left + 1, min(width, round(bounds.right * width)))
    bottom = max(top + 1, min(height, round(bounds.bottom * height)))
    return image.crop((left, top, right, bottom))


def _embedding(values: Sequence[float], name: str) -> tuple[float, ...]:
    embedding = tuple(_finite(value, name) for value in values)
    if not embedding:
        raise ValueError(f"{name} returned an empty embedding")
    if math.sqrt(sum(value * value for value in embedding)) == 0:
        raise ValueError(f"{name} returned a zero embedding")
    return embedding


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    first = _embedding(left, "left encoder")
    second = _embedding(right, "right encoder")
    if len(first) != len(second):
        raise ValueError("encoder embeddings must have the same dimensions")
    denominator = math.sqrt(sum(value * value for value in first)) * math.sqrt(
        sum(value * value for value in second)
    )
    similarity = sum(a * b for a, b in zip(first, second, strict=True)) / denominator
    return round(max(0.0, min(1.0, similarity)), 6)


def _weighted_mean(values: Sequence[tuple[float, float]]) -> float | None:
    denominator = sum(weight for _, weight in values)
    if denominator <= 0:
        return None
    return sum(value * weight for value, weight in values) / denominator


def _weighted_p10(values: Sequence[tuple[float, float]]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    total = sum(weight for _, weight in ordered)
    target = total * 0.1
    cumulative = 0.0
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= target:
            return value
    return ordered[-1][0]


def aggregate_character_evidence(
    samples: Sequence[CharacterEvidence],
    threshold_profile: QAThresholdProfile,
) -> CharacterEvidenceAggregate:
    ordered = sorted(samples, key=lambda sample: sample.sample_time)
    usable = [
        sample
        for sample in ordered
        if sample.face_similarity is not None
        and sample.evidence_quality >= threshold_profile.minimum_evidence_quality
    ]
    identity_values = [
        (float(sample.face_similarity), sample.evidence_quality)
        for sample in usable
        if sample.face_similarity is not None
    ]
    average_identity = _weighted_mean(identity_values)
    minimum_identity = min((value for value, _ in identity_values), default=None)
    identity_p10 = _weighted_p10(identity_values)

    drift_slope: float | None = None
    if len(usable) >= 2:
        total_weight = sum(sample.evidence_quality for sample in usable)
        x_mean = sum(sample.sample_time * sample.evidence_quality for sample in usable) / total_weight
        y_mean = (
            sum(
                float(sample.face_similarity) * sample.evidence_quality
                for sample in usable
                if sample.face_similarity is not None
            )
            / total_weight
        )
        denominator = sum(sample.evidence_quality * (sample.sample_time - x_mean) ** 2 for sample in usable)
        if denominator > 0:
            drift_slope = (
                sum(
                    sample.evidence_quality
                    * (sample.sample_time - x_mean)
                    * (float(sample.face_similarity) - y_mean)
                    for sample in usable
                    if sample.face_similarity is not None
                )
                / denominator
            )
        else:
            drift_slope = 0.0

    low_score_duration = 0.0
    if len(usable) >= 2:
        gaps = [
            max(0.0, usable[index + 1].sample_time - sample.sample_time)
            for index, sample in enumerate(usable[:-1])
        ]
        fallback_gap = median(gaps) if gaps else 0.0
        for index, sample in enumerate(usable):
            if float(sample.face_similarity) < threshold_profile.identity_pass:
                low_score_duration += gaps[index] if index < len(gaps) else fallback_gap

    reacquisition_score: float | None = None
    if usable:
        minimum_index = min(
            range(len(usable)),
            key=lambda index: float(usable[index].face_similarity),
        )
        later = usable[minimum_index + 1 :]
        if later:
            reacquisition_score = _weighted_mean(
                [
                    (float(sample.face_similarity), sample.evidence_quality)
                    for sample in later
                    if sample.face_similarity is not None
                ]
            )

    appearance_values = [
        (float(sample.appearance_similarity), sample.evidence_quality)
        for sample in ordered
        if sample.appearance_similarity is not None
        and sample.evidence_quality >= threshold_profile.minimum_evidence_quality
    ]
    views = Counter(sample.reference_view for sample in usable)
    dominant_view = views.most_common(1)[0][0] if views else ANY_VIEW
    visibility = mean(sample.face_visibility for sample in ordered) if ordered else 0.0
    return CharacterEvidenceAggregate(
        average_identity=round(average_identity, 6) if average_identity is not None else None,
        minimum_identity=round(minimum_identity, 6) if minimum_identity is not None else None,
        identity_p10=round(identity_p10, 6) if identity_p10 is not None else None,
        drift_slope=round(drift_slope, 6) if drift_slope is not None else None,
        low_score_duration=round(low_score_duration, 6),
        appearance_similarity=(
            round(value, 6) if (value := _weighted_mean(appearance_values)) is not None else None
        ),
        hair_similarity=UNAVAILABLE,
        costume_similarity=UNAVAILABLE,
        reacquisition_score=(round(reacquisition_score, 6) if reacquisition_score is not None else None),
        usable_samples=len(usable),
        total_samples=len(ordered),
        dominant_face_view=dominant_view,
        average_face_visibility=round(visibility, 6),
    )


class CharacterEvidenceProducer:
    version = "character-evidence-producer-v1"
    default_sample_positions = (0.0, 0.2, 0.4, 0.6, 0.8, 0.98)

    def __init__(
        self,
        *,
        frame_sampler: FrameSampler,
        detector: PersonFaceDetector,
        tracker: CharacterTracker,
        face_encoder: FaceIdentityEncoder,
        appearance_encoder: AppearanceEncoder,
        threshold_registry: QAThresholdRegistry | None = None,
    ):
        self.frame_sampler = frame_sampler
        self.detector = detector
        self.tracker = tracker
        self.face_encoder = face_encoder
        self.appearance_encoder = appearance_encoder
        self.threshold_registry = threshold_registry or QAThresholdRegistry()

    def produce(
        self,
        video_path: Path,
        *,
        candidate_id: str,
        character_id: str,
        references: Sequence[CanonicalIdentityReference],
        shot_type: str = "DIALOGUE",
        sample_positions: tuple[float, ...] | None = None,
    ) -> CharacterEvidenceReport:
        if not candidate_id.strip() or not character_id.strip():
            raise ValueError("candidate_id and character_id are required")
        if not references:
            raise ValueError("canonical identity references are required")
        reference_ids = [reference.reference_asset_id.strip() for reference in references]
        if any(not reference_id for reference_id in reference_ids):
            raise ValueError("canonical reference asset IDs are required")
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("canonical reference asset IDs must be unique")
        positions = sample_positions or self.default_sample_positions
        frames = self.frame_sampler.sample(Path(video_path), positions)
        detection_batches = tuple(
            FrameDetections(frame=frame, detections=tuple(self.detector.detect(frame))) for frame in frames
        )
        tracking = self.tracker.associate(detection_batches, character_id=character_id)
        if tracking.status not in {TRACKED, TRACKING_UNCERTAIN}:
            raise ValueError("tracker returned an unsupported status")

        reference_embeddings: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {}
        for reference in references:
            reference_image = _decode_image(reference.image_bytes)
            reference_embeddings[reference.reference_asset_id] = (
                _embedding(
                    self.face_encoder.encode(_crop(reference_image, reference.face_box)),
                    "face identity encoder",
                ),
                _embedding(
                    self.appearance_encoder.encode(
                        _crop(reference_image, reference.appearance_box or reference.face_box)
                    ),
                    "appearance encoder",
                ),
            )

        assignments = {assignment.frame_index: assignment for assignment in tracking.assignments}
        evidence: list[CharacterEvidence] = []
        for batch in detection_batches:
            assignment = assignments.get(batch.frame.frame_index)
            if assignment is None:
                continue
            detection = next(
                (item for item in batch.detections if item.detection_id == assignment.detection_id),
                None,
            )
            if detection is None:
                raise ValueError("tracker assignment references a missing detection")
            detection_confidence = _probability(detection.detection_confidence, "detection confidence")
            face_visibility = _probability(detection.face_visibility, "face visibility")
            track_confidence = _probability(assignment.track_confidence, "track confidence")
            blur_score = _probability(detection.blur_score, "blur score")
            pose_yaw = _finite(detection.pose_yaw, "pose_yaw")
            reference = select_identity_reference(pose_yaw, references)
            reference_view = normalize_reference_view(reference.view)
            reference_face, reference_appearance = reference_embeddings[reference.reference_asset_id]
            frame_image = _decode_image(batch.frame.image_png)
            face_similarity = None
            if detection.face_box is not None and face_visibility > 0:
                face_similarity = cosine_similarity(
                    self.face_encoder.encode(_crop(frame_image, detection.face_box)),
                    reference_face,
                )
            appearance_similarity = cosine_similarity(
                self.appearance_encoder.encode(_crop(frame_image, detection.person_box)),
                reference_appearance,
            )
            evidence_quality = round(
                detection_confidence
                * track_confidence
                * face_visibility
                * (1.0 - blur_score)
                * _probability(reference.reference_confidence, "reference confidence"),
                6,
            )
            evidence.append(
                CharacterEvidence(
                    candidate_id=candidate_id,
                    character_id=character_id,
                    sample_time=batch.frame.sample_time,
                    face_similarity=face_similarity,
                    appearance_similarity=appearance_similarity,
                    face_visibility=face_visibility,
                    detection_confidence=detection_confidence,
                    track_confidence=track_confidence,
                    pose_yaw=pose_yaw,
                    blur_score=blur_score,
                    reference_asset_id=reference.reference_asset_id,
                    reference_view=reference_view,
                    evidence_quality=evidence_quality,
                    track_id=assignment.track_id,
                    face_encoder_version=self.face_encoder.version,
                    appearance_encoder_version=self.appearance_encoder.version,
                    detector_model_name=self.detector.version,
                    detector_model_version=self.detector.version,
                    tracker_name=self.tracker.version,
                    tracker_version=self.tracker.version,
                    face_detector_model=self.detector.version,
                    face_detector_version=self.detector.version,
                    face_identity_model=self.face_encoder.version,
                    face_identity_version=self.face_encoder.version,
                    appearance_model=self.appearance_encoder.version,
                    appearance_model_version=self.appearance_encoder.version,
                    threshold_version="resolved-after-aggregation",
                    reference_asset_version=reference.reference_asset_version,
                    pipeline_version=self.version,
                )
            )

        preliminary_visibility = mean(item.face_visibility for item in evidence) if evidence else 0.0
        preliminary_views = Counter(item.reference_view for item in evidence)
        preliminary_view = preliminary_views.most_common(1)[0][0] if preliminary_views else ANY_VIEW
        threshold = self.threshold_registry.resolve(
            shot_type=shot_type,
            face_view=preliminary_view,
            face_visibility=preliminary_visibility,
        )
        evidence = [replace(item, threshold_version=threshold.version) for item in evidence]
        aggregate = aggregate_character_evidence(evidence, threshold)
        review_requirements: list[str] = []
        reason_codes = list(tracking.reason_codes)
        if tracking.status == TRACKING_UNCERTAIN:
            review_requirements.append(VLM_REVIEW_REQUIRED)
            if TRACKING_UNCERTAIN not in reason_codes:
                reason_codes.append(TRACKING_UNCERTAIN)
        if aggregate.usable_samples < threshold.minimum_required_samples:
            review_requirements.append(VLM_REVIEW_REQUIRED)
            reason_codes.append("INSUFFICIENT_CHARACTER_EVIDENCE")
        if review_requirements or aggregate.average_identity is None:
            decision = ABSTAIN
        elif (
            aggregate.minimum_identity is not None
            and aggregate.minimum_identity < threshold.identity_hard_fail
        ):
            decision = FAIL
        elif aggregate.average_identity >= threshold.identity_pass:
            decision = PASS
        else:
            decision = ABSTAIN
            review_requirements.append(VLM_REVIEW_REQUIRED)
            reason_codes.append("IDENTITY_GRAY_ZONE")
        return CharacterEvidenceReport(
            producer_run_id=str(uuid.uuid4()),
            producer_version=self.version,
            candidate_id=candidate_id,
            character_id=character_id,
            tracking_status=tracking.status,
            tracking_reason_codes=tuple(dict.fromkeys(reason_codes)),
            review_requirements=tuple(dict.fromkeys(review_requirements)),
            samples=tuple(evidence),
            aggregate=aggregate,
            threshold_profile=threshold,
            pipeline_versions={
                "frame_sampler": self.frame_sampler.version,
                "detector": self.detector.version,
                "tracker": self.tracker.version,
                "face_identity_encoder": self.face_encoder.version,
                "appearance_encoder": self.appearance_encoder.version,
                "threshold_registry": self.threshold_registry.version,
            },
            decision=decision,
        )


__all__ = [
    "ANY_VIEW",
    "AppearanceEncoder",
    "ABSTAIN",
    "AsyncCharacterEvidenceProducer",
    "BoundingBox",
    "CanonicalIdentityReference",
    "CharacterEvidence",
    "CharacterEvidenceAggregate",
    "CharacterEvidenceProducer",
    "CharacterEvidenceReport",
    "CharacterEvidenceSubmission",
    "CharacterTracker",
    "FFmpegFrameSampler",
    "FRONT",
    "FAIL",
    "FaceIdentityEncoder",
    "FrameDetections",
    "FrameSampler",
    "LEFT_PROFILE",
    "PersonFaceDetector",
    "PASS",
    "QAThresholdProfile",
    "QAThresholdRegistry",
    "RIGHT_PROFILE",
    "SHADOW",
    "SampledFrame",
    "THREE_QUARTER_LEFT",
    "THREE_QUARTER_RIGHT",
    "TRACKED",
    "TRACKING_UNCERTAIN",
    "TrackAssignment",
    "TrackingResult",
    "UNAVAILABLE",
    "VLM_REVIEW_REQUIRED",
    "VisualDetection",
    "aggregate_character_evidence",
    "cosine_similarity",
    "normalize_reference_view",
    "reference_view_for_yaw",
    "select_identity_reference",
]
