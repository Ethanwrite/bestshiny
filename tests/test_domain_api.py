import pytest
from fastapi.testclient import TestClient
from production_domain.models import GenerationJob
from sqlalchemy import func, select
from video_platform_api.main import create_app


def test_project_episode_scene_shot_flow(container):
    with TestClient(create_app(container)) as client:
        project = client.post("/v1/projects", json={"title": "Vertical Drama"})
        assert project.status_code == 200
        project_id = project.json()["id"]
        episode = client.post(
            "/v1/episodes",
            json={"project_id": project_id, "title": "Pilot", "episode_number": 1},
        ).json()
        scene = client.post(
            "/v1/scenes",
            json={"episode_id": episode["id"], "sequence": 1, "description": "Subway platform"},
        ).json()
        shot = client.post(
            "/v1/shots",
            json={
                "scene_id": scene["id"],
                "sequence": 1,
                "prompt": "The woman turns toward the arriving train.",
                "continuity_mode": "NONE",
            },
        )
        assert shot.status_code == 200
        assert shot.json()["scene_id"] == scene["id"]


def test_required_generation_routes_exist(container, project):
    with TestClient(create_app(container)) as client:
        body = {
            "project_id": project.id,
            "type": "video",
            "provider": "google_flow",
            "model": "veo",
            "prompt": "One visible action.",
            "duration": 8,
            "idempotency_key": "ep1-shot1-v1",
        }
        created = client.post("/v1/generations", json=body)
        assert created.status_code == 202
        job_id = created.json()["id"]
        assert client.get(f"/v1/generations/{job_id}").status_code == 200
        assert client.get("/v1/providers").status_code == 200
        internal_headers = {"Authorization": "Bearer test-platform-key"}
        assert client.get("/v1/accounts", headers=internal_headers).status_code == 200
        assert client.get("/v1/workers", headers=internal_headers).status_code == 200
        assert client.get("/health").json()["ok"] is True


def test_openai_compatibility_is_only_an_adapter(container, project):
    with TestClient(create_app(container)) as client:
        response = client.post(
            "/v1/videos/generations",
            headers={"Idempotency-Key": "openai-adapter-1"},
            json={"project_id": project.id, "prompt": "A locked-off establishing shot."},
        )
        assert response.status_code == 202
        assert response.json()["object"] == "video.generation"


@pytest.mark.parametrize(
    ("path", "headers", "body", "error_fragment"),
    [
        (
            "/v1/generations",
            {},
            {
                "project_id": "PROJECT_ID",
                "type": "video",
                "provider": "unknown-provider",
                "model": "unknown-model",
                "prompt": "One action.",
                "idempotency_key": "reject-unknown-provider",
            },
            "provider is not registered",
        ),
        (
            "/v1/generations",
            {},
            {
                "project_id": "PROJECT_ID",
                "type": "video",
                "provider": "grok",
                "model": "grok-video",
                "prompt": "One action.",
                "idempotency_key": "reject-unavailable-provider",
            },
            "no configured generation transport",
        ),
        (
            "/v1/images/generations",
            {"Idempotency-Key": "reject-unknown-image-model"},
            {
                "project_id": "PROJECT_ID",
                "provider": "google_flow",
                "model": "invented-image-model",
                "prompt": "One product.",
            },
            "model is not registered",
        ),
        (
            "/v1/videos/generations",
            {"Idempotency-Key": "reject-mismatched-video-model"},
            {
                "project_id": "PROJECT_ID",
                "provider": "google_flow",
                "model": "grok-video",
                "prompt": "One action.",
            },
            "model is not registered",
        ),
    ],
)
def test_generation_entrypoints_reject_unregistered_or_unavailable_targets(
    container,
    project,
    path,
    headers,
    body,
    error_fragment,
):
    body = {**body, "project_id": project.id}
    with TestClient(create_app(container)) as client:
        response = client.post(path, headers=headers, json=body)

    assert response.status_code == 400
    assert error_fragment in response.json()["detail"]
    with container.database.session() as session:
        assert session.scalar(select(func.count(GenerationJob.id))) == 0


def test_director_and_prompt_compiler_skills_are_discoverable(container):
    with TestClient(create_app(container)) as client:
        skills = {item["name"] for item in client.get("/v1/skills").json()}
    assert {"director", "prompt-compiler"}.issubset(skills)
