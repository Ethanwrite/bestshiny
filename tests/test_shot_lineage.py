from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from production_domain.models import Episode, Project, Scene, Shot
from sqlalchemy.exc import IntegrityError
from video_platform_api.main import create_app


def _two_project_scenes(container):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        first_project = Project(title="First")
        second_project = Project(title="Second")
        session.add_all([first_project, second_project])
        session.flush()
        first_episode = Episode(project_id=first_project.id, title="First", episode_number=1)
        second_episode = Episode(project_id=second_project.id, title="Second", episode_number=1)
        session.add_all([first_episode, second_episode])
        session.flush()
        first_scene = Scene(episode_id=first_episode.id, sequence=1)
        second_scene = Scene(episode_id=second_episode.id, sequence=1)
        session.add_all([first_scene, second_scene])
        session.flush()
        previous = Shot(scene_id=first_scene.id, sequence=1, prompt="First action")
        session.add(previous)
        session.flush()
        return first_scene.id, second_scene.id, previous.id


def test_database_rejects_cross_project_previous_shot(container) -> None:  # type: ignore[no-untyped-def]
    _first_scene_id, second_scene_id, previous_id = _two_project_scenes(container)

    with pytest.raises(IntegrityError, match="previous shot must belong to the same project"):
        with container.database.session() as session:
            session.add(
                Shot(
                    scene_id=second_scene_id,
                    sequence=2,
                    prompt="Second action",
                    previous_shot_id=previous_id,
                )
            )
            session.flush()


def test_create_shot_route_rejects_cross_project_previous_shot(container) -> None:  # type: ignore[no-untyped-def]
    _first_scene_id, second_scene_id, previous_id = _two_project_scenes(container)
    client = TestClient(create_app(container))

    response = client.post(
        "/v1/shots",
        json={
            "scene_id": second_scene_id,
            "sequence": 2,
            "prompt": "Second action",
            "previous_shot_id": previous_id,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "previous shot must belong to the same project"
