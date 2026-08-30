from __future__ import annotations

import ipaddress
import json
import math
import socket
import tempfile
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import urlsplit

import httpx

from .model_manifest import load_manifest, verify_artifacts
from .models import (
    ByteTrackTracker,
    DINOv2AppearanceEncoder,
    SFaceIdentityEncoder,
    YOLOXPersonDetector,
    YuNetFaceDetector,
)
from .models.face_detector import DetectedFace
from .models.tracker import TrackedPerson
from .schemas import AnalyzeRequest, CharacterInput

UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ReferenceEmbedding:
    asset_id: str
    asset_version: str
    view: str
    face: Any | None
    appearance: Any


@dataclass(frozen=True)
class EvidenceSample:
    track_id: int
    sample_time: float
    face_similarity: float | None
    appearance_similarity: float
    face_visibility: float
    detection_confidence: float
    track_confidence: float
    pose_yaw: float
    blur_score: float
    reference: ReferenceEmbedding


def _threshold_path() -> Path:
    import os

    configured = os.environ.get("CHARACTER_EVIDENCE_THRESHOLDS_PATH", "").strip()
    return (
        Path(configured)
        if configured
        else Path(__file__).resolve().parents[3]
        / "config/character-evidence/thresholds-v1.json"
    )


def _download(url: str, destination: Path, *, limit: int) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("evidence media must use an absolute credential-free HTTPS URL")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("evidence media host did not resolve exclusively to public addresses")
    size = 0
    with httpx.stream("GET", url, follow_redirects=False, timeout=60.0) as response:
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError("evidence media redirects are forbidden")
        with destination.open("wb") as output:
            for chunk in response.iter_bytes(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise ValueError("evidence media exceeds its byte limit")
                output.write(chunk)
    if not size:
        raise ValueError("evidence media is empty")


def _crop(image: Any, box: tuple[float, float, float, float]) -> Any:
    height, width = image.shape[:2]
    x1, y1 = max(0, round(box[0])), max(0, round(box[1]))
    x2, y2 = min(width, round(box[2])), min(height, round(box[3]))
    return image[y1 : max(y1 + 1, y2), x1 : max(x1 + 1, x2)]


def _face_in_track(track: TrackedPerson, faces: list[DetectedFace]) -> DetectedFace | None:
    inside = [
        face
        for face in faces
        if track.box[0] <= (face.box[0] + face.box[2]) / 2 <= track.box[2]
        and track.box[1] <= (face.box[1] + face.box[3]) / 2 <= track.box[3]
    ]
    return max(inside, key=lambda item: item.confidence, default=None)


def resolve_track_selection(
    tracks: dict[int, list[EvidenceSample]], thresholds: dict[str, Any]
) -> tuple[tuple[int, list[EvidenceSample]] | None, bool, int]:
    """Rank one character's candidate tracks and count identity switches.

    Returns the best-scoring ``(track_id, samples)`` (or ``None`` with no
    tracks), whether the top two scores are too close to trust the assignment,
    and the number of identity switches the evidence implies — attributable
    tracks beyond the first. A track is attributable when its face evidence
    passes the identity threshold, or — with no usable face contradicting it —
    its whole-body appearance passes the appearance threshold. Two attributable
    tracks means the person re-entered under a new track ID, and a decision
    made from one of them would rest on a fraction of the evidence.
    """

    identity_cfg = thresholds["identity"]
    appearance_cfg = thresholds["appearance"]
    ranked: list[tuple[float, int, list[EvidenceSample]]] = []
    attributable_tracks = 0
    for track_id, samples in tracks.items():
        identities = [
            float(item.face_similarity) for item in samples if item.face_similarity is not None
        ]
        identity_mean = mean(identities) if identities else None
        appearance_mean = mean(item.appearance_similarity for item in samples)
        score = (identity_mean or 0.0) * 0.75 + appearance_mean * 0.25
        ranked.append((score, track_id, samples))
        if identity_mean is not None and identity_mean >= identity_cfg["pass_cosine"]:
            attributable_tracks += 1
        elif (
            (identity_mean is None or identity_mean > identity_cfg["fail_cosine"])
            and appearance_mean >= appearance_cfg["pass_cosine"]
        ):
            attributable_tracks += 1
    ranked.sort(reverse=True)
    ambiguous = len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.05
    chosen = (ranked[0][1], ranked[0][2]) if ranked else None
    return chosen, ambiguous, max(0, attributable_tracks - 1)


def _iou(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    area = max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )
    union = (left[2] - left[0]) * (left[3] - left[1]) + (right[2] - right[0]) * (
        right[3] - right[1]
    ) - area
    return area / union if union else 0.0


class CharacterEvidencePipeline:
    def __init__(self) -> None:
        import cv2

        self.cv2 = cv2
        self.manifest = load_manifest()
        verify_artifacts(self.manifest)
        self.thresholds = json.loads(_threshold_path().read_text(encoding="utf-8"))
        if self.thresholds["version"] != self.manifest.threshold_version:
            raise RuntimeError("threshold and model manifest versions diverge")
        root = Path("/models")
        self.detector = YOLOXPersonDetector(root / "yolox_s.pth")
        self.face_detector = YuNetFaceDetector(root / "face_detection_yunet_2026may.onnx")
        self.face_identity = SFaceIdentityEncoder(root / "face_recognition_sface_2021dec.onnx")
        self.appearance = DINOv2AppearanceEncoder(
            Path("/opt/dinov2"), root / "dinov2_vitb14_pretrain.pth"
        )
        self.detector.warmup()
        self.appearance.warmup()

    def _frames(self, path: Path, positions: list[float] | None) -> tuple[list[tuple[float, Any]], float]:
        capture = self.cv2.VideoCapture(str(path), self.cv2.CAP_FFMPEG)
        fps = float(capture.get(self.cv2.CAP_PROP_FPS))
        count = int(capture.get(self.cv2.CAP_PROP_FRAME_COUNT))
        if not capture.isOpened() or not math.isfinite(fps) or fps <= 0 or count <= 0:
            capture.release()
            raise ValueError("video is not valid FFmpeg-decodable media")
        indices = (
            sorted({min(count - 1, max(0, round(value * (count - 1)))) for value in positions})
            if positions
            else list(range(0, count, max(1, round(fps / 2))))[:120]
        )
        frames: list[tuple[float, Any]] = []
        for index in indices:
            capture.set(self.cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = capture.read()
            if ok:
                frames.append((round(index / fps, 6), frame))
        capture.release()
        if not frames:
            raise ValueError("video produced no sampled frames")
        return frames, min(fps, 2.0)

    def _references(
        self, root: Path, character: CharacterInput
    ) -> tuple[list[ReferenceEmbedding], list[str]]:
        required_pixels = int(self.thresholds["evidence"]["minimum_reference_face_pixels"])
        results: list[ReferenceEmbedding] = []
        reasons: list[str] = []
        for index, reference in enumerate(character.reference_assets):
            path = root / f"reference-{index}.image"
            _download(str(reference.url), path, limit=50 * 1024 * 1024)
            image = self.cv2.imread(str(path), self.cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("reference asset is not a decodable image")
            faces = [
                face
                for face in self.face_detector.detect(image)
                if min(face.box[2] - face.box[0], face.box[3] - face.box[1]) >= required_pixels
            ]
            face = max(
                faces,
                key=lambda item: (item.box[2] - item.box[0]) * (item.box[3] - item.box[1]),
                default=None,
            )
            if face is None:
                reasons.append("REFERENCE_FACE_UNAVAILABLE")
            results.append(
                ReferenceEmbedding(
                    reference.asset_id,
                    reference.asset_version,
                    reference.view,
                    self.face_identity.encode_aligned(image, face) if face else None,
                    self.appearance.encode(image),
                )
            )
        return results, reasons

    def _sample(
        self,
        track: TrackedPerson,
        frame: Any,
        faces: list[DetectedFace],
        references: list[ReferenceEmbedding],
        sample_time: float,
    ) -> EvidenceSample:
        import cv2

        body_embedding = self.appearance.encode(_crop(frame, track.box))
        face = _face_in_track(track, faces)
        face_embedding = None
        visibility = 0.0
        blur = 1.0
        yaw = 0.0
        if face:
            face_crop = _crop(frame, face.box)
            variance = float(cv2.Laplacian(cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
            blur = 1.0 / (1.0 + variance / 100.0)
            visibility = max(0.0, min(1.0, face.confidence))
            left_eye, right_eye, nose = face.landmarks[:3]
            width = max(1.0, abs(right_eye[0] - left_eye[0]))
            yaw = max(-90.0, min(90.0, (nose[0] - (left_eye[0] + right_eye[0]) / 2) / width * 90))
            face_pixels = min(face.box[2] - face.box[0], face.box[3] - face.box[1])
            if (
                face_pixels >= self.thresholds["identity"]["minimum_aligned_face_pixels"]
                and blur <= self.thresholds["evidence"]["maximum_blur_score"]
            ):
                # This is the only face embedding path: YuNet landmarks ->
                # SFace alignCrop (five-point alignment) -> SFace feature.
                face_embedding = self.face_identity.encode_aligned(frame, face)
            else:
                visibility *= 0.25
        ranked: list[tuple[float, ReferenceEmbedding, float | None, float]] = []
        for reference in references:
            identity = (
                max(0.0, self.face_identity.cosine(face_embedding, reference.face))
                if face_embedding is not None and reference.face is not None
                else None
            )
            appearance = max(0.0, self.appearance.cosine(body_embedding, reference.appearance))
            rank = (identity or 0.0) * 0.75 + appearance * 0.25
            ranked.append((rank, reference, identity, appearance))
        _, reference, identity, appearance = max(ranked, key=lambda item: item[0])
        return EvidenceSample(
            track.track_id,
            sample_time,
            identity,
            appearance,
            round(visibility, 6),
            track.detection_confidence,
            track.confidence,
            round(yaw, 3),
            round(blur, 6),
            reference,
        )

    def _decision(self, samples: list[EvidenceSample], reasons: list[str]) -> str:
        identities = [float(item.face_similarity) for item in samples if item.face_similarity is not None]
        appearances = [item.appearance_similarity for item in samples]
        identity = self.thresholds["identity"]
        appearance = self.thresholds["appearance"]
        if len(identities) < identity["minimum_usable_face_samples"]:
            reasons.append("INSUFFICIENT_CHARACTER_EVIDENCE")
            return "ABSTAIN"
        if min(item.track_confidence for item in samples) < self.thresholds["tracking"][
            "minimum_track_confidence"
        ]:
            reasons.append("TRACK_CONFIDENCE_INSUFFICIENT")
            return "ABSTAIN"
        identity_score, appearance_score = mean(identities), mean(appearances)
        if identity_score >= identity["pass_cosine"]:
            if appearance_score <= appearance["fail_cosine"]:
                reasons.append("IDENTITY_APPEARANCE_CONFLICT")
                return "ABSTAIN"
            return "PASS"
        if identity_score <= identity["fail_cosine"]:
            if appearance_score >= appearance["pass_cosine"]:
                reasons.append("IDENTITY_APPEARANCE_CONFLICT")
                return "ABSTAIN"
            return "FAIL"
        reasons.append("IDENTITY_GRAY_ZONE")
        return "ABSTAIN"

    def _report(
        self,
        request: AnalyzeRequest,
        character: CharacterInput,
        samples: list[EvidenceSample],
        reasons: list[str],
        uncertain: bool,
    ) -> dict[str, Any]:
        decision = self._decision(samples, reasons) if samples else "ABSTAIN"
        if not samples:
            reasons.append("NO_CHARACTER_TRACK")
        if uncertain:
            decision = "ABSTAIN"
            reasons.append("TRACKING_UNCERTAIN")
        reasons = list(dict.fromkeys(reasons))
        identities = [float(item.face_similarity) for item in samples if item.face_similarity is not None]
        appearances = [item.appearance_similarity for item in samples]
        model = self.manifest.by_role
        threshold = self.thresholds["identity"]
        encoded: list[dict[str, Any]] = []
        for item in samples:
            quality = item.detection_confidence * item.track_confidence * (
                0.35 + 0.65 * item.face_visibility
            )
            encoded.append(
                {
                    "candidate_id": request.job_id,
                    "character_id": character.character_id,
                    "sample_time": item.sample_time,
                    "face_similarity": round(item.face_similarity, 6)
                    if item.face_similarity is not None
                    else None,
                    "appearance_similarity": round(item.appearance_similarity, 6),
                    "face_visibility": item.face_visibility,
                    "detection_confidence": round(item.detection_confidence, 6),
                    "track_confidence": round(item.track_confidence, 6),
                    "pose_yaw": item.pose_yaw,
                    "blur_score": item.blur_score,
                    "reference_asset_id": item.reference.asset_id,
                    "reference_view": item.reference.view,
                    "evidence_quality": round(quality, 6),
                    "track_id": str(item.track_id),
                    "face_encoder_version": "SFace-2021dec",
                    "appearance_encoder_version": "dinov2_vitb14",
                    "hair_similarity": UNAVAILABLE,
                    "costume_similarity": UNAVAILABLE,
                    "detector_model_name": model["person_detection"]["model_name"],
                    "detector_model_version": model["person_detection"]["model_version"],
                    "tracker_name": model["multi_object_tracking"]["model_name"],
                    "tracker_version": model["multi_object_tracking"]["model_version"],
                    "face_detector_model": model["face_detection"]["model_name"],
                    "face_detector_version": model["face_detection"]["model_version"],
                    "face_identity_model": model["face_identity"]["model_name"],
                    "face_identity_version": model["face_identity"]["model_version"],
                    "appearance_model": model["appearance_encoding"]["model_name"],
                    "appearance_model_version": model["appearance_encoding"]["model_version"],
                    "threshold_version": self.manifest.threshold_version,
                    "reference_asset_version": item.reference.asset_version,
                    "pipeline_version": self.manifest.pipeline_version,
                }
            )
        return {
            "producer_run_id": str(uuid.uuid4()),
            "producer_version": "modal-character-evidence-producer-2026-08-27-v1",
            "candidate_id": request.job_id,
            "character_id": character.character_id,
            "tracking_status": "TRACKING_UNCERTAIN" if uncertain else "TRACKED",
            "tracking_reason_codes": reasons,
            "review_requirements": ["VLM_REVIEW_REQUIRED"] if decision == "ABSTAIN" else [],
            "samples": encoded,
            "aggregate": {
                "average_identity": round(mean(identities), 6) if identities else None,
                "minimum_identity": round(min(identities), 6) if identities else None,
                "identity_p10": round(sorted(identities)[max(0, math.ceil(len(identities) / 10) - 1)], 6)
                if identities
                else None,
                "drift_slope": None,
                "low_score_duration": round(
                    sum(0.5 for value in identities if value < threshold["pass_cosine"]), 6
                ),
                "appearance_similarity": round(mean(appearances), 6) if appearances else None,
                "hair_similarity": UNAVAILABLE,
                "costume_similarity": UNAVAILABLE,
                "reacquisition_score": None,
                "usable_samples": len(identities),
                "total_samples": len(samples),
                "dominant_face_view": samples[0].reference.view if samples else "ANY",
                "average_face_visibility": round(mean(item.face_visibility for item in samples), 6)
                if samples
                else 0.0,
            },
            "threshold_profile": {
                "profile_id": "modal-character-evidence-shadow-v1",
                "version": self.manifest.threshold_version,
                "shot_type": request.shot_type,
                "face_view": "ANY",
                "visibility_range": [0.0, 1.0],
                "identity_pass": threshold["pass_cosine"],
                "identity_hard_fail": threshold["fail_cosine"],
                "drift_limit": 0.055,
                "minimum_required_samples": threshold["minimum_usable_face_samples"],
                "minimum_evidence_quality": 0.15,
            },
            "pipeline_versions": {
                "detector": "YOLOX-s@0.1.1rc0",
                "tracker": "ByteTrack@d1bf0191",
                "face_detector": "YuNet@2026may",
                "face_identity_encoder": "SFace@2021dec",
                "appearance_encoder": "dinov2_vitb14",
                "threshold_registry": self.manifest.threshold_version,
                "pipeline": self.manifest.pipeline_version,
            },
            "decision": decision,
            "operating_mode": "SHADOW",
            "model_manifest_version": self.manifest.version,
            "model_provenance": self.manifest.provenance(),
        }

    def analyze(self, request: AnalyzeRequest) -> list[dict[str, Any]]:
        if request.threshold_version != self.manifest.threshold_version:
            raise ValueError("requested threshold version is not deployed")
        positions = request.sample_positions
        if positions and any(not math.isfinite(value) or not 0 <= value <= 1 for value in positions):
            raise ValueError("sample positions must be finite and between zero and one")
        with tempfile.TemporaryDirectory(prefix="character-evidence-") as temporary:
            root = Path(temporary)
            video = root / "candidate.video"
            _download(str(request.video_url), video, limit=500 * 1024 * 1024)
            frames, sampled_fps = self._frames(video, positions)
            references: dict[str, list[ReferenceEmbedding]] = {}
            reason_codes: dict[str, list[str]] = {}
            for character in request.characters:
                references[character.character_id], reason_codes[character.character_id] = (
                    self._references(root, character)
                )
            tracker = ByteTrackTracker(frame_rate=sampled_fps)
            candidates: dict[str, dict[int, list[EvidenceSample]]] = {
                item.character_id: defaultdict(list) for item in request.characters
            }
            crossing = False
            for sample_time, frame in frames:
                detections = self.detector.detect(frame)
                tracks = tracker.update(detections, image_shape=frame.shape[:2])
                crossing = crossing or any(
                    _iou(left.box, right.box) >= 0.30
                    for index, left in enumerate(tracks)
                    for right in tracks[index + 1 :]
                )
                faces = self.face_detector.detect(frame)
                for track in tracks:
                    for character in request.characters:
                        candidates[character.character_id][track.track_id].append(
                            self._sample(
                                track,
                                frame,
                                faces,
                                references[character.character_id],
                                sample_time,
                            )
                        )
            max_id_switches = int(self.thresholds["tracking"]["maximum_id_switches_for_decision"])
            chosen: dict[str, tuple[int, list[EvidenceSample]] | None] = {}
            ambiguous: set[str] = set()
            switch_limited: set[str] = set()
            for character in request.characters:
                selection, is_ambiguous, id_switches = resolve_track_selection(
                    candidates[character.character_id], self.thresholds
                )
                if is_ambiguous:
                    ambiguous.add(character.character_id)
                if id_switches > max_id_switches:
                    switch_limited.add(character.character_id)
                chosen[character.character_id] = selection
            track_owners: dict[int, list[str]] = defaultdict(list)
            for character_id, selection in chosen.items():
                if selection:
                    track_owners[selection[0]].append(character_id)
            for owners in track_owners.values():
                if len(owners) > 1:
                    ambiguous.update(owners)
            reports: list[dict[str, Any]] = []
            for character in request.characters:
                selection = chosen[character.character_id]
                reasons = list(reason_codes[character.character_id])
                if crossing:
                    reasons.append("MULTIPLE_CHARACTER_CROSSING")
                if character.character_id in ambiguous:
                    reasons.append("AMBIGUOUS_TRACK_TO_CHARACTER_ASSIGNMENT")
                if character.character_id in switch_limited:
                    reasons.append("ID_SWITCH_LIMIT_EXCEEDED")
                reports.append(
                    self._report(
                        request,
                        character,
                        selection[1] if selection else [],
                        reasons,
                        crossing
                        or character.character_id in ambiguous
                        or character.character_id in switch_limited,
                    )
                )
            return reports
