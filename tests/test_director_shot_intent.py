"""The director's shot intent has to reach the model, not just the audit log.

`apply_shot_intents` wrote back exactly two fields - shot type and duration -
so the action description the director staged, the start and end states, the
gaze target, the per-shot continuity obligations and the key visuals the shot
was bound to survived only in `creative_shot_lineage.intent_json`, which
nothing in the generation path reads. The prompt the model finally saw was
compiled from the parsed action line alone.

These tests follow one approved ShotIntent all the way: onto the Shot row and
its TimelineStates, into the CanonicalShotSpec the prompt compiler builds, into
the lines every provider adapter renders, and into the GenerationRequest's
reference set.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from production_domain.models import Character, Episode, Shot, TimelineState
from sqlalchemy import select
from test_creative_director import (
    RICH_IDEA,
    SCREENPLAY,
    ScriptedDirector,
    _approve_brief,
    _approve_screenplay,
    _client,
    _complete_visuals,
    _registered_pro,
    _rich_turn,
    _state,
    _wire_openrouter_images,
)
from test_creative_director import (
    openrouter_container as openrouter_container,  # re-exported so the fixture resolves here
)

GAZE = "the phone screen"
STAGING = "hesitant, half-turned away from the ledge"
OBLIGATION = "the phone stays on the ledge until picked up"


def _screenplay_with_staging() -> dict[str, Any]:
    content = copy.deepcopy(SCREENPLAY)
    first = content["beats"][0]["shots"][0]
    first["gaze_target"] = GAZE
    first["action"]["description"] = STAGING
    first["continuity_obligations"] = [OBLIGATION]
    first["start_state"] = "rooftop empty, phone glowing on the ledge"
    first["end_state"] = "Mira at the ledge, phone still down"
    return content


async def _compiled_session(container, client, headers, project_id):  # type: ignore[no-untyped-def]
    started = client.post(
        "/v1/creative/sessions", headers=headers, json={"project_id": project_id, "idea": RICH_IDEA}
    ).json()
    session_id = started["session_id"]
    _approve_brief(client, session_id, started["brief_revision"], headers)
    _approve_screenplay(client, session_id, headers)
    await _complete_visuals(container, client, session_id, headers)
    bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose", headers=headers).json()
    locked = client.post(
        f"/v1/creative/sessions/{session_id}/bible/approve",
        headers=headers,
        json={"version": bible["version"]},
    )
    assert locked.status_code == 200, locked.text
    client.post(f"/v1/creative/sessions/{session_id}/beats/propose", headers=headers)
    compiled = client.post(
        f"/v1/creative/sessions/{session_id}/beats/approve",
        headers=headers,
        json={"plan_revision": 1},
    )
    assert compiled.status_code == 200, compiled.text
    return session_id, compiled.json()


@pytest.mark.asyncio
async def test_the_approved_intent_lands_on_the_shot_and_its_timeline_states(openrouter_container):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(
        _rich_turn, screenplay=_screenplay_with_staging()
    )
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "intent-shot@example.com")
        session_id, compiled = await _compiled_session(container, client, headers, project_id)
        view = _state(client, session_id, headers)

    anchor_media = {
        anchor["anchor_key"]: anchor["media_asset_id"]
        for anchor in view["anchors"]
        if anchor["media_asset_id"]
    }
    first_shot_id = compiled["shot_ids"][0]
    with container.database.session() as session:
        shot = session.get(Shot, first_shot_id)
        intent = dict(shot.director_intent_json)
        input_state = session.get(TimelineState, shot.input_state_id)
        output_state = session.get(TimelineState, shot.output_state_id)

    # Everything the director approved, not just shot type and duration.
    assert intent["gaze_target"] == GAZE
    assert intent["description"] == STAGING
    assert intent["continuity_obligations"] == [OBLIGATION]
    assert intent["start_state"] == "rooftop empty, phone glowing on the ledge"
    assert intent["end_state"] == "Mira at the ledge, phone still down"
    assert "character:mira" in intent["anchors"]
    # Anchor bindings resolved to real reference media, not names.
    assert anchor_media["character:mira"] in intent["reference_asset_ids"]
    assert anchor_media["style:master"] in intent["reference_asset_ids"]
    # And every field is traceable to one screenplay revision, beat and shot.
    assert intent["screenplay_id"]
    assert intent["beat_sequence"] == 1 and intent["shot_sequence"] == 1

    # The states the compiler reads carry the director's own wording beside
    # the authoritative state, never over it.
    assert input_state.state_json["director_staging"] == intent["start_state"]
    assert output_state.state_json["director_staging"] == intent["end_state"]
    assert "characters" in input_state.state_json


@pytest.mark.asyncio
async def test_the_intent_reaches_the_canonical_shot_spec_and_every_adapter_line(
    openrouter_container,
):
    from video_adapter_core.base import canonical_lines

    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(
        _rich_turn, screenplay=_screenplay_with_staging()
    )
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "intent-spec@example.com")
        _session_id, compiled = await _compiled_session(container, client, headers, project_id)


    first_shot_id = compiled["shot_ids"][0]
    with container.database.session() as session:
        mira = session.scalar(
            select(Character).where(Character.project_id == project_id, Character.name == "Mira")
        )
        mira_id = mira.id
    result = container.video_prompt_compiler.compile(
        first_shot_id, character_bindings=[container.characters.binding(mira_id)]
    )
    spec = result.spec

    # Gaze: the director decides where the shot looks.
    assert spec.subjects, spec.model_dump()
    assert any(subject.eyeline_target == GAZE for subject in spec.subjects), [
        subject.eyeline_target for subject in spec.subjects
    ]
    # Staging enters the instruction without replacing the approved action.
    assert f"stage the approved action as: {STAGING}" in spec.constraints
    assert spec.dominant_action and STAGING not in spec.dominant_action
    # The continuity the shot owes is a constraint AND a continuity fact.
    assert f"continuity obligation: {OBLIGATION}" in spec.constraints
    assert any(OBLIGATION in str(fact) for fact in spec.continuity.get("facts", []))
    # The director's staging rides beside the authoritative state.
    assert spec.start_state["director_staging"] == "rooftop empty, phone glowing on the ledge"
    assert spec.end_state["director_staging"] == "Mira at the ledge, phone still down"

    # Every provider adapter renders the same spec, so one check covers them all.
    lines = "\n".join(canonical_lines(spec, {}))
    assert GAZE in lines
    assert STAGING in lines
    assert OBLIGATION in lines

    # And the compiled prompt itself carries them.
    assert result.output.positive_prompt
    assert any(OBLIGATION in str(item) for item in result.output.continuity_assertions)


def test_the_director_reference_set_reaches_the_generation_request(
    container, project, account_worker, register_bytes
):
    """The last link: anchor bindings become GenerationRequest.reference_asset_ids."""

    from production_domain.models import GenerationJob

    account_id, _worker = account_worker
    container.flow_affinity.bind_existing(
        local_project_id=project.id,
        provider_account_id=account_id,
        provider_project_id="flow-project-test",
    )
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="E1",
            episode_number=1,
            script_source="INT. ROOM - NIGHT\nMira raises the phone.\n",
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    result = container.narrative.compile_episode(episode_id)
    shot_id = result.shot_ids[0]
    key_visual = register_bytes(container, project.id, "CHARACTER_MASTER", b"director-key-visual")
    with container.database.session() as session:
        shot = session.get(Shot, shot_id)
        shot.director_intent_json = {
            "version": "director-shot-intent-v1",
            "anchors": ["character:mira"],
            "reference_asset_ids": [key_visual.id],
        }

    candidate, replayed = container.candidates.create_candidate(
        shot_id,
        idempotency_key="director-reference-set",
        enforce_entitlements=False,
    )
    assert replayed is False and candidate.generation_job_id
    with container.database.session() as session:
        job = session.get(GenerationJob, candidate.generation_job_id)
        request = dict(job.request_json)
    assert key_visual.id in request["reference_asset_ids"], request["reference_asset_ids"]


def test_the_deterministic_scaffold_produces_the_same_intent_shape():
    """The fallback path must not emit a narrower intent than the model path."""

    from creative_director_core.beats import BeatPlanner, director_intent, render_script

    planned = BeatPlanner().plan(
        {"characters": [{"name": "Mira"}], "duration_seconds": 30}, format_value="SHORT_DRAMA"
    )
    _script, intents = render_script([beat.as_json() for beat in planned])
    assert intents
    for index, intent in enumerate(intents):
        for key in ("start_state", "end_state", "gaze_target", "continuity_obligations", "description"):
            assert key in intent, (index, sorted(intent))
        payload = director_intent(intent, screenplay_id="s1")
        assert payload["version"] == "director-shot-intent-v1"
        assert payload["screenplay_id"] == "s1"
        assert payload["beat_sequence"] >= 1 and payload["shot_sequence"] >= 1



def test_a_gaze_that_says_off_camera_does_not_approve_camera_gaze(container, project):  # type: ignore[no-untyped-def]
    """Free director text must not flip the camera-gaze approval by accident."""

    from production_domain.models import Character

    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="E1",
            episode_number=1,
            script_source="INT. ROOM - NIGHT\nMira looks at the door.\n",
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    shot_id = container.narrative.compile_episode(episode_id).shot_ids[0]
    with container.database.session() as session:
        session.get(Shot, shot_id).director_intent_json = {
            "version": "director-shot-intent-v1",
            "gaze_target": "off-camera partner",
        }
        mira = session.scalar(select(Character).where(Character.project_id == project.id))
        mira_id = mira.id
    spec = container.video_prompt_compiler.compile(
        shot_id,
        character_bindings=[{"character_id": mira_id, "name": "Mira", "canonical_assets": []}],
    ).spec
    assert spec.allow_camera_gaze is False
    assert "no subject acknowledges the camera" in spec.constraints
    assert any(subject.eyeline_target == "off-camera partner" for subject in spec.subjects)


def test_a_product_the_shot_is_not_bound_to_never_enters_its_props(container, project):  # type: ignore[no-untyped-def]
    """The project's canonical list is not this shot's prop list."""

    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="E1",
            episode_number=1,
            script_source="INT. ROOM - NIGHT\nMira looks at the door.\n",
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    shot_id = container.narrative.compile_episode(episode_id).shot_ids[0]
    canonical = [
        {
            "id": "product-asset",
            "version_id": "product-version",
            "type": "PRODUCT",
            "name": "Aurora Serum",
            "canonical_metadata": {},
            "version_metadata": {},
            "continuity_state": {},
            "image_urls": ["media-not-in-this-shot"],
            "video_urls": [],
            "constraints": [],
        }
    ]
    spec = container.video_prompt_compiler.compile(shot_id, canonical_assets=canonical).spec
    assert [prop for prop in spec.props if prop.get("kind") == "PRODUCT"] == []

    with container.database.session() as session:
        session.get(Shot, shot_id).director_intent_json = {
            "version": "director-shot-intent-v1",
            "anchors": ["product:aurora serum"],
            "reference_asset_ids": ["media-not-in-this-shot"],
        }
    bound = container.video_prompt_compiler.compile(shot_id, canonical_assets=canonical).spec
    assert [prop["asset_id"] for prop in bound.props if prop.get("kind") == "PRODUCT"] == [
        "product-asset"
    ]
