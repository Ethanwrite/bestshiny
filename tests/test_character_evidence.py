from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from PIL import Image
from production_domain.models import (
    CandidateStatus,
    Episode,
    GenerationCandidate,
    QADecision,
    Scene,
    Shot,
    TimelineState,
)
from qa_core import (
    FRONT,
    LEFT_PROFILE,
    TRACKED,
    TRACKING_UNCERTAIN,
    UNAVAILABLE,
    VLM_REVIEW_REQUIRED,
    BoundingBox,
    CanonicalIdentityReference,
    CharacterEvidence,
    CharacterEvidenceProducer,
    FFmpegFrameSampler,
    FrameDetections,
    QAThresholdProfile,
    SampledFrame,
    TrackAssignment,
    TrackingResult,
    VisualDetection,
    aggregate_character_evidence,
    select_identity_reference,
)

FIXTURE_VIDEO = Path(__file__).parent / "fixtures" / "video" / "character_evidence_synthetic.mp4"
FULL_FRAME = BoundingBox(0.0, 0.0, 1.0, 1.0)


class _PixelGridEncoder:
    """Deterministic CPU fixture encoder; it is not labeled as a production FaceID model."""

    def __init__(self, version: str):
        self.version = version

    def encode(self, image: Image.Image) -> Sequence[float]:
        reduced = image.convert("RGB").resize((4, 4))
        values: list[float] = []
        for y in range(4):
            for x in range(4):
                pixel = cast(tuple[int, int, int], reduced.getpixel((x, y)))
                values.extend(channel / 255.0 for channel in pixel)
        return tuple(values)


class _FixtureDetector:
    version = "fixture-person-face-detector-v1"

    def __init__(
        self,
        *,
        yaws: tuple[float, ...] = (),
        detection_confidence: float = 0.98,
        face_visibility: float = 0.96,
        blur_score: float = 0.02,
    ):
        self.yaws = yaws
        self.detection_confidence = detection_confidence
        self.face_visibility = face_visibility
        self.blur_score = blur_score

    def detect(self, frame: SampledFrame) -> tuple[VisualDetection, ...]:
        yaw = self.yaws[frame.frame_index] if frame.frame_index < len(self.yaws) else 0.0
        return (
            VisualDetection(
                detection_id=f"fixture-detection-{frame.frame_index}",
                person_box=FULL_FRAME,
                face_box=FULL_FRAME,
                detection_confidence=self.detection_confidence,
                face_visibility=self.face_visibility,
                pose_yaw=yaw,
                blur_score=self.blur_score,
            ),
        )


class _FixtureTracker:
    version = "fixture-character-tracker-v1"

    def __init__(self, *, status: str = TRACKED, confidence: float = 0.97):
        self.status = status
        self.confidence = confidence

    def associate(
        self,
        detections: tuple[FrameDetections, ...],
        *,
        character_id: str,
    ) -> TrackingResult:
        del character_id
        assignments = tuple(
            TrackAssignment(
                frame_index=batch.frame.frame_index,
                detection_id=batch.detections[0].detection_id,
                track_id="fixture-track-1",
                track_confidence=self.confidence,
            )
            for batch in detections
        )
        reasons = ("MULTIPLE_CHARACTER_CROSSING",) if self.status == TRACKING_UNCERTAIN else ()
        return TrackingResult(self.status, assignments, reasons)


def _producer(*, tracker_status: str = TRACKED, yaws: tuple[float, ...] = ()) -> CharacterEvidenceProducer:
    return CharacterEvidenceProducer(
        frame_sampler=FFmpegFrameSampler(),
        detector=_FixtureDetector(yaws=yaws),
        tracker=_FixtureTracker(status=tracker_status),
        face_encoder=_PixelGridEncoder("fixture-face-identity-encoder-v1"),
        appearance_encoder=_PixelGridEncoder("fixture-appearance-encoder-v1"),
    )


def _reference_bytes() -> bytes:
    return FFmpegFrameSampler().sample(FIXTURE_VIDEO, (0.0,))[0].image_png


def _reference(asset_id: str = "fixture-front", view: str = FRONT) -> CanonicalIdentityReference:
    return CanonicalIdentityReference(
        reference_asset_id=asset_id,
        view=view,
        image_bytes=_reference_bytes(),
        face_box=FULL_FRAME,
        appearance_box=FULL_FRAME,
    )


def _png(color: tuple[int, int, int]) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (24, 24), color).save(stream, format="PNG")
    return stream.getvalue()


def _candidate_for_fixture(container, project) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Evidence", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Synthetic evidence stage")
        session.add(scene)
        session.flush()
        input_state = TimelineState(
            project_id=project.id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_INPUT",
        )
        output_state = TimelineState(
            project_id=project.id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_OUTPUT",
        )
        session.add_all([input_state, output_state])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=1,
            prompt="Synthetic character remains visible.",
            user_prompt="Synthetic character remains visible.",
            compiled_prompt="Synthetic character remains visible.",
            input_state_id=input_state.id,
            output_state_id=output_state.id,
        )
        session.add(shot)
        session.flush()
        candidate = GenerationCandidate(
            shot_id=shot.id,
            attempt_number=1,
            status=CandidateStatus.VALIDATING.value,
        )
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
        shot_id = shot.id
    with FIXTURE_VIDEO.open("rb") as stream:
        asset, _ = container.media.register(
            project.id,
            "VIDEO",
            stream,
            filename="character-evidence-synthetic.mp4",
            mime_type="video/mp4",
            shot_id=shot_id,
            generation_candidate_id=candidate_id,
            metadata={"fixture_license": "SELF_GENERATED_SYNTHETIC"},
        )
    with container.database.session() as session:
        session.get(GenerationCandidate, candidate_id).output_asset_id = asset.id
    return candidate_id


def _semantic_scores() -> dict[str, Any]:
    return {
        "scene_score": 0.9,
        "composition_score": 0.9,
        "action_score": 0.9,
        "camera_score": 0.9,
        "lighting_score": 0.9,
        "narrative_score": 0.9,
    }


def test_character_evidence_from_real_fixture_video() -> None:
    report = _producer().produce(
        FIXTURE_VIDEO,
        candidate_id="fixture-candidate",
        character_id="fixture-character",
        references=[_reference()],
    )

    assert len(report.samples) == 6
    assert report.tracking_status == TRACKED
    assert report.review_requirements == ()
    assert report.aggregate.usable_samples == 6
    assert report.aggregate.average_identity is not None
    assert report.aggregate.average_identity > 0.999
    assert report.aggregate.appearance_similarity is not None
    assert report.aggregate.appearance_similarity > 0.999
    assert report.aggregate.hair_similarity == UNAVAILABLE
    assert report.aggregate.costume_similarity == UNAVAILABLE
    assert all(sample.reference_asset_id == "fixture-front" for sample in report.samples)
    assert all(sample.face_encoder_version == "fixture-face-identity-encoder-v1" for sample in report.samples)
    assert report.pipeline_versions["frame_sampler"] == "ffmpeg-frame-sampler-v1"
    assert report.threshold_profile.version == "character-identity-thresholds-2026-08-21-v1"


def test_view_aware_identity_reference_selection() -> None:
    front = CanonicalIdentityReference("front", FRONT, _png((20, 40, 220)))
    left_profile = CanonicalIdentityReference("left", LEFT_PROFILE, _png((220, 40, 20)))

    assert select_identity_reference(-82.0, [front, left_profile]).reference_asset_id == "left"
    assert select_identity_reference(2.0, [front, left_profile]).reference_asset_id == "front"

    report = _producer(yaws=(-82.0,)).produce(
        FIXTURE_VIDEO,
        candidate_id="profile-candidate",
        character_id="profile-character",
        references=[front, left_profile],
        sample_positions=(0.0,),
    )
    assert report.samples[0].reference_asset_id == "left"
    assert report.samples[0].reference_view == LEFT_PROFILE


def test_low_confidence_face_not_overweighted() -> None:
    profile = QAThresholdProfile(
        profile_id="confidence-weight-test-v1",
        version="test-v1",
        shot_type="DIALOGUE",
        face_view=FRONT,
        visibility_range=(0.0, 1.0),
        identity_pass=0.75,
        identity_hard_fail=0.5,
        drift_limit=0.05,
        minimum_required_samples=1,
        minimum_evidence_quality=0.1,
    )
    high_quality = CharacterEvidence(
        candidate_id="candidate",
        character_id="character",
        sample_time=0.0,
        face_similarity=0.95,
        appearance_similarity=0.9,
        face_visibility=0.99,
        detection_confidence=0.99,
        track_confidence=0.99,
        pose_yaw=0.0,
        blur_score=0.0,
        reference_asset_id="front",
        reference_view=FRONT,
        evidence_quality=0.97,
        track_id="track",
        face_encoder_version="face-v1",
        appearance_encoder_version="appearance-v1",
    )
    low_quality = [
        CharacterEvidence(
            **{
                **high_quality.to_dict(),
                "sample_time": float(index + 1),
                "face_similarity": 0.05,
                "evidence_quality": 0.01,
            }
        )
        for index in range(20)
    ]

    aggregate = aggregate_character_evidence([high_quality, *low_quality], profile)

    assert aggregate.usable_samples == 1
    assert aggregate.average_identity == 0.95
    assert aggregate.minimum_identity == 0.95


def test_character_evidence_drives_qa_without_scalar_identity_samples(
    container,
    project,
) -> None:  # type: ignore[no-untyped-def]
    candidate_id = _candidate_for_fixture(container, project)
    container.qa.evidence_producer = _producer()

    result = container.qa.validate_candidate_with_character_evidence(
        candidate_id,
        character_id="fixture-character",
        references=[_reference()],
        semantic_evidence=_semantic_scores(),
    )

    assert result.decision == QADecision.PASS.value
    assert result.metrics_json["evidence_source"] == "CHARACTER_EVIDENCE_PRODUCER_V1"
    assert result.metrics_json["identity"]["usable_samples"] == 6
    assert result.metrics_json["character_evidence"]["samples"][0]["face_similarity"] > 0.999
    assert result.metrics_json["character_evidence"]["aggregate"]["hair_similarity"] == UNAVAILABLE


def test_tracking_uncertain_requires_semantic_review(container, project) -> None:  # type: ignore[no-untyped-def]
    candidate_id = _candidate_for_fixture(container, project)
    container.qa.evidence_producer = _producer(tracker_status=TRACKING_UNCERTAIN)

    result = container.qa.validate_candidate_with_character_evidence(
        candidate_id,
        character_id="fixture-character",
        references=[_reference()],
        semantic_evidence=_semantic_scores(),
    )

    assert result.decision == QADecision.USER_REVIEW_REQUIRED.value
    assert result.hard_failures == []
    assert result.metrics_json["evidence_source"] == "CHARACTER_EVIDENCE_PRODUCER_V1"
    assert result.metrics_json["semantic_review_required"] is True
    assert result.metrics_json["semantic_review_reason"] == VLM_REVIEW_REQUIRED
    persisted = result.metrics_json["character_evidence"]
    assert persisted["tracking_status"] == TRACKING_UNCERTAIN
    assert VLM_REVIEW_REQUIRED in persisted["review_requirements"]
    assert persisted["samples"][0]["reference_asset_id"] == "fixture-front"
    assert persisted["samples"][0]["hair_similarity"] == UNAVAILABLE
    assert persisted["threshold_profile"]["version"] == "character-identity-thresholds-2026-08-21-v1"
    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        assert candidate.status == CandidateStatus.USER_REVIEW_REQUIRED.value
        assert candidate.metadata_json["character_evidence_run_id"] == persisted["producer_run_id"]
