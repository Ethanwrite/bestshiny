from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from PIL import Image
from production_domain.models import (
    ContinuityMode,
    GenerationCandidate,
    GenerationIdempotency,
    GenerationJob,
    Shot,
)
from sqlalchemy import event, func, select
from video_platform_api.main import create_app


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format="PNG")
    return output.getvalue()


def test_web_director_api_script_to_candidate_flow(container):
    with TestClient(create_app(container)) as client:
        project = client.post(
            "/v1/projects",
            json={"title": "Rainy Night", "default_provider": "google_flow"},
        )
        assert project.status_code == 200
        project_id = project.json()["id"]
        episode = client.post(
            f"/v1/projects/{project_id}/episodes",
            json={
                "project_id": project_id,
                "title": "Pilot",
                "episode_number": 1,
                "script_source": "EXT. HOTEL - NIGHT\nLinJin turns toward the door.",
            },
        )
        assert episode.status_code == 200
        compiled = client.post(f"/v1/episodes/{episode.json()['id']}/compile")
        assert compiled.status_code == 200
        shot_id = compiled.json()["shot_ids"][0]
        shot = client.get(f"/v1/shots/{shot_id}")
        assert shot.status_code == 200
        assert shot.json()["input_state"] is not None
        generated = client.post(
            f"/v1/shots/{shot_id}/generate",
            json={"idempotency_key": "director-shot-1", "estimated_cost": 0.8},
        )
        assert generated.status_code == 202
        assert generated.json()["replayed"] is False
        replay = client.post(
            f"/v1/shots/{shot_id}/generate",
            json={"idempotency_key": "director-shot-1", "estimated_cost": 0.8},
        )
        assert replay.status_code == 202
        assert replay.json()["replayed"] is True
        candidates = client.get(f"/v1/shots/{shot_id}/candidates").json()
        assert len(candidates) == 1
        assert candidates[0]["generation_plan"]["provider"] == "google_flow"
        assert candidates[0]["cost"] > 0
        assert candidates[0]["cost_source"] == "ESTIMATED"
        assert candidates[0]["known_actual_cost"] == 0
        assert candidates[0]["estimated_fallback_cost"] == candidates[0]["cost"]


def test_concurrent_generate_requests_replay_one_candidate_and_job(container):
    app = create_app(container)
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"title": "Concurrent Director"}).json()
        episode = client.post(
            f"/v1/projects/{project['id']}/episodes",
            json={
                "project_id": project["id"],
                "title": "Pilot",
                "episode_number": 1,
                "script_source": "INT. STUDIO - NIGHT\nMina raises one hand.",
            },
        ).json()
        shot_id = client.post(f"/v1/episodes/{episode['id']}/compile").json()["shot_ids"][0]

    barrier = threading.Barrier(2)
    seen = threading.local()

    def synchronize_initial_lookup(execute_state) -> None:  # type: ignore[no-untyped-def]
        if (
            execute_state.is_select
            and "generation_idempotency" in str(execute_state.statement)
            and not getattr(seen, "initial_lookup", False)
        ):
            seen.initial_lookup = True
            barrier.wait(timeout=5)

    def submit(_index: int):  # type: ignore[no-untyped-def]
        with TestClient(app) as client:
            return client.post(
                f"/v1/shots/{shot_id}/generate",
                json={"idempotency_key": "same-shot-concurrent-request"},
            )

    event.listen(container.database.Session, "do_orm_execute", synchronize_initial_lookup)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(submit, range(2)))
    finally:
        event.remove(container.database.Session, "do_orm_execute", synchronize_initial_lookup)

    assert [response.status_code for response in responses] == [202, 202]
    bodies = [response.json() for response in responses]
    assert bodies[0]["id"] == bodies[1]["id"]
    assert sorted(body["replayed"] for body in bodies) == [False, True]
    with container.database.session() as session:
        assert (
            session.scalar(
                select(func.count(GenerationCandidate.id)).where(GenerationCandidate.shot_id == shot_id)
            )
            == 1
        )
        assert (
            session.scalar(select(func.count(GenerationJob.id)).where(GenerationJob.shot_id == shot_id)) == 1
        )
        assert (
            session.scalar(
                select(func.count(GenerationIdempotency.id)).where(
                    GenerationIdempotency.project_id == project["id"]
                )
            )
            == 1
        )


def test_prompt_refine_reports_diff_without_fact_mutation(container, project):
    with TestClient(create_app(container)) as client:
        result = client.post(
            "/v1/prompts/refine",
            json={"project_id": project.id, "prompt": "LinJin   raises the phone!!"},
        )
        assert result.status_code == 200
        body = result.json()
        assert "LinJin raises the phone" in body["refined"]
        assert body["preserved_facts"][0].startswith("subject, action")
        assert body["changes"][0]["category"] == "visual_specificity"


def test_character_creation_and_identity_confirmation_api(container, project):
    with TestClient(create_app(container)) as client:
        character = client.post(
            "/v1/characters",
            json={"project_id": project.id, "name": "Lin Jin", "description": "Blue jacket"},
        )
        assert character.status_code == 200
        asset = client.post(
            "/v1/assets",
            data={
                "project_id": project.id,
                "asset_type": "CHARACTER_MASTER",
                "character_id": character.json()["id"],
            },
            files={"file": ("lin.png", io.BytesIO(_png_bytes((20, 80, 180))), "image/png")},
        )
        assert asset.status_code == 200
        confirmed = client.post(
            f"/v1/characters/{character.json()['id']}/confirm-identity",
            json={
                "master_asset_id": asset.json()["id"],
                "hair_signature": "black bob",
                "costume_signature": "blue jacket",
            },
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["status"] == "LOCKED"
        second = client.post(
            "/v1/assets",
            data={
                "project_id": project.id,
                "asset_type": "CHARACTER_MASTER",
                "character_id": character.json()["id"],
            },
            files={"file": ("lin-v2.png", io.BytesIO(_png_bytes((30, 90, 190))), "image/png")},
        )
        confirmed_v2 = client.post(
            f"/v1/characters/{character.json()['id']}/confirm-identity",
            json={"master_asset_id": second.json()["id"]},
        )
        assert confirmed_v2.json()["version"] == 2
        characters = client.get(f"/v1/projects/{project.id}/characters").json()
        assert [item["version"] for item in characters[0]["identity_versions"]] == [1, 2]


def test_continuity_and_observability_endpoints(container, project):
    with TestClient(create_app(container)) as client:
        episode = client.post(
            "/v1/episodes",
            json={"project_id": project.id, "title": "Pilot", "episode_number": 1},
        ).json()
        scene = client.post(
            "/v1/scenes",
            json={"episode_id": episode["id"], "sequence": 1, "description": "Room"},
        ).json()
        shot = client.post(
            "/v1/shots",
            json={"scene_id": scene["id"], "sequence": 1, "prompt": "One turn."},
        ).json()
        decision = client.post(
            f"/v1/shots/{shot['id']}/continuity",
            json={"project_id": project.id, "risk": {"camera_axis_delta": 0.9}},
        )
        assert decision.status_code == 200
        assert decision.json()["mode"] == "RE_ANCHOR"
        assert decision.json()["require_new_keyframe"] is True
        with container.database.session() as session:
            stored_shot = session.get(Shot, shot["id"])
            assert stored_shot.continuity_mode == ContinuityMode.RE_ANCHOR.value
        records = client.get(f"/v1/shots/{shot['id']}/decisions")
        assert records.status_code == 200
        assert records.json()[0]["reason_codes"] == ["CAMERA_AXIS_CHANGE"]
