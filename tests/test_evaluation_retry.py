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
