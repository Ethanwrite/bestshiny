"""Deleting a shot the user created (2026-09-02).

A shot with no paid history is removed and its neighbours re-joined; a shot
that has been generated on is refused with the reason, because jobs, credits
and cost records reference it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from production_domain.models import GenerationJob, Shot
from sqlalchemy import select
from video_platform_api.main import create_app


def _episode_with_shots(client: TestClient, project_id: str, count: int = 3) -> tuple[str, list[str]]:
    episode = client.post(
        "/v1/episodes", json={"project_id": project_id, "title": "Deletable", "episode_number": 1}
    )
    assert episode.status_code == 200, episode.text
    scene = client.post("/v1/scenes", json={"episode_id": episode.json()["id"], "sequence": 1})
    assert scene.status_code == 200, scene.text
    shots: list[str] = []
    for sequence in range(1, count + 1):
        created = client.post(
            "/v1/shots",
            json={
                "scene_id": scene.json()["id"],
                "sequence": sequence,
                "prompt": f"shot {sequence}",
                "previous_shot_id": shots[-1] if shots else None,
            },
        )
        assert created.status_code == 200, created.text
        shots.append(created.json()["id"])
    return episode.json()["id"], shots


def test_a_never_generated_shot_is_deleted_and_the_chain_is_rejoined(container, project):  # type: ignore[no-untyped-def]
    with TestClient(create_app(container)) as client:
        _episode_id, (first, middle, last) = _episode_with_shots(client, project.id)
        with container.database.session() as session:
            session.get(Shot, first).next_shot_id = middle
            session.get(Shot, middle).next_shot_id = last
        deleted = client.delete(f"/v1/shots/{middle}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted"] is True
        assert client.get(f"/v1/shots/{middle}").status_code == 404
        assert client.delete(f"/v1/shots/{middle}").status_code == 404
    with container.database.session() as session:
        assert session.get(Shot, middle) is None
        assert session.get(Shot, last).previous_shot_id == first
        assert session.get(Shot, first).next_shot_id == last


def test_a_generated_shot_is_refused_with_the_reason(container, project):  # type: ignore[no-untyped-def]
    with TestClient(create_app(container)) as client:
        _episode_id, (only,) = _episode_with_shots(client, project.id, count=1)
        with container.database.session() as session:
            job = GenerationJob(
                project_id=project.id,
                shot_id=only,
                generation_type="video",
                provider="seedance",
                model="doubao-seedance-2-5-260628",
                request_json={"prompt": "x"},
                request_hash="0" * 64,
            )
            session.add(job)
            session.flush()
        refused = client.delete(f"/v1/shots/{only}")
        assert refused.status_code == 409, refused.text
        assert "generation job" in refused.json()["detail"]
    with container.database.session() as session:
        assert session.scalar(select(Shot).where(Shot.id == only)) is not None
