from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Protocol

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
from pydantic import ValidationError

from .schemas import (
    CHECK_NAMES,
    EvaluationDecision,
    EvaluationEvidence,
    EvaluationExpectation,
    EvaluationResult,
    StatePathObservation,
)

_MISSING = object()
_IDENTITY_PATH_SEGMENTS = frozenset(
    {
        "canonical_asset_id",
        "canonical_hair",
        "canonical_identity",
        "canonical_identity_id",
        "canonical_outfit",
        "body_proportions",
        "face_embedding",
        "face_identity",
        "identity",
        "identity_embedding",
        "identity_embedding_id",
        "identity_fingerprint",
        "identity_version",
        "identity_version_id",
    }
)
_IDENTITY_PATH_PREFIXES = (
    ("appearance", "face"),
    ("appearance", "hair"),
    ("appearance", "body"),
    ("appearance", "body_proportions"),
    ("appearance", "canonical_hair"),
    ("appearance", "canonical_outfit"),
    ("appearance", "outfit", "type"),
    ("appearance", "outfit", "design"),
    ("appearance", "outfit", "color"),
)


@dataclass(frozen=True)
class _StateRequirement:
    path: str
    expected_value: Any
    operator: Literal["EQUALS", "EXISTS"]
    minimum_confidence: float
    severity: str
    reason_code: str
    tolerance: float | None = None


@dataclass(frozen=True)
class _StateValidation:
    decision: Literal["NOT_REQUIRED", "PASS", "REVIEW", "REJECT"]
    failures: tuple[str, ...]
    critical: bool
    details: tuple[dict[str, Any], ...]
    invalid_expectations: tuple[str, ...] = ()


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
    version = "generation-evaluator-v2-state-evidence"
    critical_failures = frozenset(
        {
            "wrong_character",
            "wrong_costume",
            "missing_key_prop",
            "direct_camera_gaze",
            "wrong_screen_direction",
            "wrong_scene",
            "state_identity_mismatch",
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
        "state_identity_mismatch": (
            "Re-anchor the subject to the locked canonical identity. Never change an identity path."
        ),
        "state_continuity_mismatch": (
            "Preserve every required narrative-state value and continuity constraint in the final frame."
        ),
    }

    @staticmethod
    def _path_parts(path: str) -> tuple[str, ...]:
        normalized = path.strip()
        if normalized.startswith("/"):
            return tuple(
                part.replace("~1", "/").replace("~0", "~")
                for part in normalized.removeprefix("/").split("/")
                if part
            )
        return tuple(part for part in normalized.split(".") if part)

    @classmethod
    def _state_value(cls, state: Any, path: str) -> Any:
        current = state
        for part in cls._path_parts(path):
            if isinstance(current, dict):
                if part not in current:
                    return _MISSING
                current = current[part]
                continue
            if isinstance(current, list):
                try:
                    index = int(part)
                except ValueError:
                    return _MISSING
                if index < 0 or index >= len(current):
                    return _MISSING
                current = current[index]
                continue
            return _MISSING
        return current

    @classmethod
    def _identity_path(cls, path: str) -> bool:
        parts = tuple(part.casefold().replace("-", "_") for part in cls._path_parts(path))
        if any(
            part in _IDENTITY_PATH_SEGMENTS or part.startswith("identity_") or part.endswith("_identity_id")
            for part in parts
        ):
            return True
        return any(
            parts[index : index + len(prefix)] == prefix
            for prefix in _IDENTITY_PATH_PREFIXES
            for index in range(len(parts) - len(prefix) + 1)
        )

    @staticmethod
    def _expected_value(raw: dict[str, Any]) -> Any:
        for key in ("expected_value", "expected", "equals", "value"):
            if key in raw:
                return raw[key]
        return _MISSING

    @classmethod
    def _parse_requirement(
        cls,
        raw: Any,
        expected_state: dict[str, Any],
        *,
        default_severity: str,
    ) -> _StateRequirement:
        if isinstance(raw, str):
            path = raw.strip()
            specification: dict[str, Any] = {}
        elif isinstance(raw, dict):
            path = str(raw.get("path") or "").strip()
            specification = raw
        else:
            raise ValueError("state requirement must be a path string or object")
        if not path:
            raise ValueError("state requirement path is required")

        raw_operator = (
            str(specification.get("operator") or specification.get("rule") or "EQUALS").strip().upper()
        )
        if raw_operator in {"=", "==", "EQ", "EQUAL", "EQUALS", "MUST_EQUAL"}:
            operator: Literal["EQUALS", "EXISTS"] = "EQUALS"
        elif raw_operator in {"EXISTS", "MUST_EXIST"}:
            operator = "EXISTS"
        elif raw_operator == "LOCK_UNTIL_SCENE":
            # State-policy validation decides whether the lock is still active.
            # Here we validate the explicit expected target rendered for this shot.
            operator = "EQUALS"
        else:
            raise ValueError(f"state requirement {path!r} uses unsupported operator {raw_operator!r}")
        expected_value = cls._expected_value(specification)
        state_value = cls._state_value(expected_state, path)
        if raw_operator == "LOCK_UNTIL_SCENE" and state_value is not _MISSING:
            expected_value = state_value
        elif expected_value is _MISSING:
            expected_value = state_value
        if operator == "EQUALS" and expected_value is _MISSING:
            raise ValueError(f"state requirement {path!r} has no expected value")
        if operator == "EXISTS":
            expected_value = None

        raw_confidence = specification.get(
            "minimum_confidence",
            specification.get("confidence_threshold", 0.75),
        )
        if isinstance(raw_confidence, bool) or not isinstance(raw_confidence, (int, float)):
            raise ValueError(f"state requirement {path!r} has invalid minimum confidence")
        minimum_confidence = float(raw_confidence)
        if not math.isfinite(minimum_confidence) or not 0 <= minimum_confidence <= 1:
            raise ValueError(f"state requirement {path!r} has invalid minimum confidence")

        severity = str(specification.get("severity") or default_severity).strip().upper()
        if severity not in {"REVIEW", "REJECT", "HARD"}:
            raise ValueError(f"state requirement {path!r} has invalid severity")
        raw_tolerance = specification.get("tolerance")
        tolerance: float | None = None
        if raw_tolerance is not None:
            if isinstance(raw_tolerance, bool) or not isinstance(raw_tolerance, (int, float)):
                raise ValueError(f"state requirement {path!r} has invalid tolerance")
            tolerance = float(raw_tolerance)
            if not math.isfinite(tolerance) or tolerance < 0:
                raise ValueError(f"state requirement {path!r} has invalid tolerance")
        reason_code = (
            str(specification.get("reason_code") or specification.get("id") or "").strip().casefold()
        )
        return _StateRequirement(
            path=path,
            expected_value=expected_value,
            operator=operator,
            minimum_confidence=minimum_confidence,
            severity=severity,
            reason_code=reason_code,
            tolerance=tolerance,
        )

    @staticmethod
    def _requirement_items(raw: Any, name: str) -> list[Any]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            if "path" in raw:
                return [raw]
            return [
                (
                    {**expected, "path": path}
                    if isinstance(expected, dict)
                    else {"path": path, "expected_value": expected}
                )
                for path, expected in raw.items()
            ]
        raise ValueError(f"{name} must be a list or path-keyed object")

    @classmethod
    def _state_requirements(
        cls,
        expected_state: dict[str, Any],
    ) -> tuple[tuple[_StateRequirement, ...], tuple[str, ...]]:
        validation = expected_state.get("_validation")
        validation = validation if isinstance(validation, dict) else {}
        raw_required = [
            *cls._requirement_items(
                expected_state.get("required_state_paths", validation.get("required_state_paths")),
                "required_state_paths",
            ),
        ]
        raw_continuity = [
            *cls._requirement_items(
                expected_state.get(
                    "continuity_constraints",
                    validation.get("continuity_constraints"),
                ),
                "continuity_constraints",
            ),
        ]
        requirements: dict[str, _StateRequirement] = {}
        invalid: list[str] = []
        for raw, default_severity in [
            *((item, "REJECT") for item in raw_required),
            *((item, "REJECT") for item in raw_continuity),
        ]:
            if isinstance(raw, dict) and raw.get("evidence_required") is False:
                continue
            try:
                requirement = cls._parse_requirement(
                    raw,
                    expected_state,
                    default_severity=default_severity,
                )
            except ValueError as exc:
                invalid.append(str(exc))
                continue
            requirements[requirement.path] = requirement
        return tuple(requirements.values()), tuple(invalid)

    @staticmethod
    def _legacy_state_observations(raw: Any) -> tuple[StatePathObservation, ...]:
        if raw is None:
            return ()
        try:
            parsed = EvaluationEvidence.model_validate({"state_observations": raw})
        except ValidationError:
            return ()
        return tuple(parsed.state_observations)

    @staticmethod
    def _values_match(expected: Any, observed: Any, tolerance: float | None) -> bool:
        if (
            tolerance is not None
            and not isinstance(expected, bool)
            and not isinstance(observed, bool)
            and isinstance(expected, (int, float))
            and isinstance(observed, (int, float))
        ):
            return abs(float(expected) - float(observed)) <= tolerance
        return expected == observed

    @classmethod
    def _validate_expected_state(
        cls,
        expectation: EvaluationExpectation,
        evidence: EvaluationEvidence,
    ) -> _StateValidation:
        try:
            requirements, invalid_expectations = cls._state_requirements(expectation.expected_state)
        except ValueError as exc:
            requirements, invalid_expectations = (), (str(exc),)
        if not requirements and not invalid_expectations:
            return _StateValidation("NOT_REQUIRED", (), False, ())

        typed_observations = tuple(evidence.state_observations)
        legacy_payload = evidence.observations.get("state_observations")
        if legacy_payload is None:
            legacy_payload = evidence.observations.get("state_paths")
        observations = typed_observations or cls._legacy_state_observations(legacy_payload)
        by_path = {cls._path_parts(item.path): item for item in observations}
        details: list[dict[str, Any]] = []
        failures: list[str] = []
        review_required = bool(invalid_expectations)
        rejected = False
        critical = False
        if invalid_expectations:
            failures.append("state_expectation_invalid")

        for requirement in requirements:
            observation = by_path.get(cls._path_parts(requirement.path))
            identity_path = cls._identity_path(requirement.path)
            detail: dict[str, Any] = {
                "path": requirement.path,
                "operator": requirement.operator,
                "minimum_confidence": requirement.minimum_confidence,
                "severity": requirement.severity,
                "identity_path": identity_path,
            }
            if requirement.operator == "EQUALS":
                detail["expected"] = requirement.expected_value
            if observation is None:
                review_required = True
                failures.append("state_evidence_review_required")
                detail.update({"status": "REVIEW", "reason": "MISSING_OBSERVATION"})
                details.append(detail)
                continue
            detail.update(
                {
                    "observed": observation.value,
                    "confidence": observation.confidence,
                    "observable": observation.observable,
                    "source": observation.source,
                }
            )
            if not observation.observable or observation.confidence < requirement.minimum_confidence:
                review_required = True
                failures.append("state_evidence_review_required")
                detail.update(
                    {
                        "status": "REVIEW",
                        "reason": ("NOT_OBSERVABLE" if not observation.observable else "LOW_CONFIDENCE"),
                    }
                )
                details.append(detail)
                continue
            if requirement.operator == "EXISTS" or cls._values_match(
                requirement.expected_value, observation.value, requirement.tolerance
            ):
                detail.update({"status": "PASS", "reason": "MATCH"})
                details.append(detail)
                continue

            reason = "state_identity_mismatch" if identity_path else "state_continuity_mismatch"
            failures.append(reason)
            if requirement.reason_code:
                failures.append(requirement.reason_code)
            if identity_path or requirement.severity in {"REJECT", "HARD"}:
                rejected = True
                critical = critical or identity_path or requirement.severity == "HARD"
                detail.update({"status": "REJECT", "reason": "VALUE_MISMATCH"})
            else:
                review_required = True
                failures.append("state_evidence_review_required")
                detail.update({"status": "REVIEW", "reason": "VALUE_MISMATCH"})
            details.append(detail)

        decision: Literal["PASS", "REVIEW", "REJECT"] = (
            "REJECT" if rejected else "REVIEW" if review_required else "PASS"
        )
        return _StateValidation(
            decision=decision,
            failures=tuple(dict.fromkeys(failures)),
            critical=critical,
            details=tuple(details),
            invalid_expectations=invalid_expectations,
        )

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
        state_validation = self._validate_expected_state(expectation, evidence)
        failures.extend(state_validation.failures)
        state_evidence_complete = state_validation.decision in {"NOT_REQUIRED", "PASS", "REJECT"}
        evidence_complete = (
            evidence.evidence_complete
            and not missing_checks
            and not invalid_scores
            and state_evidence_complete
        )
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
        critical = state_validation.critical or bool(self.critical_failures.intersection(failures))

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
        checks: dict[str, dict[str, Any]] = {
            name: {
                "score": normalized_scores.get(name),
                "passed": normalized_scores.get(name, 0.0) >= expectation.minimum_check_score,
                "critical": name in self.critical_failures,
            }
            for name in CHECK_NAMES
        }
        checks["state"] = {
            "status": state_validation.decision,
            "passed": state_validation.decision in {"NOT_REQUIRED", "PASS"},
            "critical": state_validation.critical,
            "details": list(state_validation.details),
            "invalid_expectations": list(state_validation.invalid_expectations),
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
            state_decision=state_validation.decision,
            state_observations=evidence.state_observations,
            model_execution_record_id=evidence.model_execution_record_id,
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
