"""Every character on screen carries a key visual and an identity lock.

The screenplay schema admitted twelve characters while anchor derivation only
ever looked at the first six, so characters 7-12 acted on screen with no
canonical reference, no CharacterIdentityVersion and no error. These tests pin
the fix from three sides: the cap is one shared constant, coverage follows the
beats and shots rather than list order, and a character with no locked identity
stops the compile instead of writing an empty lineage row.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from creative_director_core import MAX_CAST, MAX_PROP_ANCHORS, MAX_SCENE_ANCHORS
from creative_director_core.brief import apply_operations
from creative_director_core.schemas import BriefOperation, BriefOperationKind
from creative_director_core.screenplay import (
    ScreenplayCastOverflow,
    ScreenplayInvalid,
    appearing_character_keys,
    derive_anchors,
    validate_screenplay,
)
from test_creative_director import SCREENPLAY
from test_creative_director import (
    openrouter_container as openrouter_container,  # re-exported so the fixture resolves here
)

FIELDS: dict[str, Any] = {
    "format": "SHORT_DRAMA",
    "logline": "A rooftop call from the future",
    "duration_seconds": 60,
    "characters": [{"name": "Mira", "look": "black coat"}],
}


def _cast_screenplay(count: int, *, on_screen: int | None = None) -> dict[str, Any]:
    """A screenplay with `count` named characters, `on_screen` of them acting."""

    on_screen = count if on_screen is None else on_screen
    names = [f"Cast{index}" for index in range(1, count + 1)]
    content = copy.deepcopy(SCREENPLAY)
    content["characters"] = [
        {"name": name, "role": "ensemble", "look": f"{name} in grey"} for name in names
    ]
    content["beats"] = [
        {
            "sequence": index + 1,
            "intent": "BEAT",
            "summary": f"{name} takes the ledge",
            "scene_key": "roof",
            "characters": [name],
            "shots": [
                {
                    "sequence": 1,
                    "shot_type": "MEDIUM",
                    "duration": 4,
                    "action": {"actor": name, "verb": "enter", "description": f"{name} arrives"},
                    "start_state": "rooftop empty",
                    "end_state": f"{name} at the ledge",
                    "gaze_target": "the ledge",
                }
            ],
        }
        for index, name in enumerate(names[:on_screen])
    ]
    return content


def test_the_cast_cap_is_one_shared_constant_across_schema_brief_prompt_and_derivation():
    from creative_director_core.brief import sanitize_value
    from creative_director_core.director_context import SCREENPLAY_PROTOCOL
    from creative_director_core.schemas import Screenplay

    def _max_length(field_name: str) -> int:
        return next(
            item.max_length
            for item in Screenplay.model_fields[field_name].metadata
            if getattr(item, "max_length", None) is not None
        )

    assert _max_length("characters") == MAX_CAST
    assert _max_length("scenes") == MAX_SCENE_ANCHORS
    assert f"at most {MAX_CAST} characters" in SCREENPLAY_PROTOCOL
    # The brief's own cast sanitizer is bound by the same number, not by 8.
    oversized = [{"name": f"Extra{index}"} for index in range(MAX_CAST + 4)]
    assert len(sanitize_value("characters", oversized)) == MAX_CAST


def test_every_character_in_a_shot_gets_a_required_anchor_beyond_the_old_six():
    screenplay = validate_screenplay(_cast_screenplay(MAX_CAST))
    derivation = derive_anchors(FIELDS, screenplay)
    character_anchors = {
        spec.anchor_key: spec for spec in derivation.specs if spec.kind == "CHARACTER"
    }
    assert len(character_anchors) == MAX_CAST
    # The seventh through twelfth are exactly the ones the old slice dropped.
    for index in range(7, MAX_CAST + 1):
        key = f"character:cast{index}"
        assert key in character_anchors, sorted(character_anchors)
        assert character_anchors[key].required is True
    assert derivation.uncovered == ()


def test_a_background_only_character_is_recorded_as_uncovered_rather_than_dropped():
    screenplay = validate_screenplay(_cast_screenplay(8, on_screen=5))
    derivation = derive_anchors(FIELDS, screenplay)
    anchored = {spec.anchor_key for spec in derivation.specs if spec.kind == "CHARACTER"}
    assert anchored == {f"character:cast{index}" for index in range(1, 6)}
    uncovered = {item.title: item.reason for item in derivation.uncovered if item.kind == "CHARACTER"}
    assert uncovered == {
        "Cast6": "NOT_IN_ANY_BEAT_OR_SHOT",
        "Cast7": "NOT_IN_ANY_BEAT_OR_SHOT",
        "Cast8": "NOT_IN_ANY_BEAT_OR_SHOT",
    }
    assert appearing_character_keys(screenplay) == {f"cast{index}" for index in range(1, 6)}


def test_character_order_cannot_hide_a_late_character_from_the_identity_lock():
    """Reversing the cast list must not change which characters are anchored."""

    forward = validate_screenplay(_cast_screenplay(MAX_CAST))
    reversed_content = copy.deepcopy(forward.model_dump(by_alias=True))
    reversed_content["characters"] = list(reversed(reversed_content["characters"]))
    reversed_screenplay = validate_screenplay(reversed_content)
    keys = lambda play: {  # noqa: E731 - a one-line projection reads better inline here
        spec.anchor_key for spec in derive_anchors(FIELDS, play).specs if spec.kind == "CHARACTER"
    }
    assert keys(forward) == keys(reversed_screenplay)
    assert len(keys(forward)) == MAX_CAST


def test_a_cast_over_the_cap_is_refused_at_validation_never_truncated():
    with pytest.raises(ScreenplayInvalid) as raised:
        validate_screenplay(_cast_screenplay(MAX_CAST + 1))
    assert any("characters" in detail for detail in raised.value.details)
    # And the derivation itself refuses a hand-built structure over the cap.
    over = validate_screenplay(_cast_screenplay(MAX_CAST))
    inflated = over.model_copy(update={"characters": list(over.characters) * 2})
    with pytest.raises(ScreenplayCastOverflow):
        derive_anchors(FIELDS, inflated)


def test_the_brief_editor_refuses_a_cast_addition_past_the_cap():
    fields = {"characters": [{"name": f"Cast{index}"} for index in range(1, MAX_CAST + 1)]}
    provenance = {f"characters/cast{index}": {"source": "USER_STATED"} for index in range(1, MAX_CAST + 1)}
    operation = BriefOperation(
        op=BriefOperationKind.UPSERT,
        path="characters",
        value={"name": "OneTooMany", "role": "extra"},
        confidence="USER_STATED",
        evidence="add one more",
    )
    from creative_director_core.brief import OperationActor

    actor = OperationActor(reasoner="USER", turn_id=None, turn_sequence=1, revision=2, at="now")
    result, _records, outcome = apply_operations(fields, provenance, [operation], actor)
    assert len(result["characters"]) == MAX_CAST
    assert any(item["reason"] == "CAST_LIMIT_REACHED" for item in outcome.rejected), outcome.rejected


def test_every_scene_a_beat_plays_in_is_anchored_and_unused_scenes_are_recorded():
    content = _cast_screenplay(3)
    content["scenes"] = [
        {"key": "roof", "location": "rooftop", "time": "NIGHT"},
        {"key": "stair", "location": "stairwell", "time": "NIGHT"},
        {"key": "never", "location": "lobby", "time": "DAY"},
    ]
    content["beats"][1]["scene_key"] = "stair"
    derivation = derive_anchors(FIELDS, validate_screenplay(content))
    scene_keys = {spec.anchor_key for spec in derivation.specs if spec.kind == "SCENE"}
    assert scene_keys == {"scene:rooftop", "scene:stairwell"}
    assert ("SCENE", "lobby", "SCENE_NOT_USED_BY_ANY_BEAT") in {
        (item.kind, item.title, item.reason) for item in derivation.uncovered
    }


def test_props_beyond_the_budget_are_recorded_rather_than_silently_sliced():
    content = _cast_screenplay(1, on_screen=1)
    content["beats"] = [
        {
            "sequence": 1,
            "intent": "BEAT",
            "summary": "objects",
            "scene_key": "roof",
            "characters": ["Cast1"],
            "shots": [
                {
                    "sequence": index + 1,
                    "shot_type": "CLOSE",
                    "duration": 2,
                    "action": {"actor": "Cast1", "verb": "pick_up", "object": f"prop{index}"},
                }
                for index in range(MAX_PROP_ANCHORS + 2)
            ],
        }
    ]
    derivation = derive_anchors({"format": "SHORT_DRAMA"}, validate_screenplay(content))
    props = [spec for spec in derivation.specs if spec.kind == "PROP"]
    assert len(props) == MAX_PROP_ANCHORS
    limited = [item.title for item in derivation.uncovered if item.reason == "PROP_ANCHOR_LIMIT"]
    assert len(limited) == 2


@pytest.mark.asyncio
async def test_a_seven_character_screenplay_locks_seven_identities_end_to_end(openrouter_container):
    """The defect this pins: characters 7-12 used to compile with no identity.

    Seven characters all act on screen, so seven required CHARACTER anchors are
    generated, seven CharacterIdentityVersion rows are locked, and every
    compiled shot's lineage names the identity of the character in it.
    """

    from production_domain.models import (
        Character,
        CharacterIdentityVersion,
        CreativeShotLineage,
        Shot,
    )
    from sqlalchemy import select
    from test_creative_director import (
        RICH_IDEA,
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

    cast = 7
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(
        _rich_turn, screenplay=_cast_screenplay(cast)
    )
    with _client(container) as client:
        headers, project_id, _user_id = _registered_pro(client, container, "seven-cast@example.com")
        started = client.post(
            "/v1/creative/sessions", headers=headers, json={"project_id": project_id, "idea": RICH_IDEA}
        ).json()
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"], headers)
        _approve_screenplay(client, session_id, headers)

        view = _state(client, session_id, headers)
        character_anchors = [a for a in view["anchors"] if a["kind"] == "CHARACTER"]
        assert len(character_anchors) == cast, [a["anchor_key"] for a in view["anchors"]]
        assert all(anchor["required"] for anchor in character_anchors)
        assert view["session"]["limits"]["max_cast"] == MAX_CAST

        await _complete_visuals(container, client, session_id, headers)
        bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose", headers=headers).json()
        locked = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": bible["version"]},
        )
        assert locked.status_code == 200, locked.text
        lineage = locked.json()["lineage"]
        assert set(lineage["identities"]) == {f"character:cast{index}" for index in range(1, cast + 1)}

        with container.database.session() as session:
            identities = list(session.scalars(select(CharacterIdentityVersion)))
            assert len(identities) == cast
            assert {identity.status for identity in identities} == {"LOCKED"}
            names = {
                row.name
                for row in session.scalars(select(Character).where(Character.project_id == project_id))
            }
            assert names == {f"Cast{index}" for index in range(1, cast + 1)}

        client.post(f"/v1/creative/sessions/{session_id}/beats/propose", headers=headers)
        compiled = client.post(
            f"/v1/creative/sessions/{session_id}/beats/approve", headers=headers, json={"plan_revision": 1}
        )
        assert compiled.status_code == 200, compiled.text
        shot_ids = compiled.json()["shot_ids"]
        assert len(shot_ids) == cast
        with container.database.session() as session:
            rows = {
                row.shot_id: row
                for row in session.scalars(
                    select(CreativeShotLineage).where(CreativeShotLineage.session_id == session_id)
                )
            }
            assert len(rows) == cast
            # Every compiled shot names exactly the identity of the character in it -
            # including the seventh, which the old slice left with an empty list.
            for shot_id in shot_ids:
                shot = session.get(Shot, shot_id)
                assert rows[shot_id].identity_version_ids, (shot_id, shot.prompt)
            covered = {
                identity
                for row in rows.values()
                for identity in row.identity_version_ids
            }
            assert len(covered) == cast
