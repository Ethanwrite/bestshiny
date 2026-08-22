from __future__ import annotations

import pytest
from evaluation_core import (
    EvaluationDecision,
    EvaluationEvidence,
    EvaluationExpectation,
    EvaluationResult,
    GenerationEvaluator,
    RetryEngine,
)
from pydantic import ValidationError


def _expectation(project_id: str) -> EvaluationExpectation:
    return EvaluationExpectation(
        project_id=project_id,
        shot_requirement={"end_state": {"orientation": "rear three-quarter"}},
        forbid_camera_gaze=True,
    )


def _complete_scores(value: float = 0.95) -> dict[str, float]:
    return {
        "identity": value,
        "hair": value,
        "wardrobe": value,
        "body": value,
        "props": value,
        "scene": value,
        "blocking": value,
        "eyeline": value,
        "lighting": value,
        "camera": value,
        "dialogue": value,
        "text": value,
        "motion": value,
        "continuity": value,
    }


def _mira_expectation(project_id: str) -> EvaluationExpectation:
    return EvaluationExpectation(
        project_id=project_id,
        expected_state={
            "characters": {
                "mira": {
                    "identity": {"identity_version_id": "mira-identity-v1"},
                    "appearance": {
                        "injury": {
                            "location": "right_eyebrow",
                            "status": "unhealed",
                            "blood_state": "dried",
                        },
                        "outfit": {"left_sleeve": "torn"},
                    },
                    "props": {"flare": {"state": "unlit"}},
                }
            },
            "lighting": {"time_of_day": "dusk", "palette": "cold_blue_gray"},
            "required_state_paths": [
                "characters.mira.identity.identity_version_id",
                "characters.mira.appearance.injury.status",
                "characters.mira.appearance.injury.blood_state",
                "characters.mira.appearance.outfit.left_sleeve",
                "lighting.time_of_day",
                "lighting.palette",
            ],
            "continuity_constraints": [
                {
                    "id": "flare_must_remain_unlit",
                    "path": "characters.mira.props.flare.state",
                    "rule": "MUST_EQUAL",
                    "value": "unlit",
                    "evidence_required": True,
                }
            ],
        },
    )


def _mira_state_observations() -> dict[str, dict[str, object]]:
    return {
        "characters.mira.identity.identity_version_id": {
            "value": "mira-identity-v1",
            "confidence": 0.99,
        },
        "characters.mira.appearance.injury.status": {
            "value": "unhealed",
            "confidence": 0.93,
        },
        "characters.mira.appearance.injury.blood_state": {
            "value": "dried",
            "confidence": 0.91,
        },
        "characters.mira.appearance.outfit.left_sleeve": {
            "value": "torn",
            "confidence": 0.95,
        },
        "characters.mira.props.flare.state": {
            "value": "unlit",
            "confidence": 0.98,
        },
        "lighting.time_of_day": {"value": "dusk", "confidence": 0.9},
        "lighting.palette": {"value": "cold_blue_gray", "confidence": 0.9},
    }


def test_evaluator_accepts_only_complete_high_scoring_evidence(project):
    result = GenerationEvaluator().evaluate(
        _expectation(project.id),
        EvaluationEvidence(
            scores=_complete_scores(),
            evidence_complete=True,
            judge_provider="test-visual-judge",
        ),
    )
    assert result.decision == EvaluationDecision.ACCEPT
    assert result.critical_failure is False


def test_required_state_paths_pass_only_with_matching_confident_observations(project):
    result = GenerationEvaluator().evaluate(
        _mira_expectation(project.id),
        EvaluationEvidence(
            scores=_complete_scores(),
            state_observations=_mira_state_observations(),
            evidence_complete=True,
            judge_provider="trusted-state-judge",
        ),
    )

    assert result.decision == EvaluationDecision.ACCEPT
    assert result.state_decision == "PASS"
    assert result.checks["state"]["passed"] is True
    assert all(item["status"] == "PASS" for item in result.checks["state"]["details"])


def test_legacy_nested_state_evidence_and_path_aliases_remain_supported(project):
    legacy_observations = [
        {
            "state_path": f"/{path.replace('.', '/')}",
            "observed_value": observation["value"],
            "confidence": observation["confidence"],
        }
        for path, observation in _mira_state_observations().items()
    ]
    result = GenerationEvaluator().evaluate(
        _mira_expectation(project.id),
        EvaluationEvidence(
            scores=_complete_scores(),
            observations={"state_paths": legacy_observations},
            evidence_complete=True,
            judge_provider="legacy-state-judge",
        ),
    )

    assert result.decision == EvaluationDecision.ACCEPT
    assert result.state_decision == "PASS"


def test_missing_or_low_confidence_required_state_evidence_requires_review(project):
    observations = _mira_state_observations()
    observations.pop("characters.mira.props.flare.state")
    observations["lighting.palette"] = {
        "value": "cold_blue_gray",
        "confidence": 0.2,
    }
    result = GenerationEvaluator().evaluate(
        _mira_expectation(project.id),
        EvaluationEvidence(
            scores=_complete_scores(),
            state_observations=observations,
            evidence_complete=True,
            judge_provider="trusted-state-judge",
        ),
    )

    assert result.decision == EvaluationDecision.REJECT
    assert result.state_decision == "REVIEW"
    assert result.evidence_complete is False
    assert "state_evidence_review_required" in result.retry_reasons
    reasons = {item["reason"] for item in result.checks["state"]["details"]}
    assert {"MISSING_OBSERVATION", "LOW_CONFIDENCE"}.issubset(reasons)


def test_continuity_constraint_mismatch_is_an_observed_state_rejection(project):
    observations = _mira_state_observations()
    observations["characters.mira.props.flare.state"] = {
        "value": "lit",
        "confidence": 0.99,
    }
    result = GenerationEvaluator().evaluate(
        _mira_expectation(project.id),
        EvaluationEvidence(
            scores=_complete_scores(),
            state_observations=observations,
            evidence_complete=True,
            judge_provider="trusted-state-judge",
        ),
    )

    assert result.decision == EvaluationDecision.RETRY_SAME_MODEL
    assert result.state_decision == "REJECT"
    assert result.evidence_complete is True
    assert result.critical_failure is False
    assert "state_continuity_mismatch" in result.retry_reasons
    assert "flare_must_remain_unlit" in result.retry_reasons


def test_identity_path_mismatch_is_always_a_hard_failure(project):
    observations = _mira_state_observations()
    observations["characters.mira.identity.identity_version_id"] = {
        "value": "different-character-v4",
        "confidence": 0.99,
    }
    result = GenerationEvaluator().evaluate(
        _mira_expectation(project.id),
        EvaluationEvidence(
            scores=_complete_scores(),
            state_observations=observations,
            evidence_complete=True,
            judge_provider="trusted-state-judge",
        ),
    )

    assert result.decision == EvaluationDecision.RETRY_REWRITE_PROMPT
    assert result.state_decision == "REJECT"
    assert result.critical_failure is True
    assert "state_identity_mismatch" in result.retry_reasons
    assert "Never change an identity path" in result.retry_patch


def test_direct_camera_gaze_is_critical_and_generates_specific_patch(project):
    result = GenerationEvaluator().evaluate(
        _expectation(project.id),
        EvaluationEvidence(
            scores=_complete_scores(),
            observations={"direct_camera_gaze": True},
            evidence_complete=True,
            judge_provider="test-visual-judge",
        ),
    )
    assert result.decision == EvaluationDecision.RETRY_REWRITE_PROMPT
    assert result.critical_failure is True
    assert "direct_camera_gaze" in result.retry_reasons
    assert "never acknowledges the camera" in result.retry_patch


def test_evaluator_never_invents_a_pass_without_visual_evidence(project):
    result = GenerationEvaluator().evaluate(_expectation(project.id))
    assert result.decision == EvaluationDecision.REJECT
    assert result.evidence_complete is False
    assert "visual_evidence_unavailable" in result.retry_reasons


def test_evaluator_rejects_self_declared_complete_but_partial_scores(project):
    result = GenerationEvaluator().evaluate(
        _expectation(project.id),
        EvaluationEvidence(
            scores={"identity": 1.0},
            evidence_complete=True,
            judge_provider="partial-test-judge",
        ),
    )
    assert result.decision == EvaluationDecision.REJECT
    assert result.evidence_complete is False
    assert "visual_evidence_incomplete" in result.retry_reasons


@pytest.mark.parametrize("invalid_score", [float("nan"), float("inf"), float("-inf"), 100.0, "0.95"])
def test_evaluation_evidence_rejects_nonfinite_or_out_of_range_scores(invalid_score):
    with pytest.raises(ValidationError):
        EvaluationEvidence(scores=_complete_scores(invalid_score), evidence_complete=True)


def test_evaluator_rejects_invalid_scores_even_if_schema_validation_is_bypassed(project):
    evidence = EvaluationEvidence.model_construct(
        scores=_complete_scores(float("nan")),
        observations={},
        detected_failures=[],
        evidence_complete=True,
        judge_provider="unsafe-test-judge",
        judge_model="",
    )
    result = GenerationEvaluator().evaluate(_expectation(project.id), evidence)
    assert result.decision == EvaluationDecision.REJECT
    assert result.evidence_complete is False
    assert "visual_evidence_invalid" in result.retry_reasons


def test_retry_engine_patches_first_and_stops_at_configured_limit(project):
    evaluation = GenerationEvaluator().evaluate(
        _expectation(project.id),
        EvaluationEvidence(
            scores={**_complete_scores(), "eyeline": 0.4},
            observations={"direct_camera_gaze": True},
            evidence_complete=True,
            judge_provider="test-visual-judge",
        ),
    )
    engine = RetryEngine(max_auto_retries=2)
    first = engine.plan(
        evaluation,
        attempt_number=0,
        current_provider="grok",
        current_model="grok-video",
        alternatives=[("kling", "kling-3.0")],
    )
    assert first.action == EvaluationDecision.RETRY_REWRITE_PROMPT
    assert first.inject_stronger_references is True
    assert first.terminal is False

    stopped = engine.plan(
        evaluation,
        attempt_number=2,
        current_provider="grok",
        current_model="grok-video",
    )
    assert stopped.action == EvaluationDecision.REJECT
    assert stopped.terminal is True


def test_retry_engine_honors_switch_model_decision_even_with_patch():
    evaluation = EvaluationResult(
        decision=EvaluationDecision.SWITCH_MODEL,
        overall_score=0.5,
        critical_failure=False,
        scores=_complete_scores(0.5),
        checks={},
        retry_reasons=["motion"],
        retry_patch="simplify the motion",
        evidence_complete=True,
        evaluator_version="test",
        judge_provider="test",
        judge_model="test",
    )
    plan = RetryEngine(max_auto_retries=2).plan(
        evaluation,
        attempt_number=1,
        current_provider="google_flow",
        current_model="flow-veo-3.1",
        alternatives=[("kling", "kling-3.0")],
    )
    assert plan.action == EvaluationDecision.SWITCH_MODEL
    assert (plan.next_provider, plan.next_model) == ("kling", "kling-3.0")
    assert plan.prompt_patch == "simplify the motion"
