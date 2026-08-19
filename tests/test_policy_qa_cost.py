from __future__ import annotations

import subprocess

from continuity_core import ContinuityRiskVector
from production_domain.models import (
    ContinuityMode,
    CostRecord,
    Episode,
    GenerationCandidate,
    GenerationPolicy,
    QADecision,
    Scene,
    Shot,
    TimelineState,
)
from qa_core import analyze_identity_drift


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


def _candidate_video(container, project, tmp_path):
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Pilot", episode_number=1)
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
