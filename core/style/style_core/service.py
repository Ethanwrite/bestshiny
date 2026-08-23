from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from PIL import Image, ImageFilter
from platform_database import Database
from platform_shared import StorageProvider
from production_domain.models import (
    Asset,
    AssetKind,
    AssetVersion,
    AssetVersionMedia,
    AssetVersionStatus,
    CandidateStyleEvaluation,
    GenerationCandidate,
    MediaAsset,
    Project,
    ProjectStyleLock,
    Shot,
    StyleEmbedding,
    User,
)
from qa_core import FFmpegFrameSampler
from sqlalchemy import select

from .semantic import SemanticStyleEmbedder, SemanticStyleUnavailable


class StyleLockConflict(RuntimeError):
    pass


class StyleCommitViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class StyleGenerationControl:
    lock_id: str
    asset_id: str
    version_id: str
    embedding_id: str
    embedding_model: str
    embedding_hash: str
    embedding: tuple[float, ...]
    reference_media_ids: tuple[str, ...]
    name: str
    constraints: tuple[str, ...]

    def prompt_view(self) -> dict[str, Any]:
        return {
            "lock_id": self.lock_id,
            "asset_id": self.asset_id,
            "version_id": self.version_id,
            "embedding_id": self.embedding_id,
            "embedding_model": self.embedding_model,
            "embedding_hash": self.embedding_hash,
            "name": self.name,
            "constraints": list(self.constraints),
        }

    def provider_view(self) -> dict[str, Any]:
        return {**self.prompt_view(), "embedding": list(self.embedding)}


class LocalStyleDescriptor:
    """Deterministic 64-D color/tonal/edge descriptor for offline style control.

    This is an auditable fallback, not a claim that a learned production vision
    encoder has been calibrated. Provider-backed replacements can create a new
    immutable embedding row under a different model name.
    """

    provider = "LOCAL_DETERMINISTIC"
    model = "visual-style-descriptor-64d"
    version = "style-descriptor-v1"
    dimension = 64

    @staticmethod
    def _histogram(values: list[int], bins: int, maximum: int = 256) -> list[float]:
        counts = [0] * bins
        for value in values:
            counts[min(bins - 1, max(0, int(value) * bins // maximum))] += 1
        total = max(1, len(values))
        return [count / total for count in counts]

    @staticmethod
    def _pixels(image: Image.Image) -> list[Any]:
        reader = getattr(image, "get_flattened_data", image.getdata)
        return list(reader())

    @classmethod
    def image_embedding(cls, image: Image.Image) -> list[float]:
        rgb = image.convert("RGB")
        rgb.thumbnail((256, 256), Image.Resampling.LANCZOS)
        pixels = cls._pixels(rgb)
        features: list[float] = []
        for channel in range(3):
            features.extend(cls._histogram([pixel[channel] for pixel in pixels], 8))
        luminance = rgb.convert("L")
        features.extend(cls._histogram(cls._pixels(luminance), 16))
        saturation = rgb.convert("HSV").getchannel("S")
        features.extend(cls._histogram(cls._pixels(saturation), 8))
        edges = luminance.filter(ImageFilter.FIND_EDGES)
        features.extend(cls._histogram(cls._pixels(edges), 8))
        grid = luminance.resize((4, 2), Image.Resampling.BILINEAR)
        features.extend(float(value) / 255.0 for value in cls._pixels(grid))
        if len(features) != cls.dimension:
            raise RuntimeError("style descriptor dimension changed unexpectedly")
        norm = math.sqrt(sum(value * value for value in features))
        if norm <= 0:
            raise ValueError("style reference has no measurable visual signal")
        return [round(value / norm, 8) for value in features]

    @classmethod
    def bytes_embedding(cls, payload: bytes) -> list[float]:
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            return cls.image_embedding(image)

    @staticmethod
    def aggregate(vectors: list[list[float]]) -> list[float]:
        if not vectors:
            raise ValueError("at least one style reference image is required")
        dimension = len(vectors[0])
        if dimension == 0 or any(len(vector) != dimension for vector in vectors):
            raise ValueError("style embedding dimensions are inconsistent")
        averaged = [mean(vector[index] for vector in vectors) for index in range(dimension)]
        norm = math.sqrt(sum(value * value for value in averaged))
        if norm <= 0:
            raise ValueError("style embedding aggregate has zero magnitude")
        return [round(value / norm, 8) for value in averaged]

    @staticmethod
    def similarity(left: list[float] | tuple[float, ...], right: list[float]) -> float:
        if not left or len(left) != len(right):
            raise ValueError("style embedding dimensions do not match")
        left_norm = math.sqrt(sum(value * value for value in left))
        right_norm = math.sqrt(sum(value * value for value in right))
        if left_norm <= 0 or right_norm <= 0:
            raise ValueError("style embedding has zero magnitude")
        score = sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)
        return max(0.0, min(1.0, score))


@dataclass(frozen=True)
class SemanticReferenceAttempt:
    """The layer-2 reference for a style version, or why there is not one."""

    embedding: StyleEmbedding | None
    reason: str | None


def _worst_status(deterministic: str, semantic: str | None) -> str:
    """Combine layer verdicts by severity. A missing layer is never a pass.

    FAIL beats REVIEW_REQUIRED beats PASS, so a candidate is only committable
    when every configured layer agreed it was.
    """

    order = {"PASS": 0, "REVIEW_REQUIRED": 1, "FAIL": 2}
    worst = deterministic
    if semantic is not None and order[semantic] > order[worst]:
        worst = semantic
    return worst


class ProjectStyleService:
    evaluator_version = "project-style-qa-v1"
    sample_positions = (0.0, 0.2, 0.4, 0.6, 0.8, 0.98)

    def __init__(
        self,
        database: Database,
        storage: StorageProvider,
        descriptor: LocalStyleDescriptor | None = None,
        semantic: SemanticStyleEmbedder | None = None,
    ):
        self.database = database
        self.storage = storage
        self.descriptor = descriptor or LocalStyleDescriptor()
        # Layer 2. Absent means the deterministic gate runs alone, which is the
        # pre-existing behaviour, not a weaker version of a two-layer gate.
        self.semantic = semantic
        self.frame_sampler = FFmpegFrameSampler()

    @staticmethod
    def _vector_hash(vector: list[float]) -> str:
        encoded = json.dumps(vector, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _media_for_version(session: Any, version: AssetVersion) -> list[MediaAsset]:
        media_ids = [version.primary_media_asset_id] if version.primary_media_asset_id else []
        media_ids.extend(
            session.scalars(
                select(AssetVersionMedia.media_asset_id)
                .where(AssetVersionMedia.asset_version_id == version.id)
                .order_by(AssetVersionMedia.sort_order, AssetVersionMedia.id)
            )
        )
        unique_ids = list(dict.fromkeys(media_id for media_id in media_ids if media_id))
        return [media for media_id in unique_ids if (media := session.get(MediaAsset, media_id))]

    def _media_vectors(self, media: MediaAsset) -> list[list[float]]:
        if media.mime_type.startswith("image/"):
            with self.storage.open(media.storage_key, "rb") as stream:
                return [self.descriptor.bytes_embedding(stream.read())]
        if media.mime_type.startswith("video/"):
            path = Path(media.local_path or self.storage.path_for(media.storage_key))
            frames = self.frame_sampler.sample(path, (0.0, 0.5, 0.98))
            return [self.descriptor.bytes_embedding(frame.image_png) for frame in frames]
        return []

    def ensure_embedding(self, style_version_id: str) -> StyleEmbedding:
        with self.database.session() as session:
            existing = session.scalar(
                select(StyleEmbedding).where(
                    StyleEmbedding.asset_version_id == style_version_id,
                    StyleEmbedding.model == self.descriptor.model,
                )
            )
            if existing:
                return existing
            version = session.get(AssetVersion, style_version_id)
            asset = session.get(Asset, version.asset_id) if version else None
            if not version or not asset:
                raise LookupError("style asset version not found")
            if asset.asset_type != AssetKind.STYLE.value:
                raise ValueError("style embedding can only be extracted from a STYLE asset version")
            if version.status != AssetVersionStatus.READY.value:
                raise ValueError("style embedding requires a READY asset version")
            media = self._media_for_version(session, version)
            project_id = asset.project_id
        vectors: list[list[float]] = []
        usable_media: list[MediaAsset] = []
        for item in media:
            item_vectors = self._media_vectors(item)
            if item_vectors:
                usable_media.append(item)
                vectors.extend(item_vectors)
        if not vectors:
            raise ValueError("style asset version needs at least one decodable image or video reference")
        vector = self.descriptor.aggregate(vectors)
        embedding_hash = self._vector_hash(vector)
        with self.database.session() as session:
            existing = session.scalar(
                select(StyleEmbedding).where(
                    StyleEmbedding.asset_version_id == style_version_id,
                    StyleEmbedding.model == self.descriptor.model,
                )
            )
            if existing:
                return SemanticReferenceAttempt(existing, None)
            embedding = StyleEmbedding(
                project_id=project_id,
                asset_version_id=style_version_id,
                embedding=vector,
                dimension=len(vector),
                provider=self.descriptor.provider,
                model=self.descriptor.model,
                algorithm_version=self.descriptor.version,
                embedding_hash=embedding_hash,
                source_media_ids=[item.id for item in usable_media],
                source_media_hashes=[item.sha256 for item in usable_media],
                evidence_kind="DETERMINISTIC_LOCAL",
                metadata_json={"frame_count": len(vectors), "network_used": False},
            )
            session.add(embedding)
            session.flush()
            return embedding

    def ensure_semantic_embedding(self, style_version_id: str) -> StyleEmbedding | None:
        return self.semantic_reference(style_version_id).embedding

    def semantic_reference(self, style_version_id: str) -> SemanticReferenceAttempt:
        """Extract the locked style's semantic reference, and say why if it cannot.

        A missing layer 2 is not an error — a project may be locked before layer
        2 is switched on, and it then keeps the deterministic gate rather than
        acquiring a second gate whose reference was chosen after the fact. But
        it must not be *silent*: with the feature enabled and the provider
        transport in mock mode, every lock would quietly come out single-layer
        and look identical to one made with the feature off. The reason is
        returned so `lock()` can record it on the lock itself.
        """

        if self.semantic is None:
            return SemanticReferenceAttempt(None, "SEMANTIC_EMBEDDER_NOT_CONFIGURED")
        with self.database.session() as session:
            existing = session.scalar(
                select(StyleEmbedding).where(
                    StyleEmbedding.asset_version_id == style_version_id,
                    StyleEmbedding.model == self.semantic.model,
                )
            )
            if existing:
                return SemanticReferenceAttempt(existing, None)
            version = session.get(AssetVersion, style_version_id)
            asset = session.get(Asset, version.asset_id) if version else None
            if not version or not asset:
                raise LookupError("style asset version not found")
            media = self._media_for_version(session, version)
            project_id = asset.project_id

        frames: list[bytes] = []
        usable_media: list[MediaAsset] = []
        for item in media:
            item_frames = self._media_frames(item)
            if item_frames:
                usable_media.append(item)
                frames.extend(item_frames)
        if not frames:
            return SemanticReferenceAttempt(None, "SEMANTIC_REFERENCE_MEDIA_UNREADABLE")
        try:
            vectors = self.semantic.embed_images(frames, project_id=project_id)
        except SemanticStyleUnavailable as exc:
            return SemanticReferenceAttempt(None, f"SEMANTIC_MODEL_UNAVAILABLE:{exc}"[:400])
        vector = self.descriptor.aggregate(vectors)
        embedding_hash = self._vector_hash(vector)
        with self.database.session() as session:
            existing = session.scalar(
                select(StyleEmbedding).where(
                    StyleEmbedding.asset_version_id == style_version_id,
                    StyleEmbedding.model == self.semantic.model,
                )
            )
            if existing:
                return existing
            embedding = StyleEmbedding(
                project_id=project_id,
                asset_version_id=style_version_id,
                embedding=vector,
                dimension=len(vector),
                provider=self.semantic.provider,
                model=self.semantic.model,
                algorithm_version=getattr(self.semantic, "version", "semantic-style-v1"),
                embedding_hash=embedding_hash,
                source_media_ids=[item.id for item in usable_media],
                source_media_hashes=[item.sha256 for item in usable_media],
                evidence_kind="MODEL_SEMANTIC",
                metadata_json={"frame_count": len(frames), "network_used": True},
            )
            session.add(embedding)
            session.flush()
            return SemanticReferenceAttempt(embedding, None)

    def _media_frames(self, media: MediaAsset) -> list[bytes]:
        """Raw frame bytes for a semantic embedder, which reads pixels not stats."""

        if media.mime_type.startswith("image/"):
            with self.storage.open(media.storage_key, "rb") as stream:
                return [stream.read()]
        if media.mime_type.startswith("video/"):
            path = Path(media.local_path or self.storage.path_for(media.storage_key))
            return [frame.image_png for frame in self.frame_sampler.sample(path, (0.0, 0.5, 0.98))]
        return []

    def lock(
        self,
        project_id: str,
        style_version_id: str,
        *,
        locked_by_user_id: str,
        reason: str,
        explicit_confirmation: bool,
        similarity_threshold: float = 0.72,
        minimum_similarity_threshold: float = 0.55,
        drift_limit: float = 0.06,
        max_low_score_fraction: float = 0.5,
    ) -> ProjectStyleLock:
        if explicit_confirmation is not True:
            raise ValueError("locking project style requires explicit boolean confirmation")
        if not locked_by_user_id.strip():
            raise ValueError("locking project style requires an authenticated user")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("locking project style requires a reason")
        thresholds = (
            similarity_threshold,
            minimum_similarity_threshold,
            drift_limit,
            max_low_score_fraction,
        )
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in thresholds):
            raise ValueError("style QA thresholds must be finite values between zero and one")
        embedding = self.ensure_embedding(style_version_id)
        # Layer 2's reference is extracted at lock time, from the same version,
        # so the two layers can never describe different frames.
        semantic_attempt = self.semantic_reference(style_version_id)
        semantic_embedding_id = (
            semantic_attempt.embedding.id if semantic_attempt.embedding else None
        )
        with self.database.session() as session:
            project = session.scalar(select(Project).where(Project.id == project_id).with_for_update())
            if not project:
                raise LookupError("project not found")
            existing = session.scalar(
                select(ProjectStyleLock).where(ProjectStyleLock.project_id == project_id)
            )
            if existing:
                if existing.style_version_id == style_version_id:
                    return existing
                raise StyleLockConflict("project visual style is already locked and cannot be replaced")
            version = session.get(AssetVersion, style_version_id)
            asset = session.get(Asset, version.asset_id) if version else None
            current_embedding = session.get(StyleEmbedding, embedding.id)
            actor = session.get(User, locked_by_user_id)
            if not version or not asset or not current_embedding or not actor:
                raise LookupError("style lock provenance disappeared")
            if asset.project_id != project_id or asset.asset_type != AssetKind.STYLE.value:
                raise ValueError("style version does not belong to this project's STYLE asset")
            if asset.canonical_version_id != version.id:
                raise ValueError("style version must be explicitly promoted to Canonical before locking")
            if version.status != AssetVersionStatus.READY.value:
                raise ValueError("only a READY style version can be locked")
            style_lock = ProjectStyleLock(
                project_id=project_id,
                style_asset_id=asset.id,
                style_version_id=version.id,
                style_embedding_id=current_embedding.id,
                semantic_style_embedding_id=semantic_embedding_id,
                similarity_threshold=similarity_threshold,
                minimum_similarity_threshold=minimum_similarity_threshold,
                drift_limit=drift_limit,
                max_low_score_fraction=max_low_score_fraction,
                locked_by_user_id=locked_by_user_id,
                reason=normalized_reason,
                metadata_json={
                    "explicit_confirmation": True,
                    "lock_version": "project-style-lock-v1",
                    "style_layers": 2 if semantic_embedding_id else 1,
                    # Present only when layer 2 was wanted and could not be
                    # produced, so a single-layer lock is never indistinguishable
                    # from one made before the layer existed.
                    "semantic_layer_absent_reason": semantic_attempt.reason,
                },
            )
            session.add(style_lock)
            session.flush()
            project.canonical_style_version_id = version.id
            session.flush()
            return style_lock

    def generation_control(self, project_id: str) -> StyleGenerationControl | None:
        with self.database.session() as session:
            project = session.get(Project, project_id)
            if not project or not project.canonical_style_version_id:
                return None
            style_lock = session.scalar(
                select(ProjectStyleLock).where(ProjectStyleLock.project_id == project_id)
            )
            if not style_lock or style_lock.style_version_id != project.canonical_style_version_id:
                raise StyleLockConflict("project style pointer has no matching immutable lock")
            embedding = session.get(StyleEmbedding, style_lock.style_embedding_id)
            version = session.get(AssetVersion, style_lock.style_version_id)
            asset = session.get(Asset, style_lock.style_asset_id)
            if not embedding or not version or not asset:
                raise StyleLockConflict("project style lock provenance is incomplete")
            media = self._media_for_version(session, version)
            constraints = tuple(
                str(item) for item in asset.canonical_metadata.get("constraints", []) if str(item).strip()
            )
            return StyleGenerationControl(
                lock_id=style_lock.id,
                asset_id=asset.id,
                version_id=version.id,
                embedding_id=embedding.id,
                embedding_model=embedding.model,
                embedding_hash=embedding.embedding_hash,
                embedding=tuple(float(value) for value in embedding.embedding),
                reference_media_ids=tuple(
                    item.id for item in media if item.mime_type.startswith(("image/", "video/"))
                ),
                name=asset.name,
                constraints=constraints,
            )

    @staticmethod
    def _drift_slope(scores: list[float]) -> float:
        if len(scores) < 2:
            return 0.0
        x_mean = (len(scores) - 1) / 2
        denominator = sum((index - x_mean) ** 2 for index in range(len(scores)))
        return (
            sum((index - x_mean) * (score - mean(scores)) for index, score in enumerate(scores)) / denominator
            if denominator
            else 0.0
        )

    def evaluate_candidate(self, candidate_id: str) -> CandidateStyleEvaluation | None:
        with self.database.session() as session:
            existing = session.scalar(
                select(CandidateStyleEvaluation).where(CandidateStyleEvaluation.candidate_id == candidate_id)
            )
            if existing:
                return existing
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate or not candidate.output_asset_id:
                raise LookupError("candidate output is not available for style QA")
            shot = session.get(Shot, candidate.shot_id)
            if not shot:
                raise LookupError("candidate shot is not available for style QA")
            project_id = shot.scene.episode.project_id
            project = session.get(Project, project_id)
            if not project or not project.canonical_style_version_id:
                return None
            style_lock = session.scalar(
                select(ProjectStyleLock).where(ProjectStyleLock.project_id == project_id)
            )
            embedding = session.get(StyleEmbedding, style_lock.style_embedding_id) if style_lock else None
            semantic_embedding = (
                session.get(StyleEmbedding, style_lock.semantic_style_embedding_id)
                if style_lock and style_lock.semantic_style_embedding_id
                else None
            )
            output = session.get(MediaAsset, candidate.output_asset_id)
            if not style_lock or not embedding or not output:
                raise StyleLockConflict("locked style or candidate output provenance is incomplete")
            output_asset_id = output.id
            output_storage_key = output.storage_key
            output_mime_type = output.mime_type
            output_path = Path(output.local_path or self.storage.path_for(output.storage_key))
            target = [float(value) for value in embedding.embedding]
            semantic_target = (
                [float(value) for value in semantic_embedding.embedding] if semantic_embedding else None
            )
            semantic_threshold = style_lock.semantic_similarity_threshold

        positions: list[float] = []
        vectors: list[list[float]] = []
        raw_frames: list[bytes] = []
        reason_codes: list[str] = []
        try:
            if output_mime_type.startswith("image/"):
                with self.storage.open(output_storage_key, "rb") as stream:
                    payload = stream.read()
                raw_frames = [payload]
                vectors = [self.descriptor.bytes_embedding(payload)]
                positions = [0.0]
            elif output_mime_type.startswith("video/"):
                frames = self.frame_sampler.sample(output_path, self.sample_positions)
                raw_frames = [frame.image_png for frame in frames]
                vectors = [self.descriptor.bytes_embedding(frame.image_png) for frame in frames]
                positions = [frame.normalized_position for frame in frames]
            else:
                reason_codes.append("STYLE_EVIDENCE_UNSUPPORTED_MEDIA")
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            reason_codes.append("STYLE_EVIDENCE_UNAVAILABLE")

        scores = [self.descriptor.similarity(target, vector) for vector in vectors]
        average = mean(scores) if scores else None
        minimum = min(scores) if scores else None
        ordered = sorted(scores)
        p10 = ordered[max(0, math.ceil(len(ordered) * 0.1) - 1)] if ordered else None
        slope = self._drift_slope(scores) if scores else None
        low_fraction = (
            sum(score < style_lock.similarity_threshold for score in scores) / len(scores) if scores else None
        )
        if scores:
            if average is not None and average < style_lock.similarity_threshold:
                reason_codes.append("STYLE_SIMILARITY_TOO_LOW")
            if minimum is not None and minimum < style_lock.minimum_similarity_threshold:
                reason_codes.append("STYLE_MINIMUM_TOO_LOW")
            if (
                slope is not None
                and slope <= -style_lock.drift_limit
                and minimum is not None
                and minimum < style_lock.similarity_threshold
            ):
                reason_codes.append("STYLE_DRIFT")
            if low_fraction is not None and low_fraction > style_lock.max_low_score_fraction:
                reason_codes.append("STYLE_LOW_SCORE_FRACTION_EXCEEDED")
        deterministic_status = "REVIEW_REQUIRED" if not scores else ("FAIL" if reason_codes else "PASS")

        # --- layer 2 -----------------------------------------------------
        # Runs only when this lock carries a semantic reference. It answers a
        # different question from layer 1 — medium, brushwork, photographic
        # language — so its verdict is recorded separately and combined by
        # taking the worst, never averaged into a single number that could let
        # one layer's confidence cover the other's objection.
        semantic_status: str | None = None
        semantic_scores: list[float] = []
        if semantic_target is not None:
            if not raw_frames:
                semantic_status = "REVIEW_REQUIRED"
                reason_codes.append("STYLE_SEMANTIC_EVIDENCE_UNAVAILABLE")
            elif self.semantic is None:
                # The lock was made with a semantic reference and this process
                # cannot produce one. Passing on layer 1 alone would silently
                # weaken a gate the project was locked under.
                semantic_status = "REVIEW_REQUIRED"
                reason_codes.append("STYLE_SEMANTIC_EMBEDDER_NOT_CONFIGURED")
            else:
                try:
                    semantic_vectors = self.semantic.embed_images(raw_frames, project_id=project_id)
                    semantic_scores = [
                        self.descriptor.similarity(semantic_target, vector)
                        for vector in semantic_vectors
                    ]
                except SemanticStyleUnavailable:
                    semantic_status = "REVIEW_REQUIRED"
                    reason_codes.append("STYLE_SEMANTIC_MODEL_UNAVAILABLE")
                else:
                    semantic_average = mean(semantic_scores) if semantic_scores else None
                    semantic_minimum = min(semantic_scores) if semantic_scores else None
                    if semantic_average is None:
                        semantic_status = "REVIEW_REQUIRED"
                        reason_codes.append("STYLE_SEMANTIC_EVIDENCE_UNAVAILABLE")
                    elif semantic_average < semantic_threshold:
                        semantic_status = "FAIL"
                        reason_codes.append("STYLE_SEMANTIC_SIMILARITY_TOO_LOW")
                    elif semantic_minimum is not None and semantic_minimum < semantic_threshold * 0.85:
                        semantic_status = "FAIL"
                        reason_codes.append("STYLE_SEMANTIC_MINIMUM_TOO_LOW")
                    else:
                        semantic_status = "PASS"

        semantic_average_similarity = mean(semantic_scores) if semantic_scores else None
        semantic_minimum_similarity = min(semantic_scores) if semantic_scores else None
        status = _worst_status(deterministic_status, semantic_status)
        with self.database.session() as session:
            existing = session.scalar(
                select(CandidateStyleEvaluation).where(CandidateStyleEvaluation.candidate_id == candidate_id)
            )
            if existing:
                return existing
            evaluation = CandidateStyleEvaluation(
                project_id=project_id,
                candidate_id=candidate_id,
                output_asset_id=output_asset_id,
                style_lock_id=style_lock.id,
                style_version_id=style_lock.style_version_id,
                style_embedding_id=style_lock.style_embedding_id,
                status=status,
                semantic_status=semantic_status,
                semantic_average_similarity=(
                    round(semantic_average_similarity, 6)
                    if semantic_average_similarity is not None
                    else None
                ),
                semantic_minimum_similarity=(
                    round(semantic_minimum_similarity, 6)
                    if semantic_minimum_similarity is not None
                    else None
                ),
                average_similarity=round(average, 6) if average is not None else None,
                minimum_similarity=round(minimum, 6) if minimum is not None else None,
                p10_similarity=round(p10, 6) if p10 is not None else None,
                drift_slope=round(slope, 6) if slope is not None else None,
                low_score_fraction=(round(low_fraction, 6) if low_fraction is not None else None),
                sample_positions=[round(value, 6) for value in positions],
                sample_scores=[round(value, 6) for value in scores],
                reason_codes=list(dict.fromkeys(reason_codes)),
                evaluator_version=self.evaluator_version,
                evidence_kind=(
                    "DETERMINISTIC_LOCAL" if semantic_status is None else "DETERMINISTIC_LOCAL+MODEL_SEMANTIC"
                ),
                metrics_json={
                    "embedding_model": embedding.model,
                    "embedding_hash": embedding.embedding_hash,
                    "deterministic_status": deterministic_status,
                    "semantic_embedding_model": (
                        semantic_embedding.model if semantic_embedding else None
                    ),
                    "semantic_sample_scores": [round(value, 6) for value in semantic_scores],
                    "thresholds": {
                        "average": style_lock.similarity_threshold,
                        "minimum": style_lock.minimum_similarity_threshold,
                        "drift_limit": style_lock.drift_limit,
                        "max_low_score_fraction": style_lock.max_low_score_fraction,
                        "semantic_average": semantic_threshold,
                    },
                },
            )
            session.add(evaluation)
            session.flush()
            return evaluation

    @staticmethod
    def assert_candidate_committable_in_session(
        session: Any,
        candidate: GenerationCandidate,
    ) -> None:
        shot = session.get(Shot, candidate.shot_id)
        project = shot.scene.episode.project if shot else None
        if project is None:
            raise StyleCommitViolation("candidate project is unavailable at the style commit gate")
        if not project.canonical_style_version_id:
            return
        style_lock = session.scalar(select(ProjectStyleLock).where(ProjectStyleLock.project_id == project.id))
        evaluation = session.scalar(
            select(CandidateStyleEvaluation).where(CandidateStyleEvaluation.candidate_id == candidate.id)
        )
        if not style_lock or not evaluation:
            raise StyleCommitViolation("locked project style has no candidate style evaluation")
        if (
            evaluation.status != "PASS"
            or evaluation.output_asset_id != candidate.output_asset_id
            or evaluation.style_lock_id != style_lock.id
            or evaluation.style_version_id != project.canonical_style_version_id
            or evaluation.style_embedding_id != style_lock.style_embedding_id
        ):
            raise StyleCommitViolation("candidate failed the locked-style similarity or drift gate")
