from __future__ import annotations

import production_engine.runtime as runtime_module
import pytest
from fastapi.testclient import TestClient
from memory_core import GenerationContext
from platform_contracts import CanonicalShotSpec, CanonicalSubjectSpec, GenerationRequest
from production_domain.models import (
    Asset,
    AssetVersion,
    Episode,
    GenerationIdempotency,
    GenerationJob,
    ProductionTrace,
    Scene,
    Shot,
    TimelineState,
)
from production_engine.runtime import VisualProductionRuntime
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from video_platform_api.main import create_app


def test_passenger_uses_selected_model_and_shared_runtime_trace(container, project):
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/api/passenger/generate",
            json={
                "project_id": project.id,
                "media_type": "video",
                "provider": "google_flow",
                "model": "flow-veo-3.1",
                "prompt": "A profile portrait turns toward the door without looking at camera.",
                "duration": 8,
                "resolution": "720p",
                "idempotency_key": "passenger-selected-flow",
            },
        )
    assert response.status_code == 202
    assert response.json()["provider"] == "google_flow"
    assert response.json()["model"] == "flow-veo-3.1"
    assert response.json()["estimated_credits"] > 0
    with container.database.session() as session:
        trace = session.scalar(
            select(ProductionTrace).where(ProductionTrace.generation_job_id == response.json()["id"])
        )
        assert trace.mode == "PASSENGER_SEAT"
        assert trace.model_id == "flow-veo-3.1"


def test_passenger_idempotency_conflict_returns_409(container, project):
    app = create_app(container)
    base = {
        "project_id": project.id,
        "media_type": "video",
        "provider": "google_flow",
        "model": "flow-veo-3.1",
        "prompt": "One visible action.",
        "duration": 8,
        "idempotency_key": "passenger-conflict",
    }
    with TestClient(app) as client:
        assert client.post("/api/passenger/generate", json=base).status_code == 202
        conflict = client.post(
            "/api/passenger/generate",
            json={**base, "prompt": "A different visible action."},
        )

    assert conflict.status_code == 409
    assert "different request" in conflict.json()["detail"]


def test_job_and_trace_are_atomic_and_client_trace_id_is_ignored(container, project, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(runtime_module, "_new_trace_id", lambda: "server-trace-id")
    first_request = GenerationRequest(
        project_id=project.id,
        type="video",
        provider="google_flow",
        model="flow-veo-3.1",
        prompt="First action",
        idempotency_key="trace-atomic-first",
        metadata={"trace_id": "client-controlled-trace"},
    )
    first_job, _ = container.visual_runtime.submit(first_request, mode="PASSENGER_SEAT")
    with container.database.session() as session:
        trace = session.scalar(
            select(ProductionTrace).where(ProductionTrace.generation_job_id == first_job.id)
        )
        assert trace.trace_id == "server-trace-id"

    second_request = first_request.model_copy(
        update={
            "prompt": "Second action",
            "idempotency_key": "trace-atomic-second",
        }
    )
    with pytest.raises(IntegrityError):
        container.visual_runtime.submit(second_request, mode="PASSENGER_SEAT")

    with container.database.session() as session:
        assert session.scalar(select(func.count(GenerationJob.id))) == 1
        assert session.scalar(select(func.count(GenerationIdempotency.id))) == 1


def test_passenger_rejects_registered_but_unconfigured_provider(container, project):
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/api/passenger/generate",
            json={
                "project_id": project.id,
                "media_type": "video",
                "provider": "grok",
                "model": "grok-video",
                "prompt": "One action.",
                "duration": 8,
                "idempotency_key": "unconfigured-grok",
            },
        )
    assert response.status_code == 400
    assert "no configured generation transport" in response.json()["detail"]


def test_credit_pricing_is_explainable_and_scales_with_resolution(container):
    low = container.credit_pricing.estimate(
        provider="kling",
        model="kling-3.0",
        media_type="video",
        duration=8,
        resolution="720p",
    )
    high = container.credit_pricing.estimate(
        provider="kling",
        model="kling-3.0",
        media_type="video",
        duration=8,
        resolution="1080p",
        reference_count=2,
    )
    assert high.credits > low.credits
    assert high.resolution_multiplier == 1.3
    assert high.reference_multiplier == 1.08


def test_passenger_rejects_provider_model_mismatch(container, project):
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/api/passenger/generate",
            json={
                "project_id": project.id,
                "media_type": "video",
                "provider": "google_flow",
                "model": "kling-3.0",
                "prompt": "One action.",
                "duration": 8,
                "idempotency_key": "bad-provider-model",
            },
        )
    assert response.status_code == 400
    assert "not registered" in response.json()["detail"]


def test_video_compiler_is_canonical_and_provider_neutral(container, project):
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Pilot", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Hotel door")
        session.add(scene)
        session.flush()
        start = TimelineState(
            project_id=project.id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_INPUT",
            state_json={
                "characters": {
                    "Lin": {
                        "position": "screen-left",
                        "orientation": "rear three-quarter",
                        "eyeline_target": "the hotel door",
                    }
                },
                "camera": {"movement": "slow push", "axis": "A"},
            },
        )
        end = TimelineState(
            project_id=project.id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_OUTPUT",
            state_json={"characters": {"Lin": {"position": "at the door"}}},
        )
        session.add_all([start, end])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=1,
            shot_type="ACTION",
            prompt="Lin takes one step to the door.",
            user_prompt="Lin takes one step to the door.",
            input_state_id=start.id,
            output_state_id=end.id,
        )
        session.add(shot)
        session.flush()
        shot_id = shot.id

    compiled = container.video_prompt_compiler.compile(shot_id)
    assert compiled.spec.dominant_action == "Lin takes one step to the door."
    assert compiled.spec.subjects[0].eyeline_target == "the hotel door"
    assert compiled.spec.camera.dominant_movement == "slow push"
    assert "google_flow" not in compiled.neutral_prompt
    assert "grok" not in compiled.neutral_prompt.lower()


def test_runtime_detects_rear_view_from_approved_end_state():
    requirements = VisualProductionRuntime._requirements(
        CanonicalShotSpec(
            intent="turn away once",
            dominant_action="turn away once",
            subjects=[CanonicalSubjectSpec(body_orientation="front")],
            end_state={"characters": {"Lin": {"orientation": "rear view"}}},
        ),
        GenerationContext(),
        None,
    )
    assert requirements.requires_rear_view_ending is True


def test_video_compiler_distinguishes_approved_camera_gaze_from_a_prohibition(container):
    detector = container.video_prompt_compiler._camera_gaze_requested
    assert detector("她停下并直视镜头", {}, {}) is True
    assert detector("她看向门口，全程不要看向镜头", {}, {}) is False


def test_approved_camera_gaze_rewrites_only_the_default_eyeline_and_is_not_penalized(container, project):
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Direct gaze", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Studio")
        session.add(scene)
        session.flush()
        start = TimelineState(
            project_id=project.id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_INPUT",
            state_json={"characters": {"Lin": {"position": "center"}}},
        )
        session.add(start)
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=1,
            prompt="Lin stops and looks directly into the camera.",
            user_prompt="Lin stops and looks directly into the camera.",
            input_state_id=start.id,
        )
        session.add(shot)
        session.flush()
        shot_id = shot.id

    compiled = container.video_prompt_compiler.compile(shot_id)
    assert compiled.spec.allow_camera_gaze is True
    assert compiled.spec.subjects[0].eyeline_target == "camera lens as the explicitly approved target"
    assert "no subject acknowledges the camera" not in compiled.spec.constraints
    requirements = VisualProductionRuntime._requirements(compiled.spec, GenerationContext(), None)
    assert requirements.forbid_camera_gaze is False


def test_passenger_result_can_be_versioned_then_explicitly_promoted(container, project, register_bytes):
    with TestClient(create_app(container)) as client:
        first_job = client.post(
            "/api/passenger/generate",
            json={
                "project_id": project.id,
                "media_type": "image",
                "provider": "google_flow",
                "model": "NARWHAL",
                "prompt": "Hotel lobby reference",
                "idempotency_key": "scene-version-one",
            },
        ).json()
        first_media = register_bytes(container, project.id, "IMAGE", b"scene-one")
        with container.database.session() as session:
            session.get(GenerationJob, first_job["id"]).output_asset_id = first_media.id
        first = client.post(
            f"/api/generations/{first_job['id']}/promote",
            json={"asset_type": "SCENE", "name": "Hotel lobby"},
        )
        assert first.status_code == 200
        logical_id = first.json()["asset"]["id"]
        assert first.json()["canonical"] is False

        second_job = client.post(
            "/api/passenger/generate",
            json={
                "project_id": project.id,
                "media_type": "image",
                "provider": "google_flow",
                "model": "NARWHAL",
                "prompt": "Revised hotel lobby reference",
                "idempotency_key": "scene-version-two",
            },
        ).json()
        second_media = register_bytes(container, project.id, "IMAGE", b"scene-two")
        with container.database.session() as session:
            session.get(GenerationJob, second_job["id"]).output_asset_id = second_media.id
        second = client.post(
            f"/api/generations/{second_job['id']}/promote",
            json={
                "asset_id": logical_id,
                "asset_type": "SCENE",
                "name": "Hotel lobby",
                "promote_to_canonical": True,
            },
        )
    assert second.status_code == 200
    with container.database.session() as session:
        asset = session.get(Asset, logical_id)
        versions = list(
            session.scalars(
                select(AssetVersion).where(AssetVersion.asset_id == logical_id).order_by(AssetVersion.version)
            )
        )
        assert [item.version for item in versions] == [1, 2]
        assert asset.canonical_version_id == versions[1].id
