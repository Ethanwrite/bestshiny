"""The loop closes: an evaluated generation writes a wide production observation."""

from __future__ import annotations

import pytest
from evaluation_core import EvaluationEvidence
from platform_contracts import GenerationRequest
from production_domain.models import GenerationJob, JobStatus
from router_evidence_core import ReferenceMode, Scenario, TaskType

SCORES = {
    name: 0.91
    for name in (
        "identity",
        "hair",
        "wardrobe",
        "body",
        "props",
        "scene",
        "blocking",
        "eyeline",
        "lighting",
        "camera",
        "dialogue",
        "text",
        "motion",
        "continuity",
    )
}

ROUTING_CONTEXT = {
    "task_type": "I2V",
    "scenario": "identity",
    "reference_mode": "FIRST_FRAME",
    "duration_seconds": 5.0,
    "resolution": "720P",
    "aspect_ratio": "16:9",
    "asset_criticality": "STANDARD",
    "router_version": "video-router-v2",
    "exact_version": "flow-veo-3.1-manual-v1",
}


@pytest.fixture
def output_asset(container, project, register_bytes):  # type: ignore[no-untyped-def]
    """A real media asset, because the job's output column has a foreign key."""

    return register_bytes(container, project.id, "VIDEO", b"video-bytes").id


def _job(  # type: ignore[no-untyped-def]
    container,
    project,
    output_asset_id,
    *,
    key: str,
    routing_context: dict | None = ROUTING_CONTEXT,
    status=JobStatus.COMPLETED,
):
    metadata: dict[str, object] = {
        "canonical_shot_spec": {
            "intent": "Lin turns once.",
            "dominant_action": "Lin turns once.",
            "allow_camera_gaze": True,
        }
    }
    if routing_context is not None:
        metadata["routing_context"] = routing_context
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="google_flow",
            model="flow-veo-3.1",
            prompt="Lin turns once.",
            idempotency_key=key,
            metadata=metadata,
        )
    )
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        stored.status = status.value
        stored.output_asset_id = output_asset_id
    return job.id


def _evaluate(container, job_id):  # type: ignore[no-untyped-def]
    return container.visual_runtime.evaluate_job(
        job_id,
        EvaluationEvidence(
            scores=SCORES, evidence_complete=True, judge_provider="test-visual-judge"
        ),
    )


def test_an_evaluated_generation_writes_one_observation(container, project, output_asset) -> None:  # type: ignore[no-untyped-def]
    job_id = _job(container, project, output_asset, key="obs-1")
    _evaluate(container, job_id)
    stored = container.router_observations.observations()
    assert len(stored) == 1
    observation = stored[0]
    assert observation.provider == "google_flow"
    assert observation.model_id == "flow-veo-3.1"
    assert observation.exact_version == "flow-veo-3.1-manual-v1"
    assert observation.task_type is TaskType.I2V
    assert observation.scenario is Scenario.IDENTITY
    assert observation.reference_mode is ReferenceMode.FIRST_FRAME
    assert observation.resolution == "720P"
    assert observation.generation_success is True
    assert observation.qc_identity_score == pytest.approx(0.91)
    assert observation.qc_motion_score == pytest.approx(0.91)
    assert observation.qc_temporal_consistency == pytest.approx(0.91)
    assert observation.generation_job_id == job_id


def test_prompt_alignment_is_left_unobserved_not_mapped_to_a_neighbour(  # type: ignore[no-untyped-def]
    container, project, output_asset
) -> None:
    """The evaluator publishes no prompt-adherence check, so the field stays empty."""

    job_id = _job(container, project, output_asset, key="obs-2")
    _evaluate(container, job_id)
    assert container.router_observations.observations()[0].qc_prompt_alignment is None


def test_evaluating_twice_still_leaves_one_observation(container, project, output_asset) -> None:  # type: ignore[no-untyped-def]
    job_id = _job(container, project, output_asset, key="obs-3")
    _evaluate(container, job_id)
    _evaluate(container, job_id)
    assert len(container.router_observations.observations()) == 1


def test_a_job_planned_before_this_existed_is_skipped_not_guessed(container, project, output_asset) -> None:  # type: ignore[no-untyped-def]
    """Re-deriving the scene from today's shot spec would file it under a cell that never ran."""

    job_id = _job(container, project, output_asset, key="obs-4", routing_context=None)
    _evaluate(container, job_id)
    assert container.router_observations.observations() == []


def test_a_routing_context_without_a_version_is_skipped(container, project, output_asset) -> None:  # type: ignore[no-untyped-def]
    context = dict(ROUTING_CONTEXT, exact_version="")
    job_id = _job(container, project, output_asset, key="obs-5", routing_context=context)
    _evaluate(container, job_id)
    assert container.router_observations.observations() == []


def test_the_recorder_never_fails_the_evaluation(container, project, output_asset, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Evidence collection is not part of delivering the user's shot."""

    def explode(_observation):  # type: ignore[no-untyped-def]
        raise ValueError("storage is unhappy")

    monkeypatch.setattr(container.router_observations, "record", explode)
    job_id = _job(container, project, output_asset, key="obs-6")
    result, _plan, _retry = _evaluate(container, job_id)
    assert result is not None
    assert container.router_observations.observations() == []


def test_the_planner_writes_the_routing_context_onto_the_request() -> None:
    """The field the recorder depends on is produced where the decision is made."""

    from pathlib import Path

    source = Path("services/production-engine/production_engine/runtime.py").read_text("utf-8")
    assert '"routing_context": {' in source
    for field in (
        "task_type",
        "scenario",
        "reference_mode",
        "duration_seconds",
        "resolution",
        "asset_criticality",
        "exact_version",
    ):
        assert f'"{field}":' in source
