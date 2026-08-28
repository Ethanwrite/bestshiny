"""Series continuation: EP01 -> EP02 with the right things inherited.

The five continuity classes are the contract under test:

- narrative and character state always cross the boundary (ledger facts,
  open obligations, state heads);
- the visual layer (style lock, visual bible) always crosses;
- scene and frame state cross only on a declared CONTINUOUS pickup in the
  same location - a time or location jump reconstructs its first frame and
  must not inherit the previous episode's tail frame, scene or lighting;
- and the boundary is a contract: an episode someone continues from cannot
  be silently recompiled out from under the continuation.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from production_domain.models import (
    Episode,
    EpisodeContinuation,
    MediaAsset,
    Scene,
    Shot,
    ShotDependency,
    ShotDependencyType,
    TimelineTransition,
)
from sqlalchemy import select
from video_platform_api.main import create_app

EP1_SCRIPT = "INT. Rooftop - NIGHT\nMira picks up the phone.\nMira: You finally came."


def _client(container) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(create_app(container))


@pytest.fixture
def episode_one(container, project):
    """EP01 compiled through the ordinary chain, with an open obligation."""

    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="EP01",
            episode_number=1,
            script_source=EP1_SCRIPT,
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    container.orchestrator.compile_episode(episode_id)
    container.narrative_ledger.establish_fact(
        project.id,
        fact_key="phone-is-not-hers",
        summary="The phone Mira holds does not belong to her",
        episode=1,
    )
    container.narrative_ledger.open_obligation(
        project.id,
        obligation_key="ep1:whose-phone",
        promise="Reveal whose phone Mira found",
        episode=1,
    )
    return episode_id


def _last_shot_id(container, episode_id: str) -> str:
    with container.database.session() as session:
        rows = session.execute(
            select(Shot.id)
            .join(Scene, Shot.scene_id == Scene.id)
            .where(Scene.episode_id == episode_id)
            .order_by(Scene.sequence, Shot.sequence)
        ).all()
        return rows[-1][0]


def _give_end_frame(container, project_id: str, shot_id: str) -> str:
    with container.database.session() as session:
        asset = MediaAsset(
            project_id=project_id,
            asset_type="frame",
            sha256=hashlib.sha256(shot_id.encode()).hexdigest(),
            storage_key=f"frames/{shot_id}.png",
            mime_type="image/png",
            size_bytes=128,
        )
        session.add(asset)
        session.flush()
        shot = session.get(Shot, shot_id)
        shot.end_frame_asset_id = asset.id
        session.flush()
        return asset.id


async def test_prepare_computes_the_context_and_is_idempotent(container, project, episode_one):
    view = await container.episode_continuations.prepare(
        project.id, previous_episode_id=episode_one, continuation_mode="CONTINUOUS"
    )
    context = view["context"]
    assert context["previous_episode"]["number"] == 1
    assert context["ending"]["location"]["name"] == "Rooftop"
    assert "Reveal whose phone Mira found" in context["narrative"]["open_obligations"]
    assert any(
        "does not belong to her" in fact
        for fact in context["narrative"]["known_facts"].get("AUDIENCE", [])
    )
    assert any(character["name"] == "Mira" for character in context["characters"])
    assert context["continuity_classes"] == {
        "narrative": "INHERIT",
        "character": "INHERIT",
        "scene": "INHERIT",
        "visual": "INHERIT",
        "frame": "INHERIT",
    }
    assert view["brief"]["carried_obligations"] == ["Reveal whose phone Mira found"]
    assert view["beats"][0]["intent"] == "PICKUP"
    assert view["beats"][0]["location"] == "Rooftop"
    assert any(beat["intent"] == "ADVANCE_OBLIGATION" for beat in view["beats"])
    assert view["reasoner"] == "DETERMINISTIC"

    replay = await container.episode_continuations.prepare(
        project.id, previous_episode_id=episode_one, continuation_mode="CONTINUOUS"
    )
    assert replay["id"] == view["id"]
    assert replay["revision"] == view["revision"] == 1

    regenerated = await container.episode_continuations.prepare(
        project.id,
        previous_episode_id=episode_one,
        continuation_mode="TIME_JUMP",
        time_gap="three days later",
        new_location="Tokyo",
        regenerate=True,
    )
    assert regenerated["id"] == view["id"]
    assert regenerated["revision"] == 2
    assert regenerated["continuation_mode"] == "TIME_JUMP"
    assert regenerated["context"]["continuity_classes"]["frame"] == "RESET"


async def test_a_discontinuity_must_say_what_jumped(container, project, episode_one):
    from episode_continuation_core import EpisodeContinuationConflict

    with pytest.raises(EpisodeContinuationConflict, match="what jumped"):
        await container.episode_continuations.prepare(
            project.id, previous_episode_id=episode_one, continuation_mode="TIME_JUMP"
        )


async def test_continuous_confirm_links_the_boundary_and_inherits_the_tail_frame(
    container, project, episode_one
):
    tail_shot = _last_shot_id(container, episode_one)
    end_frame = _give_end_frame(container, project.id, tail_shot)
    view = await container.episode_continuations.prepare(
        project.id, previous_episode_id=episode_one, continuation_mode="CONTINUOUS"
    )
    result = container.episode_continuations.confirm(view["id"], actor="test-user")
    assert result["status"] == "COMPILED"
    episode_two = result["compiled"]["episode_id"]

    with container.database.session() as session:
        first_shot_id = (
            session.execute(
                select(Shot.id)
                .join(Scene, Shot.scene_id == Scene.id)
                .where(Scene.episode_id == episode_two)
                .order_by(Scene.sequence, Shot.sequence)
            )
            .first()[0]
        )
        first_shot = session.get(Shot, first_shot_id)
        tail = session.get(Shot, tail_shot)
        assert first_shot.previous_shot_id == tail_shot
        assert tail.next_shot_id == first_shot_id
        transition = session.scalar(
            select(TimelineTransition).where(TimelineTransition.target_shot_id == first_shot_id)
        )
        assert transition.transition_type == "CONTINUOUS"
        assert transition.metadata_json["source"] == "episode_continuation"
        dependency = session.scalar(
            select(ShotDependency).where(ShotDependency.target_shot_id == first_shot_id)
        )
        assert dependency is not None
        assert dependency.dependency_type == ShotDependencyType.STATE_INHERITANCE.value
        assert dependency.source_shot_id == tail_shot
        # Same location row on both sides of the boundary: the scene rows are
        # per-episode containers, the location is the scene identity.
        ep1_scene = session.get(Scene, tail.scene_id)
        ep2_scene = session.get(Scene, first_shot.scene_id)
        assert ep1_scene.location_id == ep2_scene.location_id
        # The planner planned the pair as a continuous chain and wired the
        # previous episode's tail frame as the start frame.
        assert first_shot.continuity_mode == "HARD_CONTINUITY"
        assert first_shot.start_frame_asset_id == end_frame
        plan = transition.metadata_json["frame_anchor_plan"]
        assert plan["strategy"] == "INHERIT_LAST_FRAME"
        assert "CROSS_SCENE_CONTINUOUS" in plan["reasons"]

    # Confirm replays idempotently.
    replay = container.episode_continuations.confirm(view["id"], actor="test-user")
    assert replay["compiled"]["episode_id"] == episode_two
    with container.database.session() as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(Episode).where(Episode.project_id == project.id)
                    )
                )
            )
            == 2
        )


async def test_time_jump_resets_frame_and_scene_but_keeps_narrative(
    container, project, episode_one
):
    tail_shot = _last_shot_id(container, episode_one)
    _give_end_frame(container, project.id, tail_shot)
    view = await container.episode_continuations.prepare(
        project.id,
        previous_episode_id=episode_one,
        continuation_mode="TIME_JUMP",
        time_gap="three days later",
        new_location="Tokyo",
    )
    assert view["beats"][0]["location"] == "Tokyo"
    result = container.episode_continuations.confirm(view["id"], actor="test-user")
    episode_two = result["compiled"]["episode_id"]

    with container.database.session() as session:
        first_shot = session.get(
            Shot,
            session.execute(
                select(Shot.id)
                .join(Scene, Shot.scene_id == Scene.id)
                .where(Scene.episode_id == episode_two)
                .order_by(Scene.sequence, Shot.sequence)
            )
            .first()[0],
        )
        # The link exists - the story continues - but nothing of the old
        # scene or its tail frame crossed the jump.
        assert first_shot.previous_shot_id == tail_shot
        assert first_shot.start_frame_asset_id is None
        transition = session.scalar(
            select(TimelineTransition).where(TimelineTransition.target_shot_id == first_shot.id)
        )
        assert transition.transition_type == "TIME_JUMP"
        # The continuation decision reconciled the jump through the existing
        # engine: nothing is left stale and the reason is recorded.
        assert transition.reconciliation_required is False
        assert transition.metadata_json["reconciled"] is True
        assert "three days later" in transition.metadata_json["reconciliation_reason"]
        assert first_shot.downstream_state_stale is False
        dependency = session.scalar(
            select(ShotDependency).where(ShotDependency.target_shot_id == first_shot.id)
        )
        assert dependency is None
        plan = transition.metadata_json["frame_anchor_plan"]
        assert plan["strategy"] == "RECONSTRUCT_FIRST_FRAME"
        ep1_scene_location = session.get(Scene, session.get(Shot, tail_shot).scene_id).location_id
        ep2_scene_location = session.get(Scene, first_shot.scene_id).location_id
        assert ep1_scene_location != ep2_scene_location
        first_shot_id = first_shot.id

    # Narrative continuity still crosses: the compiled prompt for the new
    # episode's first shot carries episode 1's facts and open obligation.
    compiled = container.prompts.compile(first_shot_id)
    assert "Reveal whose phone Mira found" in compiled.neutral_prompt
    assert "does not belong to her" in compiled.neutral_prompt


async def test_the_continued_episode_cannot_be_recompiled_out_from_under_the_link(
    container, project, episode_one
):
    view = await container.episode_continuations.prepare(
        project.id,
        previous_episode_id=episode_one,
        continuation_mode="TIME_JUMP",
        time_gap="three days later",
        new_location="Tokyo",
    )
    result = container.episode_continuations.confirm(view["id"], actor="test-user")
    episode_two = result["compiled"]["episode_id"]

    with container.database.session() as session:
        ep1 = session.get(Episode, episode_one)
        ep1.script_source = EP1_SCRIPT + "\nMira turns."
        ep2 = session.get(Episode, episode_two)
        ep2.script_source = ep2.script_source + "\nMira stops."
        session.flush()
    with pytest.raises(RuntimeError, match="later episode continues"):
        container.narrative.compile_episode(episode_one)
    with pytest.raises(RuntimeError, match="linked continuation"):
        container.narrative.compile_episode(episode_two)


async def test_an_unrelated_episode_number_conflict_is_refused(container, project, episode_one):
    from episode_continuation_core import EpisodeContinuationConflict

    with container.database.session() as session:
        session.add(
            Episode(
                project_id=project.id,
                title="Handwritten EP02",
                episode_number=2,
                script_source="INT. Bar - NIGHT\nMira sits.",
            )
        )
        session.flush()
    with pytest.raises(EpisodeContinuationConflict, match="already exists"):
        await container.episode_continuations.prepare(
            project.id, previous_episode_id=episode_one, continuation_mode="CONTINUOUS"
        )


def test_episode_strip_and_continuation_api_round_trip(container, project, episode_one):
    with _client(container) as client:
        strip = client.get(f"/v1/projects/{project.id}/episodes")
        assert strip.status_code == 200
        episodes = strip.json()
        assert len(episodes) == 1
        assert episodes[0]["episode_number"] == 1
        assert episodes[0]["display_status"] == "COMPILED"
        assert episodes[0]["shot_count"] == 2
        assert episodes[0]["continuation"] is None

        prepared = client.post(
            f"/v1/episodes/{episode_one}/continuations",
            json={
                "continuation_mode": "TIME_JUMP",
                "time_gap": "three days later",
                "new_location": "Tokyo",
            },
        )
        assert prepared.status_code == 201, prepared.text
        continuation_id = prepared.json()["id"]
        assert prepared.json()["status"] == "BRIEF_PROPOSED"

        fetched = client.get(f"/v1/continuations/{continuation_id}")
        assert fetched.status_code == 200
        assert fetched.json()["brief"]["episode_number"] == 2

        confirmed = client.post(
            f"/v1/continuations/{continuation_id}/confirm", json={"title": "EP02 - Tokyo"}
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "COMPILED"
        assert confirmed.json()["compiled"]["shot_count"] >= 2

        strip_after = client.get(f"/v1/projects/{project.id}/episodes").json()
        assert [entry["episode_number"] for entry in strip_after] == [1, 2]
        assert strip_after[0]["continuation"]["status"] == "COMPILED"
        assert strip_after[1]["title"] == "EP02 - Tokyo"

    with container.database.session() as session:
        row = session.get(EpisodeContinuation, continuation_id)
        assert row.confirmed_by == "development-bypass"
        assert row.script_rendered.splitlines()[0].endswith("Tokyo - DAY")
