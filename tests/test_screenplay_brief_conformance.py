"""A structurally perfect screenplay can still be the wrong story.

Before this, the screenplay was validated against a schema and nothing else.
The model could rename the protagonist, move the story to another city, drop
the product out of a product film, invent or delete a relationship, or open on
a different hook - and the structure was still valid, so the key visuals were
derived and *paid for* from a story the user never approved.

Severity follows provenance, which is the whole point: a value the user stated,
edited or explicitly accepted is a fact the director may not move (BLOCKING); a
value the director inferred or a format default supplied is the space the
director is meant to fill (ADVISORY, reported as enrichment).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from creative_director_core import ScreenplayBriefValidator
from creative_director_core.screenplay import validate_screenplay
from test_creative_director import (
    RICH_IDEA,
    SCREENPLAY,
    ScriptedDirector,
    _approve_brief,
    _client,
    _rich_turn,
    _start,
    _state,
)
from test_creative_director import (
    openrouter_container as openrouter_container,  # re-exported so the fixture resolves here
)

VALIDATOR = ScreenplayBriefValidator()

BRIEF: dict[str, Any] = {
    "format": "SHORT_DRAMA",
    "logline": "A rooftop call from the future",
    "duration_seconds": 30,
    "aspect_ratio": "9:16",
    "hook": "whose phone is this",
    "setting": {"location": "rooftop", "time": "NIGHT"},
    "characters": [
        {"name": "Mira", "role": "protagonist", "look": "black coat"},
        {"name": "Ren", "role": "the caller", "relationships": [{"with": "Mira", "relation": "stranger"}]},
    ],
}
USER_FACTS = {
    "duration_seconds": {"source": "USER_STATED"},
    "aspect_ratio": {"source": "USER_STATED"},
    "hook": {"source": "USER_STATED"},
    "setting.location": {"source": "USER_STATED"},
    "setting.time": {"source": "USER_STATED"},
    "characters/mira": {"source": "USER_STATED"},
    "characters/ren": {"source": "USER_STATED"},
    "product.name": {"source": "USER_STATED"},
    "call_to_action": {"source": "USER_STATED"},
}


def _play(**overrides: Any):  # type: ignore[no-untyped-def]
    content = copy.deepcopy(SCREENPLAY)
    content.update(overrides)
    return validate_screenplay(content)


def _codes(conformance) -> set[str]:  # type: ignore[no-untyped-def]
    return {item.code for item in conformance.violations}


def test_the_shipped_screenplay_agrees_with_its_brief():
    found = VALIDATOR.validate(_play(), BRIEF, format_value="SHORT_DRAMA", provenance=USER_FACTS)
    assert found.blocking == [], [item.as_json() for item in found.blocking]


def test_a_renamed_protagonist_is_a_blocking_conflict_with_a_structured_path():
    content = copy.deepcopy(SCREENPLAY)
    for character in content["characters"]:
        if character["name"] == "Mira":
            character["name"] = "Nadia"
    for beat in content["beats"]:
        beat["characters"] = ["Nadia" if name == "Mira" else name for name in beat["characters"]]
        for shot in beat["shots"]:
            if shot.get("action", {}).get("actor") == "Mira":
                shot["action"]["actor"] = "Nadia"
            if shot.get("dialogue", {}).get("speaker") == "Mira":
                shot["dialogue"]["speaker"] = "Nadia"
    for character in content["characters"]:
        for relation in character.get("relationships", []):
            if relation["with"] == "Mira":
                relation["with"] = "Nadia"

    found = VALIDATOR.validate(
        validate_screenplay(content), BRIEF, format_value="SHORT_DRAMA", provenance=USER_FACTS
    )
    blocking = {item.code: item for item in found.blocking}
    assert "CAST_MEMBER_MISSING" in blocking
    conflict = blocking["CAST_MEMBER_MISSING"]
    assert conflict.brief_path == "characters/mira"
    assert conflict.brief_value == "Mira"
    assert "Nadia" in conflict.screenplay_value
    assert conflict.brief_source == "USER_STATED"
    assert conflict.reason


def test_moving_the_story_to_another_location_blocks():
    content = copy.deepcopy(SCREENPLAY)
    content["scenes"][0]["location"] = "subway platform"
    found = VALIDATOR.validate(
        validate_screenplay(content), BRIEF, format_value="SHORT_DRAMA", provenance=USER_FACTS
    )
    item = next(entry for entry in found.blocking if entry.code == "LOCATION_CHANGED")
    assert item.brief_value == "rooftop" and item.screenplay_value == ["subway platform"]


def test_changing_the_time_of_day_blocks_but_dusk_still_counts_as_night():
    night_to_day = copy.deepcopy(SCREENPLAY)
    night_to_day["scenes"][0]["time"] = "DAY"
    found = VALIDATOR.validate(
        validate_screenplay(night_to_day), BRIEF, format_value="SHORT_DRAMA", provenance=USER_FACTS
    )
    assert "TIME_CHANGED" in {item.code for item in found.blocking}

    dusk = copy.deepcopy(SCREENPLAY)
    dusk["scenes"][0]["time"] = "DUSK"
    ok = VALIDATOR.validate(
        validate_screenplay(dusk), BRIEF, format_value="SHORT_DRAMA", provenance=USER_FACTS
    )
    assert "TIME_CHANGED" not in _codes(ok)


def test_a_dropped_relationship_and_a_rewritten_one_both_block():
    dropped = copy.deepcopy(SCREENPLAY)
    for character in dropped["characters"]:
        character.pop("relationships", None)
    found = VALIDATOR.validate(
        validate_screenplay(dropped), BRIEF, format_value="SHORT_DRAMA", provenance=USER_FACTS
    )
    assert "RELATIONSHIP_MISSING" in {item.code for item in found.blocking}

    rewritten = copy.deepcopy(SCREENPLAY)
    for character in rewritten["characters"]:
        if character["name"] == "Ren":
            character["relationships"] = [{"with": "Mira", "relation": "her brother"}]
    changed = VALIDATOR.validate(
        validate_screenplay(rewritten), BRIEF, format_value="SHORT_DRAMA", provenance=USER_FACTS
    )
    item = next(entry for entry in changed.blocking if entry.code == "RELATIONSHIP_CHANGED")
    assert item.brief_value == "stranger" and item.screenplay_value == "her brother"


def test_a_product_film_that_never_mentions_the_product_blocks():
    brief = {**BRIEF, "format": "ADVERTISEMENT", "product": {"name": "Aurora Serum"}}
    found = VALIDATOR.validate(
        _play(), brief, format_value="ADVERTISEMENT", provenance=USER_FACTS
    )
    item = next(entry for entry in found.blocking if entry.code == "PRODUCT_MISSING")
    assert item.brief_value == "Aurora Serum"
    # The same omission in a non-commerce format is advice, not a refusal.
    drama = VALIDATOR.validate(
        _play(), {**BRIEF, "product": {"name": "Aurora Serum"}},
        format_value="SHORT_DRAMA", provenance=USER_FACTS,
    )
    assert "PRODUCT_MISSING" not in {item.code for item in drama.blocking}


def test_a_dropped_call_to_action_blocks_and_a_kept_one_does_not():
    brief = {**BRIEF, "call_to_action": "download the app tonight"}
    found = VALIDATOR.validate(_play(), brief, format_value="SHORT_DRAMA", provenance=USER_FACTS)
    assert "CALL_TO_ACTION_MISSING" in {item.code for item in found.blocking}

    kept = _play(required_copy=["Download the app tonight"])
    ok = VALIDATOR.validate(kept, brief, format_value="SHORT_DRAMA", provenance=USER_FACTS)
    assert "CALL_TO_ACTION_MISSING" not in _codes(ok)


def test_a_different_hook_blocks():
    content = copy.deepcopy(SCREENPLAY)
    content["treatment"]["hook"]["opening_question"] = "Can she outrun the storm?"
    content["treatment"]["hook"]["promise"] = "A chase across the skyline."
    content["treatment"]["premise"] = "A courier races a storm across the skyline."
    content["treatment"]["title"] = "Skyline"
    found = VALIDATOR.validate(
        validate_screenplay(content), BRIEF, format_value="SHORT_DRAMA", provenance=USER_FACTS
    )
    item = next(entry for entry in found.blocking if entry.code == "HOOK_CHANGED")
    assert item.brief_value == "whose phone is this"


def test_a_screenplay_that_describes_a_different_frame_blocks():
    content = copy.deepcopy(SCREENPLAY)
    content["treatment"]["visual_direction"] = "anamorphic 16:9, teal night"
    found = VALIDATOR.validate(
        validate_screenplay(content), BRIEF, format_value="SHORT_DRAMA", provenance=USER_FACTS
    )
    item = next(entry for entry in found.blocking if entry.code == "ASPECT_RATIO_CHANGED")
    assert item.brief_value == "9:16" and item.screenplay_value == ["16:9"]


def test_a_forbidden_sentence_put_back_on_screen_blocks():
    content = copy.deepcopy(SCREENPLAY)
    content["beats"][1]["shots"][0]["dialogue"]["text"] = "不要提到价格, ever."
    found = VALIDATOR.validate(
        validate_screenplay(content),
        BRIEF,
        format_value="SHORT_DRAMA",
        provenance=USER_FACTS,
        prohibitions=["不要提到价格"],
    )
    item = next(entry for entry in found.blocking if entry.code == "PROHIBITION_BREACHED")
    assert item.location.startswith("beat 2")


def test_enrichment_of_a_value_the_director_itself_assumed_is_advisory_not_blocking():
    """Rule 2: the model may enrich what it inferred; it may not rewrite a user fact."""

    assumed = {**USER_FACTS, "setting.location": {"source": "MODEL_INFERRED"}}
    content = copy.deepcopy(SCREENPLAY)
    content["scenes"][0]["location"] = "subway platform"
    found = VALIDATOR.validate(
        validate_screenplay(content), BRIEF, format_value="SHORT_DRAMA", provenance=assumed
    )
    assert "LOCATION_CHANGED" not in {item.code for item in found.blocking}
    item = next(entry for entry in found.advisory if entry.code == "LOCATION_CHANGED")
    assert item.brief_source == "MODEL_INFERRED"

    # An assumption the user explicitly accepted is a user fact again.
    accepted = {**USER_FACTS, "setting.location": {"source": "ASSUMPTION_ACCEPTED"}}
    blocked = VALIDATOR.validate(
        validate_screenplay(content), BRIEF, format_value="SHORT_DRAMA", provenance=accepted
    )
    assert "LOCATION_CHANGED" in {item.code for item in blocked.blocking}


def test_a_wildly_wrong_running_time_is_advisory_because_pacing_is_the_directors():
    short = copy.deepcopy(SCREENPLAY)
    for beat in short["beats"]:
        for shot in beat["shots"]:
            shot["duration"] = 1
    found = VALIDATOR.validate(
        validate_screenplay(short), BRIEF, format_value="SHORT_DRAMA", provenance=USER_FACTS
    )
    assert "DURATION_OUT_OF_TOLERANCE" not in {item.code for item in found.blocking}
    assert "DURATION_OUT_OF_TOLERANCE" in {item.code for item in found.advisory}


# ------------------------------------------------------------ through the API
def _conflicting_screenplay() -> dict[str, Any]:
    """Structurally valid, factually a different story: Mira becomes Nadia."""

    content = copy.deepcopy(SCREENPLAY)
    for character in content["characters"]:
        if character["name"] == "Mira":
            character["name"] = "Nadia"
        for relation in character.get("relationships", []):
            if relation["with"] == "Mira":
                relation["with"] = "Nadia"
    for beat in content["beats"]:
        beat["characters"] = ["Nadia" if name == "Mira" else name for name in beat["characters"]]
        for shot in beat["shots"]:
            if shot.get("action", {}).get("actor") == "Mira":
                shot["action"]["actor"] = "Nadia"
            if shot.get("dialogue", {}).get("speaker") == "Mira":
                shot["dialogue"]["speaker"] = "Nadia"
            if shot.get("dialogue"):
                shot["dialogue"]["text"] = shot["dialogue"]["text"].replace("Mira", "Nadia")
    content["scenes"][0]["location"] = "subway platform"
    return content


def test_a_schema_valid_but_factually_conflicting_screenplay_cannot_be_approved(container, project):
    """The whole defect in one test: valid structure, wrong story, no approval."""

    container.creative_director.model_roles = ScriptedDirector(
        _rich_turn, screenplay=_conflicting_screenplay()
    )
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        view = _state(client, session_id)
        assert view["screenplay"]["reasoner"] == "MODEL:DIRECTOR"
        assert "SCREENPLAY_CONTRADICTS_BRIEF" in view["screenplay"]["reason_codes"]
        conflicts = [
            item for item in view["screenplay"]["brief_conformance"] if item["severity"] == "BLOCKING"
        ]
        paths = {item["brief_path"] for item in conflicts}
        assert {"characters/mira", "setting.location"} <= paths, conflicts

        refused = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/approve",
            json={"revision": view["screenplay"]["revision"]},
        )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["reason_code"] == "SCREENPLAY_CONTRADICTS_BRIEF"
        reported = {item["brief_path"] for item in detail["violations"]}
        assert {"characters/mira", "setting.location"} <= reported
        first = next(item for item in detail["violations"] if item["brief_path"] == "setting.location")
        assert first["brief_value"] == "rooftop"
        assert first["screenplay_value"] == ["subway platform"]
        assert first["reason"]

        # Nothing was derived and nothing was paid for.
        after = _state(client, session_id)
        assert after["session"]["status"] == "SCREENPLAY_PROPOSED"
        assert after["anchors"] == []
        assert [action["kind"] for action in after["actions"]] == []

        # The user may overrule their own brief, explicitly.
        overruled = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/approve",
            json={"revision": view["screenplay"]["revision"], "accept_brief_violations": True},
        )
        assert overruled.status_code == 200, overruled.text
        assert overruled.json()["session_status"] == "VISUALS_IN_PROGRESS"


def test_a_user_edit_that_contradicts_the_brief_is_refused_at_the_edit(container, project):
    """Rule 5: the screenplay is re-validated after the user edits it."""

    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        edited = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/edit",
            json={"content": _conflicting_screenplay()},
        )
        assert edited.status_code == 409, edited.text
        detail = edited.json()["detail"]
        assert detail["reason_code"] == "SCREENPLAY_CONTRADICTS_BRIEF"
        assert {item["brief_path"] for item in detail["violations"]} >= {"characters/mira"}
        after = _state(client, session_id)
    # The refused edit did not become a revision.
    assert len(after["screenplays"]) == 1
    assert after["session"]["screenplay_revision"] == 1


@pytest.mark.asyncio
async def test_beat_edits_that_contradict_the_brief_are_refused_at_compile(openrouter_container):
    """Rule 6: a beat edit is a new revision, so it faces the same check."""

    from test_creative_director import (
        _approve_screenplay,
        _complete_visuals,
        _registered_pro,
        _wire_openrouter_images,
    )

    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user = _registered_pro(client, container, "beat-edit@example.com")
        started = client.post(
            "/v1/creative/sessions",
            headers=headers,
            json={"project_id": project_id, "idea": RICH_IDEA + " 不要提到价格。"},
        ).json()
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"], headers)
        _approve_screenplay(client, session_id, headers)
        await _complete_visuals(container, client, session_id, headers)
        bible = client.post(
            f"/v1/creative/sessions/{session_id}/bible/propose", headers=headers
        ).json()
        locked = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": bible["version"]},
        )
        assert locked.status_code == 200, locked.text
        proposed = client.post(f"/v1/creative/sessions/{session_id}/beats/propose", headers=headers)
        assert proposed.status_code == 200, proposed.text
        beats = proposed.json()["beats"]
        # The user edits a line into the sentence they themselves forbade.
        edited = [
            {
                "sequence": beat["sequence"],
                "shots": [
                    {"dialogue": "不要提到价格" if shot.get("dialogue") else None}
                    for shot in beat["shots"]
                ],
            }
            for beat in beats
        ]
        refused = client.post(
            f"/v1/creative/sessions/{session_id}/beats/approve",
            headers=headers,
            json={"plan_revision": 1, "beats": edited},
        )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["reason_code"] == "SCREENPLAY_CONTRADICTS_BRIEF"
        assert any(item["code"] == "PROHIBITION_BREACHED" for item in detail["violations"])
        after = _state(client, session_id, headers)
    # Nothing compiled, and no new screenplay revision was written.
    assert after["session"]["status"] == "BEATS_PROPOSED"
    assert after["session"]["compiled_episode_id"] is None
    assert len(after["screenplays"]) == 1
