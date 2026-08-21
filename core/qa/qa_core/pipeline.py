from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Protocol

from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    CandidateStatus,
    DecisionRecord,
    GenerationCandidate,
    MediaAsset,
    QADecision,
    QAResult,
    Shot,
    User,
    new_id,
)
from sqlalchemy import update

from .evidence import (
    VLM_REVIEW_REQUIRED,
    CanonicalIdentityReference,
    CharacterEvidenceProducer,
    CharacterEvidenceReport,
)

TERMINAL_CANDIDATE_STATUSES = {
    CandidateStatus.COMMITTED.value,
    CandidateStatus.REJECTED.value,
}


class HumanReviewNotAllowed(RuntimeError):
    pass


@dataclass(frozen=True)
class IdentityDriftMetrics:
    minimum_similarity: float | None
    average_similarity: float | None
    p10_similarity: float | None
    drift_slope: float | None
    low_score_fraction: float
    recovery: float | None
    usable_samples: int
    invalid_samples: int = 0
    average_identity: float | None = None
    minimum_identity: float | None = None
    identity_p10: float | None = None
    appearance_similarity: float | None = None
    costume_similarity: float | None = None
    hair_similarity: float | None = None
    reacquisition_score: float | None = None


class DynamicIdentityQA(Protocol):
    """Interface for adaptive sampling/tracking implementations.

    A production tracker/VLM may implement this protocol later. The current
    rules implementation consumes already tracked, view-aware frame evidence.
    """

    def sample_positions(
        self,
        *,
        duration_seconds: float | None = None,
        motion_spikes: tuple[float, ...] = (),
    ) -> tuple[float, ...]: ...

    def evaluate(self, samples: list[dict[str, Any]]) -> IdentityDriftMetrics: ...


def _bounded_similarity(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("identity similarity must be a JSON number")
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("identity similarity must be finite and between zero and one")
    return score


def _frame_identity_score(sample: dict[str, Any]) -> float | None:
    if not isinstance(sample, dict):
        raise ValueError("identity samples must be objects")

    face = sample.get("face_similarity")
    if face is not None:
        return _bounded_similarity(face)
    fallbacks = [
        sample.get("body_similarity"),
        sample.get("hair_similarity"),
        sample.get("costume_similarity"),
        sample.get("tracking_continuity"),
    ]
    values = [_bounded_similarity(value) for value in fallbacks if value is not None]
    return mean(values) if values else None


def _mean_component(samples: list[dict[str, Any]], names: tuple[str, ...]) -> float | None:
    values: list[float] = []
    for sample in samples:
        for name in names:
            raw_value = sample.get(name)
            if raw_value is not None:
                values.append(_bounded_similarity(raw_value))
                break
    return round(mean(values), 4) if values else None


def analyze_identity_drift(
    samples: list[dict[str, Any]], low_threshold: float = 0.72
) -> IdentityDriftMetrics:
    scores: list[float] = []
    invalid_samples = 0
    for sample in samples:
        try:
            score = _frame_identity_score(sample)
        except (TypeError, ValueError):
            invalid_samples += 1
            continue
        if score is not None:
            scores.append(score)
    if not scores:
        return IdentityDriftMetrics(None, None, None, None, 0.0, None, 0, invalid_samples)
    ordered = sorted(scores)
    p10_index = max(0, math.ceil(len(ordered) * 0.1) - 1)
    x_mean = (len(scores) - 1) / 2
    denominator = sum((index - x_mean) ** 2 for index in range(len(scores)))
    slope = (
        sum((index - x_mean) * (score - mean(scores)) for index, score in enumerate(scores)) / denominator
        if denominator
        else 0.0
    )
    recovery = scores[-1] - min(scores) if scores.index(min(scores)) < len(scores) - 1 else 0.0
    minimum = round(min(scores), 4)
    average = round(mean(scores), 4)
    p10 = round(ordered[p10_index], 4)
    minimum_index = scores.index(min(scores))
    reacquired = scores[minimum_index + 1 :]
    reacquisition_score = round(mean(reacquired), 4) if reacquired else round(scores[-1], 4)
    try:
        appearance = _mean_component(samples, ("appearance_similarity", "body_similarity"))
        costume = _mean_component(samples, ("costume_similarity",))
        hair = _mean_component(samples, ("hair_similarity",))
    except (TypeError, ValueError):
        invalid_samples += 1
        appearance = costume = hair = None
    return IdentityDriftMetrics(
        minimum,
        average,
        p10,
        round(slope, 4),
        round(sum(score < low_threshold for score in scores) / len(scores), 4),
        round(recovery, 4),
        len(scores),
        invalid_samples,
        average_identity=average,
        minimum_identity=minimum,
        identity_p10=p10,
        appearance_similarity=appearance,
        costume_similarity=costume,
        hair_similarity=hair,
        reacquisition_score=reacquisition_score,
    )


class RuleBasedDynamicIdentityQA:
    version = "dynamic-identity-qa-v1"

    def sample_positions(
        self,
        *,
        duration_seconds: float | None = None,
        motion_spikes: tuple[float, ...] = (),
    ) -> tuple[float, ...]:
        del duration_seconds
        base = {0.0, 0.2, 0.4, 0.6, 0.8, 0.98}
        base.update(max(0.0, min(0.98, float(value))) for value in motion_spikes)
        return tuple(sorted(base))

    def evaluate(self, samples: list[dict[str, Any]]) -> IdentityDriftMetrics:
        return analyze_identity_drift(samples)


class QAPipeline:
    """Cascaded metadata/file QA and lightweight evidence-based visual QA."""

    profile_weights = {
        "CLOSE_UP_CHARACTER": {
            "character": 0.45,
            "composition": 0.15,
            "camera": 0.10,
            "action": 0.10,
            "scene": 0.05,
            "lighting": 0.10,
            "narrative": 0.05,
        },
        "DIALOGUE": {
            "character": 0.30,
            "scene": 0.10,
            "composition": 0.10,
            "action": 0.15,
            "camera": 0.10,
            "lighting": 0.10,
            "narrative": 0.15,
        },
        "ACTION": {
            "character": 0.20,
            "scene": 0.05,
            "composition": 0.10,
            "action": 0.30,
            "camera": 0.15,
            "lighting": 0.05,
            "narrative": 0.15,
        },
        "ESTABLISHING": {
            "character": 0.05,
            "scene": 0.30,
            "composition": 0.20,
            "action": 0.05,
            "camera": 0.15,
            "lighting": 0.15,
            "narrative": 0.10,
        },
        "COMMERCIAL_BEAUTY": {
            "character": 0.20,
            "scene": 0.10,
            "composition": 0.20,
            "action": 0.05,
            "camera": 0.10,
            "lighting": 0.25,
            "narrative": 0.10,
        },
    }

    def __init__(
        self,
        database: Database,
        identity_qa: DynamicIdentityQA | None = None,
        evidence_producer: CharacterEvidenceProducer | None = None,
    ):
        self.database = database
        self.identity_qa = identity_qa or RuleBasedDynamicIdentityQA()
        self.evidence_producer = evidence_producer

    @staticmethod
    def adaptive_sample_positions() -> tuple[float, ...]:
        """Backward-compatible default V1 sampling positions."""

        return RuleBasedDynamicIdentityQA().sample_positions()

    def identity_sample_positions(self) -> tuple[float, ...]:
        return self.identity_qa.sample_positions()

    def produce_character_evidence(
        self,
        candidate_id: str,
        *,
        character_id: str,
        references: Sequence[CanonicalIdentityReference],
        profile: str = "DIALOGUE",
        sample_positions: tuple[float, ...] | None = None,
    ) -> CharacterEvidenceReport:
        """Run the configured local evidence stack against a candidate video."""

        if self.evidence_producer is None:
            raise RuntimeError("CharacterEvidenceProducer is not configured")
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if candidate is None or candidate.output_asset_id is None:
                raise LookupError("candidate output is not available")
            asset = session.get(MediaAsset, candidate.output_asset_id)
            if asset is None or not asset.local_path:
                raise LookupError("candidate output has no local evidence file")
            video_path = Path(asset.local_path)
        return self.evidence_producer.produce(
            video_path,
            candidate_id=candidate_id,
            character_id=character_id,
            references=references,
            shot_type=profile,
            sample_positions=sample_positions,
        )

    def validate_candidate_with_character_evidence(
        self,
        candidate_id: str,
        *,
        character_id: str,
        references: Sequence[CanonicalIdentityReference],
        semantic_evidence: dict[str, Any] | None = None,
        profile: str = "DIALOGUE",
        sample_positions: tuple[float, ...] | None = None,
        defer_pass: bool = False,
    ) -> QAResult:
        report = self.produce_character_evidence(
            candidate_id,
            character_id=character_id,
            references=references,
            profile=profile,
            sample_positions=sample_positions,
        )
        return self.validate_candidate(
            candidate_id,
            semantic_evidence,
            profile=profile,
            defer_pass=defer_pass,
            character_evidence=report,
        )

    @staticmethod
    def _character_identity_metrics(report: CharacterEvidenceReport) -> IdentityDriftMetrics:
        aggregate = report.aggregate
        observation_duration = (
            max(sample.sample_time for sample in report.samples)
            - min(sample.sample_time for sample in report.samples)
            if len(report.samples) >= 2
            else 0.0
        )
        low_fraction = (
            min(1.0, aggregate.low_score_duration / observation_duration) if observation_duration > 0 else 0.0
        )
        recovery = (
            max(0.0, aggregate.reacquisition_score - aggregate.minimum_identity)
            if aggregate.reacquisition_score is not None and aggregate.minimum_identity is not None
            else None
        )
        return IdentityDriftMetrics(
            minimum_similarity=aggregate.minimum_identity,
            average_similarity=aggregate.average_identity,
            p10_similarity=aggregate.identity_p10,
            drift_slope=aggregate.drift_slope,
            low_score_fraction=round(low_fraction, 4),
            recovery=round(recovery, 4) if recovery is not None else None,
            usable_samples=aggregate.usable_samples,
            invalid_samples=0,
            average_identity=aggregate.average_identity,
            minimum_identity=aggregate.minimum_identity,
            identity_p10=aggregate.identity_p10,
            appearance_similarity=aggregate.appearance_similarity,
            costume_similarity=None,
            hair_similarity=None,
            reacquisition_score=aggregate.reacquisition_score,
        )

    def approve_human_review(
        self,
        candidate_id: str,
        *,
        project_id: str,
        reviewer_user_id: str,
        reason: str,
        explicit_confirmation: bool,
    ) -> QAResult:
        """Promote only a review-required candidate to PASS with an append-only audit trail."""

        normalized_reason = reason.strip()
        if not explicit_confirmation:
            raise ValueError("explicit human confirmation is required")
        if not normalized_reason:
            raise ValueError("human review reason is required")
        if len(normalized_reason) > 2000:
            raise ValueError("human review reason is too long")
        with self.database.session() as session:
            reviewer = session.get(User, reviewer_user_id)
            if reviewer is None or reviewer.status != "ACTIVE":
                raise HumanReviewNotAllowed("human review requires an active authenticated user")
            candidate = session.get(GenerationCandidate, candidate_id)
            if candidate is None:
                raise LookupError("candidate not found")
            shot = session.get(Shot, candidate.shot_id)
            if shot is None or shot.scene.episode.project_id != project_id:
                raise LookupError("candidate does not belong to the review project")
            if candidate.status != CandidateStatus.USER_REVIEW_REQUIRED.value:
                raise HumanReviewNotAllowed("only candidates waiting for human review can be approved")
            previous = session.get(QAResult, candidate.qa_result_id) if candidate.qa_result_id else None
            if (
                previous is None
                or previous.decision != QADecision.USER_REVIEW_REQUIRED.value
                or previous.hard_failures
            ):
                raise HumanReviewNotAllowed("candidate does not have an eligible review result")

            review = QAResult(
                id=new_id(),
                candidate_id=candidate.id,
                profile="HUMAN_REVIEW",
                level_reached=max(previous.level_reached + 1, 2),
                decision=QADecision.PASS.value,
                overall_score=previous.overall_score,
                character_score=previous.character_score,
                scene_score=previous.scene_score,
                composition_score=previous.composition_score,
                action_score=previous.action_score,
                camera_score=previous.camera_score,
                lighting_score=previous.lighting_score,
                narrative_score=previous.narrative_score,
                hard_failures=[],
                metrics_json={
                    "review_type": "HUMAN_REVIEW",
                    "source": "USER_EXPLICIT_CONFIRMATION",
                    "reviewer_user_id": reviewer_user_id,
                    "reason": normalized_reason,
                    "explicit_confirmation": True,
                    "prior_qa_result_id": previous.id,
                    "prior_decision": previous.decision,
                    "prior_metrics": previous.metrics_json,
                },
                summary=f"人工确认通过：{normalized_reason}",
            )
            session.add(review)
            session.flush()
            approved = session.execute(
                update(GenerationCandidate)
                .where(
                    GenerationCandidate.id == candidate.id,
                    GenerationCandidate.status == CandidateStatus.USER_REVIEW_REQUIRED.value,
                    GenerationCandidate.qa_result_id == previous.id,
                )
                .values(
                    qa_result_id=review.id,
                    status=CandidateStatus.PASSED.value,
                )
                .execution_options(synchronize_session=False)
            )
            if affected_rows(approved) != 1:
                raise HumanReviewNotAllowed("candidate review state changed before approval")
            session.add(
                DecisionRecord(
                    project_id=project_id,
                    shot_id=shot.id,
                    decision_type="HUMAN_REVIEW",
                    input_features={
                        "candidate_id": candidate.id,
                        "reviewer_user_id": reviewer_user_id,
                        "reason": normalized_reason,
                        "source": "USER_EXPLICIT_CONFIRMATION",
                        "explicit_confirmation": True,
                        "prior_qa_result_id": previous.id,
                        "prior_decision": previous.decision,
                    },
                    selected_action="APPROVE_FOR_COMMIT",
                    reason_codes=["EXPLICIT_HUMAN_CONFIRMATION"],
                    model_version="human-review-v1",
                    policy_version="human-review-v1",
                )
            )
            session.flush()
            return review

    @staticmethod
    def _file_metrics(asset: MediaAsset) -> tuple[dict[str, Any], list[str]]:
        failures: list[str] = []
        path = Path(asset.local_path or "")
        metrics: dict[str, Any] = {"exists": path.is_file(), "mime_type": asset.mime_type}
        if not path.is_file() or path.stat().st_size == 0:
            return metrics, ["FILE_MISSING_OR_EMPTY"]
        metrics["size_bytes"] = path.stat().st_size
        if asset.mime_type.startswith("video/"):
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=width,height,avg_frame_rate",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                failures.append("VIDEO_DECODE_ERROR")
            else:
                probe = json.loads(result.stdout or "{}")
                metrics["probe"] = probe
                duration = float((probe.get("format") or {}).get("duration") or 0)
                if duration <= 0:
                    failures.append("INVALID_DURATION")
        return metrics, failures

    def validate_candidate(
        self,
        candidate_id: str,
        evidence: dict[str, Any] | None = None,
        *,
        profile: str = "DIALOGUE",
        defer_pass: bool = False,
        character_evidence: CharacterEvidenceReport | None = None,
    ) -> QAResult:
        evidence = dict(evidence or {})
        if character_evidence is not None:
            if character_evidence.candidate_id != candidate_id:
                raise ValueError("character evidence belongs to a different candidate")
            if "identity_samples" in evidence:
                raise ValueError("typed character evidence cannot be mixed with scalar identity samples")
            evidence["character_score"] = (
                character_evidence.aggregate.average_identity
                if character_evidence.aggregate.average_identity is not None
                else character_evidence.aggregate.appearance_similarity
            )
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate or not candidate.output_asset_id:
                raise LookupError("candidate output is not available")
            if candidate.status in TERMINAL_CANDIDATE_STATUSES:
                raise LookupError("committed or rejected candidates cannot be revalidated")
            asset = session.get(MediaAsset, candidate.output_asset_id)
            file_metrics, hard_failures = self._file_metrics(asset)
            reviewer_reason_codes = {
                "wrong_main_character": "WRONG_CHARACTER",
                "critical_identity_failure": "IDENTITY_DRIFT",
                "wrong_scene": "SCENE_DRIFT",
                "missing_required_character": "WRONG_CHARACTER",
                "severe_anatomy_failure": "ANATOMY_FAILURE",
                "camera_direction_mismatch": "CAMERA_DIRECTION_MISMATCH",
                "action_not_completed": "ACTION_NOT_COMPLETED",
                "wrong_prop": "WRONG_PROP",
                "low_end_frame_quality": "LOW_END_FRAME_QUALITY",
                "hair_drift": "HAIR_DRIFT",
                "costume_drift": "COSTUME_DRIFT",
            }
            for gate, reason_code in reviewer_reason_codes.items():
                if evidence.get(gate):
                    hard_failures.append(reason_code)
            identity = (
                self._character_identity_metrics(character_evidence)
                if character_evidence is not None
                else self.identity_qa.evaluate(evidence.get("identity_samples", []))
            )
            semantic_review_required = bool(
                character_evidence is not None and character_evidence.semantic_review_required
            )
            if identity.invalid_samples:
                hard_failures.append("INVALID_IDENTITY_EVIDENCE")
            if character_evidence is not None:
                threshold = character_evidence.threshold_profile
                # An uncertain track cannot support a hard identity judgment;
                # route it to semantic/VLM review instead of binding the wrong person.
                if not semantic_review_required:
                    if (
                        identity.minimum_similarity is not None
                        and identity.minimum_similarity < threshold.identity_hard_fail
                    ):
                        hard_failures.append("IDENTITY_MINIMUM_TOO_LOW")
                        hard_failures.append("IDENTITY_DRIFT")
                    if (
                        identity.drift_slope is not None
                        and identity.drift_slope <= -threshold.drift_limit
                        and identity.minimum_similarity is not None
                        and identity.minimum_similarity < threshold.identity_pass
                    ):
                        hard_failures.append("SUSTAINED_IDENTITY_DRIFT")
                        hard_failures.append("IDENTITY_DRIFT")
            else:
                threshold = None
                if identity.minimum_similarity is not None and identity.minimum_similarity < 0.62:
                    hard_failures.append("IDENTITY_MINIMUM_TOO_LOW")
                    hard_failures.append("IDENTITY_DRIFT")
                if (
                    identity.drift_slope is not None
                    and identity.drift_slope <= -0.045
                    and identity.minimum_similarity is not None
                    and identity.minimum_similarity < 0.72
                ):
                    hard_failures.append("SUSTAINED_IDENTITY_DRIFT")
                    hard_failures.append("IDENTITY_DRIFT")
            if (
                identity.average_identity is not None
                and identity.average_identity < 0.5
                and not semantic_review_required
            ):
                hard_failures.append("WRONG_CHARACTER")
            if identity.hair_similarity is not None and identity.hair_similarity < 0.65:
                hard_failures.append("HAIR_DRIFT")
            if identity.costume_similarity is not None and identity.costume_similarity < 0.65:
                hard_failures.append("COSTUME_DRIFT")
            dimensions = {
                key: evidence.get(f"{key}_score")
                for key in ["character", "scene", "composition", "action", "camera", "lighting", "narrative"]
            }
            weights = self.profile_weights.get(profile, self.profile_weights["DIALOGUE"])
            available: dict[str, float] = {}
            invalid_dimensions: list[str] = []
            for key, value in dimensions.items():
                if value is None:
                    continue
                try:
                    score = float(value)
                except (TypeError, ValueError):
                    invalid_dimensions.append(key)
                    continue
                if not math.isfinite(score) or not 0 <= score <= 1:
                    invalid_dimensions.append(key)
                    continue
                available[key] = score
            if invalid_dimensions:
                hard_failures.append("INVALID_QA_SCORE")
            required_dimensions = {key for key, weight in weights.items() if weight > 0}
            missing_dimensions = sorted(required_dimensions - available.keys())
            identity_not_applicable = bool(
                character_evidence is None
                and evidence.get("identity_not_applicable")
                and evidence.get("_trusted_source") == "INTERNAL_QC"
            )
            sample_positions = (
                tuple(sample.sample_time for sample in character_evidence.samples)
                if character_evidence is not None
                else self.identity_sample_positions()
            )
            minimum_required_samples = (
                character_evidence.threshold_profile.minimum_required_samples
                if character_evidence is not None
                else len(sample_positions)
            )
            identity_complete = identity_not_applicable or (
                identity.usable_samples >= minimum_required_samples and not semantic_review_required
            )
            evidence_complete = not missing_dimensions and identity_complete
            weight_sum = sum(weights[key] for key in available)
            overall = (
                sum(available[key] * weights[key] for key in available) / weight_sum if weight_sum else 0.0
            )
            if hard_failures:
                decision = QADecision.HARD_FAIL.value
            elif semantic_review_required:
                decision = QADecision.USER_REVIEW_REQUIRED.value
            elif not evidence_complete:
                decision = QADecision.USER_REVIEW_REQUIRED.value
            elif character_evidence is not None and (
                overall >= 0.78
                and identity.average_identity is not None
                and identity.average_identity >= character_evidence.threshold_profile.identity_pass
                and (identity.minimum_similarity or 1.0)
                >= character_evidence.threshold_profile.identity_hard_fail
            ):
                decision = QADecision.PASS.value
            elif (
                character_evidence is None
                and overall >= 0.78
                and (identity.minimum_similarity or 1.0) >= 0.72
            ):
                decision = QADecision.PASS.value
            elif overall >= 0.62:
                decision = QADecision.SOFT_FAIL.value
            else:
                decision = QADecision.HARD_FAIL.value
            status_map = {
                QADecision.PASS.value: CandidateStatus.PASSED.value,
                QADecision.SOFT_FAIL.value: CandidateStatus.SOFT_FAILED.value,
                QADecision.HARD_FAIL.value: CandidateStatus.HARD_FAILED.value,
                QADecision.USER_REVIEW_REQUIRED.value: CandidateStatus.USER_REVIEW_REQUIRED.value,
            }
            result = QAResult(
                id=new_id(),
                candidate_id=candidate.id,
                profile=profile,
                level_reached=1,
                decision=decision,
                overall_score=round(overall, 4),
                character_score=available.get("character"),
                scene_score=available.get("scene"),
                composition_score=available.get("composition"),
                action_score=available.get("action"),
                camera_score=available.get("camera"),
                lighting_score=available.get("lighting"),
                narrative_score=available.get("narrative"),
                hard_failures=sorted(set(hard_failures)),
                metrics_json={
                    "level0": file_metrics,
                    "identity": asdict(identity),
                    "adaptive_samples": sample_positions,
                    "evidence_source": (
                        "CHARACTER_EVIDENCE_PRODUCER_V1"
                        if character_evidence is not None
                        else evidence.get("_trusted_source", "UNTRUSTED_OR_NONE")
                    ),
                    "evidence_complete": evidence_complete,
                    "missing_dimensions": missing_dimensions,
                    "identity_not_applicable": identity_not_applicable,
                    "minimum_identity_samples": minimum_required_samples,
                    "semantic_review_required": semantic_review_required,
                    "semantic_review_reason": (VLM_REVIEW_REQUIRED if semantic_review_required else None),
                    "character_evidence": (
                        character_evidence.to_dict() if character_evidence is not None else None
                    ),
                },
                summary=(
                    f"{decision}: {', '.join(sorted(set(hard_failures))) or 'weighted profile decision'}"
                    if evidence_complete or hard_failures
                    else f"{decision}: incomplete QA evidence"
                ),
            )
            session.add(result)
            next_status = (
                CandidateStatus.VALIDATING.value
                if defer_pass and decision == QADecision.PASS.value
                else status_map[decision]
            )
            updated = session.execute(
                update(GenerationCandidate)
                .where(
                    GenerationCandidate.id == candidate.id,
                    GenerationCandidate.status.not_in(TERMINAL_CANDIDATE_STATUSES),
                )
                .values(qa_result_id=result.id, status=next_status)
                .execution_options(synchronize_session=False)
            )
            if affected_rows(updated) != 1:
                raise LookupError("candidate became terminal before QA could be saved")
            session.refresh(candidate)
            candidate.metadata_json = {
                **candidate.metadata_json,
                "qa_decision": decision,
                **(
                    {"character_evidence_run_id": character_evidence.producer_run_id}
                    if character_evidence is not None
                    else {}
                ),
            }
            session.flush()
            return result
