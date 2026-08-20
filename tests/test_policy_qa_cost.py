from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from continuity_core import ContinuityRiskVector
from director_production import CandidateNotCommittable
from evaluation_core import EvaluationEvidence
from platform_contracts import GenerationRequest
from production_domain.models import (
    CandidateStatus,
    ContinuityMode,
    CostRecord,
    DecisionRecord,
    Episode,
    GenerationCandidate,
    GenerationJob,
    GenerationPolicy,
    JobStatus,
    MediaAsset,
    QADecision,
    QAResult,
    Scene,
    Shot,
    ShotStateSnapshot,
    TimelineState,
)
from qa_core import analyze_identity_drift
from sqlalchemy import func, select


def test_continuity_hard_and_reanchor_decisions(container, project):
    hard = container.continuity_decision.decide(
        ContinuityRiskVector(camera_angle_delta=0.05, action_continuity=0.95),
        project_id=project.id,
    )
    assert hard.mode == ContinuityMode.HARD_CONTINUITY.value
    assert hard.use_previous_end_frame is True
    reverse = container.continuity_decision.decide(
        ContinuityRiskVector(camera_axis_delta=0.9, face_visibility=0.2),
        project_id=project.id,
    )
    assert reverse.mode == ContinuityMode.RE_ANCHOR.value
    assert "CAMERA_AXIS_CHANGE" in reverse.reasons
    assert reverse.require_new_keyframe is True


def test_capability_resolver_falls_back_or_degrades(container, project):
    fallback = container.capability_resolver.resolve(
        GenerationPolicy.START_END_FRAME.value,
        "grok",
        ["veo_official"],
        project_id=project.id,
    )
    assert fallback.provider == "veo_official"
    assert fallback.policy == GenerationPolicy.START_END_FRAME.value
    degraded = container.capability_resolver.resolve(
        GenerationPolicy.START_END_FRAME.value,
        "grok",
        [],
        project_id=project.id,
    )
    assert degraded.policy == GenerationPolicy.IMAGE_TO_VIDEO.value
    assert degraded.degraded_from == GenerationPolicy.START_END_FRAME.value


def test_identity_drift_uses_minimum_and_slope_not_average_only():
    samples = [{"face_similarity": value} for value in [0.93, 0.91, 0.88, 0.79, 0.70, 0.62]]
    metrics = analyze_identity_drift(samples)
    assert metrics.average_similarity > 0.80
    assert metrics.minimum_similarity == 0.62
    assert metrics.drift_slope < -0.05


def test_occluded_face_falls_back_to_body_hair_costume_tracking():
    metrics = analyze_identity_drift(
        [
            {
                "face_similarity": None,
                "body_similarity": 0.91,
                "hair_similarity": 0.88,
                "costume_similarity": 0.94,
                "tracking_continuity": 0.96,
            }
        ]
    )
    assert metrics.usable_samples == 1
    assert metrics.minimum_similarity > 0.9


def _candidate_video(container, project, tmp_path, *, episode_number: int = 1):
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Pilot", episode_number=episode_number)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Room")
        session.add(scene)
        session.flush()
        input_state = TimelineState(
            project_id=project.id, episode_id=episode.id, scene_id=scene.id, state_kind="SHOT_INPUT"
        )
        output_state = TimelineState(
            project_id=project.id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_OUTPUT",
            state_json={"characters": {"lin": {"position": "center"}}},
        )
        session.add_all([input_state, output_state])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=1,
            prompt="Lin turns once.",
            user_prompt="Lin turns once.",
            compiled_prompt="Lin turns once.",
            input_state_id=input_state.id,
            output_state_id=output_state.id,
        )
        session.add(shot)
        session.flush()
        input_state.shot_id = shot.id
        output_state.shot_id = shot.id
        candidate = GenerationCandidate(shot_id=shot.id, attempt_number=1, status="VALIDATING")
        session.add(candidate)
        session.flush()
        shot_id, candidate_id = shot.id, candidate.id
    video_path = tmp_path / "candidate.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x240:d=1",
            "-pix_fmt",
            "yuv420p",
            str(video_path),
        ],
        check=True,
        capture_output=True,
    )
    with video_path.open("rb") as stream:
        asset, _ = container.media.register(
            project.id,
            "VIDEO",
            stream,
            filename="candidate.mp4",
            mime_type="video/mp4",
            shot_id=shot_id,
            generation_candidate_id=candidate_id,
        )
    with container.database.session() as session:
        session.get(GenerationCandidate, candidate_id).output_asset_id = asset.id
    return shot_id, candidate_id


def _complete_visual_scores(value: float = 0.95) -> dict[str, float]:
    return {
        name: value
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


def _completed_candidate_job(container, project, tmp_path, *, idempotency_key: str):
    shot_id, candidate_id = _candidate_video(container, project, tmp_path)
    with container.database.session() as session:
        output_asset_id = session.get(GenerationCandidate, candidate_id).output_asset_id
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            shot_id=shot_id,
            candidate_id=candidate_id,
            type="video",
            provider="google_flow",
            model="flow-veo-3.1",
            prompt="Lin turns once.",
            idempotency_key=idempotency_key,
            metadata={
                "canonical_shot_spec": {
                    "intent": "Lin turns once.",
                    "dominant_action": "Lin turns once.",
                    "allow_camera_gaze": False,
                }
            },
        )
    )
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        stored.status = JobStatus.COMPLETED.value
        stored.output_asset_id = output_asset_id
    return shot_id, candidate_id, job.id


def test_cost_recording_is_exactly_once_under_concurrency(container, project, tmp_path):
    _, _, job_id = _completed_candidate_job(
        container,
        project,
        tmp_path,
        idempotency_key="cost-record-exactly-once",
    )
    workers = 8
    start = threading.Barrier(workers)

    def record_once(_: int) -> str:
        start.wait(timeout=10)
        return container.cost.record_job(
            job_id,
            estimated_cost=1.25,
            actual_cost=1.1,
            credits=125,
            retry_cost=0.05,
            resolution="1080p",
        ).id

    with ThreadPoolExecutor(max_workers=workers) as pool:
        record_ids = list(pool.map(record_once, range(workers)))
    assert len(set(record_ids)) == 1
    with container.database.session() as session:
        records = list(session.scalars(select(CostRecord).where(CostRecord.generation_job_id == job_id)))
        assert len(records) == 1
        assert records[0].actual_cost == 1.1
        assert records[0].credits == 125


def test_qa_hard_fails_sustained_identity_drift(container, project, tmp_path):
    _, candidate_id = _candidate_video(container, project, tmp_path)
    result = container.qa.validate_candidate(
        candidate_id,
        {
            "identity_samples": [
                {"face_similarity": value} for value in [0.93, 0.91, 0.88, 0.79, 0.70, 0.62]
            ],
            "character_score": 0.84,
            "scene_score": 0.9,
            "composition_score": 0.85,
            "action_score": 0.88,
            "camera_score": 0.8,
            "lighting_score": 0.8,
            "narrative_score": 0.9,
        },
    )
    assert result.decision == QADecision.HARD_FAIL.value
    assert "SUSTAINED_IDENTITY_DRIFT" in result.hard_failures


def test_partial_high_score_evidence_never_auto_passes(container, project, tmp_path):
    _, candidate_id = _candidate_video(container, project, tmp_path)
    result = container.qa.validate_candidate(candidate_id, {"character_score": 1.0})
    assert result.decision == QADecision.USER_REVIEW_REQUIRED.value
    assert result.metrics_json["evidence_complete"] is False
    assert "camera" in result.metrics_json["missing_dimensions"]
    assert result.metrics_json["identity"]["usable_samples"] == 0
    with pytest.raises(CandidateNotCommittable):
        container.candidates.commit(candidate_id)


@pytest.mark.parametrize("invalid_score", [100.0, float("inf"), float("nan"), "0.95"])
def test_invalid_identity_evidence_fails_closed(
    container,
    project,
    tmp_path,
    invalid_score,
):
    _, candidate_id = _candidate_video(container, project, tmp_path)
    result = container.qa.validate_candidate(
        candidate_id,
        {
            "identity_samples": [{"face_similarity": invalid_score}] * 6,
            "character_score": 0.95,
            "scene_score": 0.95,
            "composition_score": 0.95,
            "action_score": 0.95,
            "camera_score": 0.95,
            "lighting_score": 0.95,
            "narrative_score": 0.95,
        },
    )
    assert result.decision == QADecision.HARD_FAIL.value
    assert "INVALID_IDENTITY_EVIDENCE" in result.hard_failures
    assert result.metrics_json["evidence_complete"] is False


def test_candidate_pass_commit_updates_timeline_and_cost(container, project, tmp_path):
    shot_id, candidate_id = _candidate_video(container, project, tmp_path)
    result = container.qa.validate_candidate(
        candidate_id,
        {
            "identity_samples": [{"face_similarity": value} for value in [0.9, 0.91, 0.89, 0.9, 0.88, 0.9]],
            "character_score": 0.9,
            "scene_score": 0.9,
            "composition_score": 0.85,
            "action_score": 0.9,
            "camera_score": 0.85,
            "lighting_score": 0.85,
            "narrative_score": 0.9,
        },
    )
    assert result.decision == QADecision.PASS.value
    with container.database.session() as session:
        session.add(
            CostRecord(
                project_id=project.id,
                shot_id=shot_id,
                candidate_id=candidate_id,
                provider="fake",
                model="fake",
                actual_cost=1.25,
            )
        )
    committed = container.candidates.commit(candidate_id)
    assert committed.status == "COMMITTED"
    with container.database.session() as session:
        shot = session.get(Shot, shot_id)
        state = session.get(TimelineState, shot.output_state_id)
        assert shot.committed_candidate_id == candidate_id
        assert state.state_json["end_frame_asset_id"] == shot.end_frame_asset_id
    assert container.cost.shot_cost(shot_id)["cost_per_accepted_shot"] == 1.25


def test_identical_outputs_keep_distinct_candidate_and_end_frame_lineage(
    container,
    project,
    tmp_path,
):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first_shot_id, first_candidate_id = _candidate_video(container, project, first_dir)
    second_shot_id, second_candidate_id = _candidate_video(
        container,
        project,
        second_dir,
        episode_number=2,
    )
    evidence = {
        "identity_samples": [{"face_similarity": 0.92}] * 6,
        "character_score": 0.92,
        "scene_score": 0.92,
        "composition_score": 0.92,
        "action_score": 0.92,
        "camera_score": 0.92,
        "lighting_score": 0.92,
        "narrative_score": 0.92,
    }
    for candidate_id in (first_candidate_id, second_candidate_id):
        assert container.qa.validate_candidate(candidate_id, evidence).decision == QADecision.PASS.value
        container.candidates.commit(candidate_id)
    with container.database.session() as session:
        first_candidate = session.get(GenerationCandidate, first_candidate_id)
        second_candidate = session.get(GenerationCandidate, second_candidate_id)
        first_shot = session.get(Shot, first_shot_id)
        second_shot = session.get(Shot, second_shot_id)
        first_video = session.get(MediaAsset, first_candidate.output_asset_id)
        second_video = session.get(MediaAsset, second_candidate.output_asset_id)
        first_end = session.get(MediaAsset, first_shot.end_frame_asset_id)
        second_end = session.get(MediaAsset, second_shot.end_frame_asset_id)
        assert first_video.id != second_video.id
        assert first_video.sha256 == second_video.sha256
        assert first_video.generation_candidate_id == first_candidate_id
        assert second_video.generation_candidate_id == second_candidate_id
        assert first_end.id != second_end.id
        assert first_end.sha256 == second_end.sha256
        assert (first_end.shot_id, first_end.parent_asset_id) == (
            first_shot_id,
            first_video.id,
        )
        assert (second_end.shot_id, second_end.parent_asset_id) == (
            second_shot_id,
            second_video.id,
        )


def test_concurrent_candidate_commit_selects_one_canonical_winner(
    container,
    project,
    tmp_path,
    monkeypatch,
):
    shot_id, first_id = _candidate_video(container, project, tmp_path)
    second_path = tmp_path / "candidate-red.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x240:d=1",
            "-pix_fmt",
            "yuv420p",
            str(second_path),
        ],
        check=True,
        capture_output=True,
    )
    with container.database.session() as session:
        second = GenerationCandidate(
            shot_id=shot_id,
            attempt_number=2,
            status=CandidateStatus.VALIDATING.value,
        )
        session.add(second)
        session.flush()
        second_id = second.id
    with second_path.open("rb") as stream:
        second_asset, _ = container.media.register(
            project.id,
            "VIDEO",
            stream,
            filename="candidate-red.mp4",
            mime_type="video/mp4",
            shot_id=shot_id,
            generation_candidate_id=second_id,
        )
    with container.database.session() as session:
        session.get(GenerationCandidate, second_id).output_asset_id = second_asset.id

    complete_evidence = {
        "identity_samples": [{"face_similarity": 0.92}] * 6,
        "character_score": 0.92,
        "scene_score": 0.92,
        "composition_score": 0.92,
        "action_score": 0.92,
        "camera_score": 0.92,
        "lighting_score": 0.92,
        "narrative_score": 0.92,
    }
    for candidate_id in (first_id, second_id):
        assert (
            container.qa.validate_candidate(candidate_id, complete_evidence).decision == QADecision.PASS.value
        )
        with container.database.session() as session:
            session.add(
                CostRecord(
                    project_id=project.id,
                    shot_id=shot_id,
                    candidate_id=candidate_id,
                    provider="fake",
                    model="fake",
                    actual_cost=1.0,
                )
            )

    extracted = threading.Barrier(2)
    real_extract = container.continuity.extract_end_frame

    def synchronized_extract(shot: str, video: str):
        result = real_extract(shot, video)
        extracted.wait(timeout=10)
        return result

    monkeypatch.setattr(container.continuity, "extract_end_frame", synchronized_extract)

    def attempt(candidate_id: str) -> tuple[str, str]:
        try:
            return "committed", container.candidates.commit(candidate_id).id
        except CandidateNotCommittable:
            return "rejected", candidate_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (first_id, second_id)))

    assert sorted(result[0] for result in results) == ["committed", "rejected"]
    winner_id = next(candidate_id for status, candidate_id in results if status == "committed")
    with container.database.session() as session:
        shot = session.get(Shot, shot_id)
        candidates = list(
            session.scalars(select(GenerationCandidate).where(GenerationCandidate.shot_id == shot_id))
        )
        costs = list(session.scalars(select(CostRecord).where(CostRecord.shot_id == shot_id)))
        end_frame = session.get(MediaAsset, shot.end_frame_asset_id)
        output_state = session.get(TimelineState, shot.output_state_id)
        assert shot.committed_candidate_id == winner_id
        assert [item.status for item in candidates].count(CandidateStatus.COMMITTED.value) == 1
        assert [item.accepted for item in costs].count(True) == 1
        assert [item.wasted for item in costs].count(True) == 1
        assert end_frame.generation_candidate_id == winner_id
        assert output_state.state_json["committed_candidate_id"] == winner_id
        assert output_state.state_json["end_frame_asset_id"] == shot.end_frame_asset_id
        assert (
            session.scalar(
                select(func.count(ShotStateSnapshot.id)).where(ShotStateSnapshot.shot_id == shot_id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(DecisionRecord.id)).where(
                    DecisionRecord.shot_id == shot_id,
                    DecisionRecord.decision_type == "CANDIDATE_COMMIT",
                )
            )
            == 1
        )


def test_committed_candidate_cannot_be_revalidated(container, project, tmp_path):
    shot_id, candidate_id = _candidate_video(container, project, tmp_path)
    complete_evidence = {
        "identity_samples": [{"face_similarity": 0.92}] * 6,
        "character_score": 0.92,
        "scene_score": 0.92,
        "composition_score": 0.92,
        "action_score": 0.92,
        "camera_score": 0.92,
        "lighting_score": 0.92,
        "narrative_score": 0.92,
    }
    container.qa.validate_candidate(candidate_id, complete_evidence)
    container.candidates.commit(candidate_id)
    with pytest.raises(LookupError, match="cannot be revalidated"):
        container.candidates.sync_candidate(candidate_id, {})
    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        shot = session.get(Shot, shot_id)
        assert candidate.status == CandidateStatus.COMMITTED.value
        assert shot.committed_candidate_id == candidate_id


def test_revalidation_before_commit_claim_blocks_all_canonical_side_effects(
    container,
    project,
    tmp_path,
    monkeypatch,
):
    shot_id, candidate_id = _candidate_video(container, project, tmp_path)
    complete_evidence = {
        "identity_samples": [{"face_similarity": 0.92}] * 6,
        "character_score": 0.92,
        "scene_score": 0.92,
        "composition_score": 0.92,
        "action_score": 0.92,
        "camera_score": 0.92,
        "lighting_score": 0.92,
        "narrative_score": 0.92,
    }
    container.qa.validate_candidate(candidate_id, complete_evidence)
    extracted = threading.Event()
    release = threading.Event()
    real_extract = container.continuity.extract_end_frame

    def pause_after_extraction(shot: str, video: str):
        frame = real_extract(shot, video)
        extracted.set()
        assert release.wait(timeout=10)
        return frame

    monkeypatch.setattr(container.continuity, "extract_end_frame", pause_after_extraction)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(container.candidates.commit, candidate_id)
        assert extracted.wait(timeout=10)
        review = container.qa.validate_candidate(candidate_id, {"character_score": 1.0})
        assert review.decision == QADecision.USER_REVIEW_REQUIRED.value
        release.set()
        with pytest.raises(CandidateNotCommittable):
            future.result(timeout=10)
    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        shot = session.get(Shot, shot_id)
        assert candidate.status == CandidateStatus.USER_REVIEW_REQUIRED.value
        assert shot.committed_candidate_id is None
        assert shot.end_frame_asset_id is None
        assert (
            session.scalar(
                select(func.count(ShotStateSnapshot.id)).where(ShotStateSnapshot.shot_id == shot_id)
            )
            == 0
        )


def test_visual_rejection_overrides_legacy_qa_pass_and_blocks_commit(container, project, tmp_path):
    _, candidate_id, _ = _completed_candidate_job(container, project, tmp_path, idempotency_key="visual-gate")
    container.feature_flags.set("auto_evaluation", True, project_id=project.id)
    candidate = container.candidates.sync_candidate(
        candidate_id,
        {
            "identity_samples": [{"face_similarity": 0.95}] * 6,
            "character_score": 0.95,
            "scene_score": 0.95,
            "composition_score": 0.95,
            "action_score": 0.95,
            "camera_score": 0.95,
            "lighting_score": 0.95,
            "narrative_score": 0.95,
            "evaluation_evidence": {
                "scores": _complete_visual_scores(),
                "observations": {"direct_camera_gaze": True},
                "evidence_complete": True,
                "judge_provider": "test-visual-judge",
            },
        },
    )
    assert candidate.status == CandidateStatus.HARD_FAILED.value
    with container.database.session() as session:
        qa = session.get(QAResult, candidate.qa_result_id)
        assert qa.decision == QADecision.HARD_FAIL.value
        assert "VISUAL_RETRY_REWRITE_PROMPT" in qa.hard_failures
    with pytest.raises(CandidateNotCommittable):
        container.candidates.commit(candidate_id)


def test_malformed_visual_evidence_cannot_leave_a_legacy_qa_pass(container, project, tmp_path):
    _, candidate_id, _ = _completed_candidate_job(
        container, project, tmp_path, idempotency_key="malformed-visual-evidence"
    )
    container.feature_flags.set("auto_evaluation", True, project_id=project.id)
    with pytest.raises(ValueError):
        container.candidates.sync_candidate(
            candidate_id,
            {
                "identity_samples": [{"face_similarity": 0.95}] * 6,
                "character_score": 0.95,
                "scene_score": 0.95,
                "composition_score": 0.95,
                "action_score": 0.95,
                "camera_score": 0.95,
                "lighting_score": 0.95,
                "narrative_score": 0.95,
                "evaluation_evidence": {
                    "scores": {"identity": "not-a-number"},
                    "evidence_complete": True,
                },
            },
        )
    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        assert candidate.qa_result_id is None
        assert candidate.status != CandidateStatus.PASSED.value
    with pytest.raises(CandidateNotCommittable):
        container.candidates.commit(candidate_id)


def test_visual_evaluator_error_turns_legacy_pass_into_hard_failure(
    container, project, tmp_path, monkeypatch
):
    _, candidate_id, _ = _completed_candidate_job(
        container, project, tmp_path, idempotency_key="visual-evaluator-error"
    )
    container.feature_flags.set("auto_evaluation", True, project_id=project.id)

    def fail_evaluation(*_args, **_kwargs):
        raise RuntimeError("visual judge unavailable")

    monkeypatch.setattr(container.visual_runtime, "evaluate_job", fail_evaluation)
    with pytest.raises(RuntimeError, match="visual judge unavailable"):
        container.candidates.sync_candidate(
            candidate_id,
            {
                "identity_samples": [{"face_similarity": 0.95}] * 6,
                "character_score": 0.95,
                "scene_score": 0.95,
                "composition_score": 0.95,
                "action_score": 0.95,
                "camera_score": 0.95,
                "lighting_score": 0.95,
                "narrative_score": 0.95,
                "evaluation_evidence": {
                    "scores": _complete_visual_scores(),
                    "evidence_complete": True,
                    "judge_provider": "test-visual-judge",
                },
            },
        )
    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        qa = session.get(QAResult, candidate.qa_result_id)
        assert candidate.status == CandidateStatus.HARD_FAILED.value
        assert qa.decision == QADecision.HARD_FAIL.value
        assert "VISUAL_EVALUATION_ERROR" in qa.hard_failures
    with pytest.raises(CandidateNotCommittable):
        container.candidates.commit(candidate_id)


def test_repeated_visual_retry_reuses_job_without_orphan_candidate(container, project, tmp_path):
    shot_id, _, job_id = _completed_candidate_job(
        container, project, tmp_path, idempotency_key="retry-source"
    )
    container.feature_flags.set("auto_retry", True, project_id=project.id)
    evidence = EvaluationEvidence(
        scores=_complete_visual_scores(),
        observations={"direct_camera_gaze": True},
        evidence_complete=True,
        judge_provider="test-visual-judge",
    )
    _, first_plan, first_retry = container.visual_runtime.evaluate_job(job_id, evidence)
    _, second_plan, second_retry = container.visual_runtime.evaluate_job(job_id, evidence)
    assert first_plan is not None and first_plan.terminal is False
    assert second_plan is not None and second_plan.terminal is False
    assert first_retry is not None and second_retry is not None
    assert first_retry.id == second_retry.id
    with container.database.session() as session:
        jobs = list(session.scalars(select(GenerationJob).where(GenerationJob.shot_id == shot_id)))
        candidates = list(
            session.scalars(select(GenerationCandidate).where(GenerationCandidate.shot_id == shot_id))
        )
    assert len(jobs) == 2
    assert len(candidates) == 2
