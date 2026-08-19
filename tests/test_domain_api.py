from fastapi.testclient import TestClient
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
        assert client.get("/v1/accounts").status_code == 200
        assert client.get("/v1/workers").status_code == 200
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


def test_director_and_prompt_compiler_skills_are_discoverable(container):
    with TestClient(create_app(container)) as client:
        skills = {item["name"] for item in client.get("/v1/skills").json()}
    assert {"director", "prompt-compiler"}.issubset(skills)
