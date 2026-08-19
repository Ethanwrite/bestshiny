from __future__ import annotations

import json
import math
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from platform_database import Database
from production_domain.models import (
    CandidateStatus,
    GenerationCandidate,
    MediaAsset,
    QADecision,
    QAResult,
)


@dataclass(frozen=True)
class IdentityDriftMetrics:
    minimum_similarity: float | None
    average_similarity: float | None
    p10_similarity: float | None
    drift_slope: float | None
    low_score_fraction: float
    recovery: float | None
    usable_samples: int


def _frame_identity_score(sample: dict[str, Any]) -> float | None:
    face = sample.get("face_similarity")
    if face is not None:
        return float(face)
    fallbacks = [
        sample.get("body_similarity"),
        sample.get("hair_similarity"),
        sample.get("costume_similarity"),
        sample.get("tracking_continuity"),
    ]
    values = [float(value) for value in fallbacks if value is not None]
    return mean(values) if values else None


def analyze_identity_drift(
    samples: list[dict[str, Any]], low_threshold: float = 0.72
) -> IdentityDriftMetrics:
    scores = [score for sample in samples if (score := _frame_identity_score(sample)) is not None]
    if not scores:
        return IdentityDriftMetrics(None, None, None, None, 0.0, None, 0)
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
    return IdentityDriftMetrics(
        round(min(scores), 4),
        round(mean(scores), 4),
        round(ordered[p10_index], 4),
        round(slope, 4),
        round(sum(score < low_threshold for score in scores) / len(scores), 4),
        round(recovery, 4),
        len(scores),
    )


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

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def adaptive_sample_positions() -> tuple[float, ...]:
        return (0.0, 0.2, 0.4, 0.6, 0.8, 0.98)

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
    ) -> QAResult:
        evidence = evidence or {}
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate or not candidate.output_asset_id:
                raise LookupError("candidate output is not available")
            asset = session.get(MediaAsset, candidate.output_asset_id)
            file_metrics, hard_failures = self._file_metrics(asset)
            for gate in [
                "wrong_main_character",
                "critical_identity_failure",
                "wrong_scene",
                "missing_required_character",
                "severe_anatomy_failure",
            ]:
                if evidence.get(gate):
                    hard_failures.append(gate.upper())
            identity = analyze_identity_drift(evidence.get("identity_samples", []))
            if identity.minimum_similarity is not None and identity.minimum_similarity < 0.62:
                hard_failures.append("IDENTITY_MINIMUM_TOO_LOW")
            if (
                identity.drift_slope is not None
                and identity.drift_slope <= -0.045
                and identity.minimum_similarity is not None
                and identity.minimum_similarity < 0.72
            ):
                hard_failures.append("SUSTAINED_IDENTITY_DRIFT")
            dimensions = {
                key: evidence.get(f"{key}_score")
                for key in ["character", "scene", "composition", "action", "camera", "lighting", "narrative"]
            }
            weights = self.profile_weights.get(profile, self.profile_weights["DIALOGUE"])
            available = {key: float(value) for key, value in dimensions.items() if value is not None}
            weight_sum = sum(weights[key] for key in available)
            overall = (
                sum(available[key] * weights[key] for key in available) / weight_sum if weight_sum else 0.0
            )
            if hard_failures:
                decision = QADecision.HARD_FAIL.value
            elif not available and identity.usable_samples == 0:
                decision = QADecision.USER_REVIEW_REQUIRED.value
            elif overall >= 0.78 and (identity.minimum_similarity or 1.0) >= 0.72:
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
                candidate_id=candidate.id,
                profile=profile,
                level_reached=1,
                decision=decision,
                overall_score=round(overall, 4),
                character_score=dimensions["character"],
                scene_score=dimensions["scene"],
                composition_score=dimensions["composition"],
                action_score=dimensions["action"],
                camera_score=dimensions["camera"],
                lighting_score=dimensions["lighting"],
                narrative_score=dimensions["narrative"],
                hard_failures=sorted(set(hard_failures)),
                metrics_json={
                    "level0": file_metrics,
                    "identity": asdict(identity),
                    "adaptive_samples": self.adaptive_sample_positions(),
                },
                summary=f"{decision}: {', '.join(sorted(set(hard_failures))) or 'weighted profile decision'}",
            )
            session.add(result)
            session.flush()
            candidate.qa_result_id = result.id
            candidate.status = status_map[decision]
            candidate.metadata_json = {**candidate.metadata_json, "qa_decision": decision}
            session.flush()
            return result
