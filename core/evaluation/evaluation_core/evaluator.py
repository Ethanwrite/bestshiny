from __future__ import annotations

import math
from typing import Protocol

from platform_database import Database
from production_domain.models import (
    EvaluationResult as EvaluationResultRecord,
)
from production_domain.models import (
    GenerationJob,
    MediaAsset,
    Project,
    Shot,
)

from .schemas import (
    CHECK_NAMES,
    EvaluationDecision,
    EvaluationEvidence,
    EvaluationExpectation,
    EvaluationResult,
)


class VisualJudge(Protocol):
    def inspect(self, expectation: EvaluationExpectation) -> EvaluationEvidence: ...


class EvidenceUnavailable(RuntimeError):
    pass


class NoopVisualJudge:
    """Honest fallback: it never manufactures visual evidence or a passing score."""

    def inspect(self, expectation: EvaluationExpectation) -> EvaluationEvidence:
        del expectation
        return EvaluationEvidence(
            evidence_complete=False,
            judge_provider="none",
            detected_failures=["visual_evidence_unavailable"],
        )


class GenerationEvaluator:
    version = "generation-evaluator-v1"
    critical_failures = frozenset(
        {
            "wrong_character",
            "wrong_costume",
            "missing_key_prop",
            "direct_camera_gaze",
            "wrong_screen_direction",
            "wrong_scene",
        }
    )

    patch_lines = {
        "direct_camera_gaze": (
            "Maintain profile orientation throughout the final second. Eye line remains toward the "
            "approved scene target. The subject never acknowledges the camera. Final frame preserves "
            "the approved rear three-quarter composition."
        ),
        "wrong_character": "Re-anchor every subject to the canonical identity reference before motion.",
        "wrong_costume": "Lock wardrobe to the canonical wardrobe version; do not alter color or silhouette.",
        "missing_key_prop": "Keep every required key prop visible and in the specified hand or position.",
        "wrong_screen_direction": "Preserve the established screen axis and movement direction throughout.",
        "wrong_scene": "Use only the canonical scene layout, architecture, and background objects.",
        "identity": "Strengthen canonical face, hair, body silhouette, and profile references.",
        "eyeline": "Maintain each subject's explicit eyeline target; never shift toward the lens.",
        "motion": "Reduce the shot to the single approved physical action and trajectory.",
    }

    def __init__(self, database: Database | None = None, judge: VisualJudge | None = None):
        self.database = database
        self.judge = judge or NoopVisualJudge()

    def evaluate(
        self,
        expectation: EvaluationExpectation,
        evidence: EvaluationEvidence | None = None,
        *,
        attempt_number: int = 0,
        model_id: str = "",
        provider: str = "",
    ) -> EvaluationResult:
        evidence = evidence or self.judge.inspect(expectation)
        normalized_scores: dict[str, float] = {}
        invalid_scores: list[str] = []
        for name in CHECK_NAMES:
            if name not in evidence.scores:
                continue
            raw_score = evidence.scores[name]
            if isinstance(raw_score, bool):
                invalid_scores.append(name)
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                invalid_scores.append(name)
                continue
            if not math.isfinite(score) or not 0 <= score <= 1:
                invalid_scores.append(name)
                continue
            normalized_scores[name] = score
        failures = list(dict.fromkeys(evidence.detected_failures))
        missing_checks = [name for name in CHECK_NAMES if name not in normalized_scores]
        evidence_complete = evidence.evidence_complete and not missing_checks and not invalid_scores
        if invalid_scores:
            failures.append("visual_evidence_invalid")
        if missing_checks:
            failures.append("visual_evidence_incomplete")
        observations = evidence.observations
        if expectation.forbid_camera_gaze and observations.get("direct_camera_gaze") is True:
            failures.append("direct_camera_gaze")
        missing_props = set(expectation.required_props).difference(observations.get("visible_props", []))
        if missing_props:
            failures.append("missing_key_prop")
        for name, score in normalized_scores.items():
            if score < expectation.minimum_check_score:
                failures.append(name)
        failures = list(dict.fromkeys(failures))
        critical = bool(self.critical_failures.intersection(failures))

        if not evidence_complete:
            decision = EvaluationDecision.REJECT
            failures = list(dict.fromkeys([*failures, "visual_evidence_unavailable"]))
            overall = 0.0
        else:
            overall = sum(normalized_scores.values()) / len(normalized_scores) if normalized_scores else 0.0
            if critical:
                decision = EvaluationDecision.RETRY_REWRITE_PROMPT
            elif overall >= expectation.threshold and not failures:
                decision = EvaluationDecision.ACCEPT
            elif attempt_number >= 1 and any(item in failures for item in ("motion", "camera", "physics")):
                decision = EvaluationDecision.SWITCH_MODEL
            else:
                decision = EvaluationDecision.RETRY_SAME_MODEL

        patch = " ".join(self.patch_lines[item] for item in failures if item in self.patch_lines)
        checks = {
            name: {
                "score": normalized_scores.get(name),
                "passed": normalized_scores.get(name, 0.0) >= expectation.minimum_check_score,
                "critical": name in self.critical_failures,
            }
            for name in CHECK_NAMES
        }
        result = EvaluationResult(
            decision=decision,
            overall_score=round(overall, 6),
            critical_failure=critical,
            scores=normalized_scores,
            checks=checks,
            retry_reasons=failures,
            retry_patch=patch,
            evidence_complete=evidence_complete,
            evaluator_version=self.version,
            judge_provider=evidence.judge_provider,
            judge_model=evidence.judge_model,
        )
        if self.database:
            self._persist(expectation, result, attempt_number, model_id, provider)
        return result

    def _persist(
        self,
        expectation: EvaluationExpectation,
        result: EvaluationResult,
        attempt_number: int,
        model_id: str,
        provider: str,
    ) -> None:
        assert self.database is not None
        with self.database.session() as session:
            if not session.get(Project, expectation.project_id):
                raise LookupError("evaluation project not found")
            if expectation.shot_id:
                shot = session.get(Shot, expectation.shot_id)
                if not shot or shot.scene.episode.project_id != expectation.project_id:
                    raise ValueError("evaluation shot does not belong to project")
            if expectation.generation_id:
                job = session.get(GenerationJob, expectation.generation_id)
                if not job or job.project_id != expectation.project_id:
                    raise ValueError("evaluation generation does not belong to project")
            related_asset_ids = list(
                dict.fromkeys(
                    asset_id
                    for asset_id in (
                        expectation.previous_frame_asset_id,
                        expectation.generated_asset_id,
                        *expectation.canonical_reference_asset_ids,
                    )
                    if asset_id
                )
            )
            for asset_id in related_asset_ids:
                asset = session.get(MediaAsset, asset_id)
                if not asset or asset.project_id != expectation.project_id:
                    raise ValueError("evaluation media asset does not belong to project")
            session.add(
                EvaluationResultRecord(
                    project_id=expectation.project_id,
                    shot_id=expectation.shot_id,
                    generation_job_id=expectation.generation_id,
                    generated_asset_id=expectation.generated_asset_id,
                    decision=result.decision.value,
                    overall_score=result.overall_score,
                    critical_failure=result.critical_failure,
                    scores_json=result.scores,
                    checks_json=result.checks,
                    retry_reasons=result.retry_reasons,
                    retry_patch=result.retry_patch,
                    evidence_complete=result.evidence_complete,
                    evaluator_version=result.evaluator_version,
                    judge_provider=result.judge_provider,
                    judge_model=result.judge_model,
                    attempt_number=attempt_number,
                    model_id=model_id,
                    provider=provider,
                )
            )
