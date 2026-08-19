from __future__ import annotations

import subprocess

from platform_contracts import GenerationRequest
from production_domain.models import (
    AssetType,
    BrowserWorker,
    ContinuityMode,
    Episode,
    JobStatus,
    Scene,
    Shot,
    WorkerCommand,
)


def test_end_frame_extraction_and_next_shot_chaining(container, project, tmp_path):
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Pilot", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Room")
        session.add(scene)
        session.flush()
        first = Shot(scene_id=scene.id, sequence=1, prompt="She opens the door")
        session.add(first)
        session.flush()
        second = Shot(
            scene_id=scene.id,
            sequence=2,
            prompt="She enters",
            previous_shot_id=first.id,
            continuity_mode=ContinuityMode.PREVIOUS_END_FRAME.value,
        )
        session.add(second)
        session.flush()
        first_id, second_id = first.id, second.id

    video_path = tmp_path / "shot.mp4"
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
        video, _ = container.media.register(
            project.id,
            AssetType.VIDEO.value,
            stream,
            filename="shot.mp4",
            mime_type="video/mp4",
            shot_id=first_id,
        )
    end_frame = container.continuity.extract_and_chain(first_id, video.id)
    assert end_frame.asset_type == AssetType.END_FRAME.value
    assert end_frame.local_path
    with container.database.session() as session:
        first = session.get(Shot, first_id)
        second = session.get(Shot, second_id)
        assert first.end_frame_asset_id == end_frame.id
        assert second.start_frame_asset_id == end_frame.id


def test_restart_recovery_never_blindly_resubmits(container, project):
    request = GenerationRequest(
        project_id=project.id,
        type="video",
        prompt="One action",
        idempotency_key="restart-1",
    )
    job, _ = container.gateway.create(request)
    with container.database.session() as session:
        current = session.get(type(job), job.id)
        current.status = JobStatus.RUNNING.value
        current.submission_state = "CONFIRMED"
        current.provider_job_id = "remote-job-1"
    assert container.gateway.recover_after_restart() == 1
    recovered = container.gateway.get(job.id)
    assert recovered.status == JobStatus.SUBMITTED.value
    assert recovered.safe_to_retry is False

    with container.database.session() as session:
        current = session.get(type(job), job.id)
        current.provider_job_id = None
        current.submission_state = "SENT_UNCONFIRMED"
        current.status = JobStatus.RUNNING.value
    container.gateway.recover_after_restart()
    uncertain = container.gateway.get(job.id)
    assert uncertain.status == JobStatus.WORKER_NEEDS_USER_ACTION.value
    assert uncertain.safe_to_retry is False


def test_late_browser_response_is_reconciled_without_resubmission(container, project):
    request = GenerationRequest(
        project_id=project.id,
        type="video",
        prompt="One action",
        idempotency_key="orphan-response-1",
    )
    job, _ = container.gateway.create(request)
    with container.database.session() as session:
        current = session.get(type(job), job.id)
        current.status = JobStatus.WORKER_NEEDS_USER_ACTION.value
        current.submission_state = "SENT_UNCONFIRMED"
        current.safe_to_retry = False
        session.add(
            BrowserWorker(
                id="late-worker",
                provider="google_flow",
                connection_id="late-connection",
                capabilities=["video"],
            )
        )
        session.add(
            WorkerCommand(
                worker_id="late-worker",
                generation_job_id=job.id,
                message_type="provider.request",
                payload={},
                status="COMPLETED",
                response={"status": 200, "data": {"media": [{"name": "late-provider-job"}]}},
            )
        )
    reconciled = container.gateway.reconcile(job.id)
    assert reconciled.provider_job_id == "late-provider-job"
    assert reconciled.status == JobStatus.SUBMITTED.value
