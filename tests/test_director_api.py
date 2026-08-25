from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
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


def test_a_stale_fence_is_still_a_conflict_when_no_competitor_claimed_the_key(container):
    """The race handling must not swallow genuine staleness.

    A stale fence stops being conclusive only because the key turns out to be
    claimed. With no claim there is no competitor, nothing to replay, and the
    caller does have to plan the shot again.
    """

    with TestClient(create_app(container)) as client:
        project = client.post("/v1/projects", json={"title": "Stale Fence"}).json()
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

        first = client.post(f"/v1/shots/{shot_id}/generate", json={"idempotency_key": "stale-fence-1"})
        assert first.status_code == 202

        # The shot is QUEUED now, so any plan made before it is stale. A second
        # request under a *different* key has no claim to fall back on.
        second = client.post(f"/v1/shots/{shot_id}/generate", json={"idempotency_key": "stale-fence-2"})
        assert second.status_code == 409
        assert "plan the shot again" in second.json()["detail"]

    with container.database.session() as session:
        assert (
            session.scalar(select(func.count(GenerationJob.id)).where(GenerationJob.shot_id == shot_id)) == 1
        )


@pytest.mark.postgres_only
def test_a_duplicate_request_replays_when_the_competitor_moved_the_shot(container):
    """The §2.20 race, forced deterministically rather than left to scheduling.

    The fence is evaluated under the Shot's row lock, so the loser of a race
    reads the Shot only after the winner has committed — and reads it QUEUED.
    The fence is then stale against a change the loser's own duplicate caused.

    The window is opened directly here: the claim is hidden from the opening
    lookup and committed by a separate transaction while the fence is being
    checked, which is what a competitor's commit does. That needs a reader that
    sees another transaction's commit mid-transaction, so it needs PostgreSQL —
    under SQLite's snapshot the reinstated row stays invisible, which is the
    same reason the defect could not occur there in the first place.
    """

    from generation_gateway import GenerationGateway

    with TestClient(create_app(container)) as client:
        project = client.post("/v1/projects", json={"title": "Forced Race"}).json()
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

        winner = client.post(f"/v1/shots/{shot_id}/generate", json={"idempotency_key": "forced-race"})
        assert winner.status_code == 202
        assert winner.json()["replayed"] is False
        winning_job_id = winner.json()["generation_job_id"]

        # Detach the winner's claim so the loser's opening lookup misses it,
        # and keep the row to reinstate at the moment the fence is read.
        with container.database.session() as session:
            claim = session.scalar(
                select(GenerationIdempotency).where(
                    GenerationIdempotency.project_id == project["id"],
                    GenerationIdempotency.key == "forced-race",
                )
            )
            claim_fields = {
                "project_id": claim.project_id,
                "key": claim.key,
                "request_hash": claim.request_hash,
                "generation_job_id": claim.generation_job_id,
                "status": claim.status,
            }
            session.delete(claim)

        original_validate = GenerationGateway._validate_timeline_fence_in_session
        reinstated = []

        def competitor_commit_becomes_visible(self, session, request, shot, fence):  # type: ignore[no-untyped-def]
            if not reinstated:
                reinstated.append(True)
                # A separate session, because this is a separate transaction in
                # the situation being reproduced. It touches only the claim
                # table, so it cannot contend with the Shot lock held here.
                with container.database.session() as other:
                    other.add(GenerationIdempotency(**claim_fields))
            return original_validate(self, session, request, shot, fence)

        GenerationGateway._validate_timeline_fence_in_session = (  # type: ignore[method-assign]
            competitor_commit_becomes_visible
        )
        try:
            loser = client.post(f"/v1/shots/{shot_id}/generate", json={"idempotency_key": "forced-race"})
        finally:
            GenerationGateway._validate_timeline_fence_in_session = (  # type: ignore[method-assign]
                original_validate
            )

        assert reinstated, "the fence was never evaluated, so the race was not reproduced"
        # Before the fix this was 409 "plan the shot again" for a request whose
        # own duplicate was already running.
        assert loser.status_code == 202
        assert loser.json()["replayed"] is True
        assert loser.json()["generation_job_id"] == winning_job_id

    with container.database.session() as session:
        assert (
            session.scalar(select(func.count(GenerationJob.id)).where(GenerationJob.shot_id == shot_id)) == 1
        )
        assert (
            session.scalar(
                select(func.count(GenerationCandidate.id)).where(GenerationCandidate.shot_id == shot_id)
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
