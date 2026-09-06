"""The director's shot contract, revised.

One generation shot is one dominant visual action. A short line may ride
beside it; a shot with a line and no action is a speaking shot. Micro-actions
(a blink, a breath, mouth movement, a glance, a slight head turn) never count
as a second action, and a staging note that sequences two actions is refused.
A line has to fit the shot it is spoken in. Everyone in frame is present;
only the faces the audience must recognise are identity-critical, and only
those become identity references for the provider.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from creative_director_core import screenplay as screenplay_module
from creative_director_core.beats import director_intent
from creative_director_core.director_context import SCREENPLAY_PROTOCOL
from creative_director_core.schemas import (
    MAX_BEATS,
    MAX_CAST,
    MAX_IDENTITY_CRITICAL_CHARACTERS,
    MAX_SCENE_ANCHORS,
    MAX_SCENES,
    MAX_SHOT_DURATION_SECONDS,
    MAX_SHOTS_PER_BEAT,
    MICRO_ACTIONS,
    MIN_SHOT_DURATION_SECONDS,
    Screenplay,
    ScreenplayBeat,
    ScreenplayShot,
    estimated_speech_seconds,
    usable_dialogue_window,
)
from creative_director_core.screenplay import (
    ScreenplayInvalid,
    anchor_keys_for_shot,
    beats_from_screenplay,
    shot_constraints,
    validate_screenplay,
)
from platform_contracts import identity_critical_subjects
from pydantic import ValidationError


def _max_length(model: type, name: str) -> int | None:
    field = model.model_fields[name]
    for item in field.metadata:
        if hasattr(item, "max_length"):
            return item.max_length
    return None


def _bound(model: type, name: str, attribute: str) -> float | None:
    field = model.model_fields[name]
    for item in field.metadata:
        if hasattr(item, attribute):
            return getattr(item, attribute)
    return None


def _shot(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sequence": 1,
        "shot_type": "MEDIUM",
        "duration": 5,
        "action": {"actor": "Mira", "verb": "pick_up", "object": "phone", "description": "slowly"},
        "start_state": "phone on the ledge",
        "end_state": "phone in hand",
        "gaze_target": "the phone",
    }
    base.update(overrides)
    return base


def _screenplay(*shots: dict[str, Any]) -> dict[str, Any]:
    return {
        "treatment": {"premise": "A phone that is not hers."},
        "characters": [{"name": "Mira"}, {"name": "Ren"}, {"name": "Bao"}],
        "scenes": [{"key": "roof", "location": "rooftop", "time": "NIGHT"}],
        "beats": [
            {
                "sequence": 1,
                "intent": "COLD_OPEN",
                "scene_key": "roof",
                "characters": ["Mira", "Ren"],
                "shots": list(shots),
            }
        ],
    }


# --- limits ------------------------------------------------------------------


def test_the_structural_limits_are_named_and_enforced() -> None:
    assert (MAX_CAST, MAX_SCENES, MAX_BEATS, MAX_SHOTS_PER_BEAT) == (12, 12, 40, 12)
    assert MAX_SCENE_ANCHORS == MAX_SCENES
    assert (MIN_SHOT_DURATION_SECONDS, MAX_SHOT_DURATION_SECONDS) == (1.0, 15.0)
    assert _max_length(Screenplay, "characters") == MAX_CAST
    assert _max_length(Screenplay, "scenes") == MAX_SCENES
    assert _max_length(Screenplay, "beats") == MAX_BEATS
    assert _max_length(ScreenplayBeat, "shots") == MAX_SHOTS_PER_BEAT
    assert _bound(ScreenplayShot, "duration", "ge") == MIN_SHOT_DURATION_SECONDS
    assert _bound(ScreenplayShot, "duration", "le") == MAX_SHOT_DURATION_SECONDS
    assert _max_length(ScreenplayShot, "identity_critical_characters") == MAX_IDENTITY_CRITICAL_CHARACTERS
    with pytest.raises(ValidationError):
        ScreenplayShot.model_validate(_shot(duration=16))
    with pytest.raises(ValidationError):
        ScreenplayShot.model_validate(_shot(duration=0.5))


def test_requested_duration_is_the_directors_name_for_the_shot_length() -> None:
    payload = _shot()
    payload.pop("duration")
    shot = ScreenplayShot.model_validate({**payload, "requested_duration": 7})
    assert shot.duration == 7 and shot.requested_duration == 7


# --- one dominant action, 0..1 line ---------------------------------------------


def test_an_action_and_a_line_may_share_one_shot() -> None:
    shot = ScreenplayShot.model_validate(
        _shot(duration=6, dialogue={"speaker": "Ren", "text": "Put it down."})
    )
    assert shot.action is not None and shot.dialogue is not None
    assert shot.shot_type == "MEDIUM", "the action keeps the framing; the line rides beside it"
    assert shot.present_characters == ["Mira", "Ren"]
    assert shot.identity_critical_characters == ["Mira", "Ren"]


def test_a_line_alone_is_a_speaking_shot_and_nothing_alone_is_refused() -> None:
    speaking = ScreenplayShot.model_validate(
        _shot(action=None, dialogue={"speaker": "Ren", "text": "Put it down."})
    )
    assert speaking.shot_type == "DIALOGUE"
    assert speaking.present_characters == ["Ren"]
    with pytest.raises(ValidationError, match="dominant visual action, a line, or both"):
        ScreenplayShot.model_validate(_shot(action=None))


def test_micro_actions_are_a_closed_vocabulary_with_aliases() -> None:
    shot = ScreenplayShot.model_validate(
        _shot(micro_actions=["眨眼", "lip sync", "glance", "breathing"])
    )
    assert shot.micro_actions == ["blink", "mouth_movement", "gaze_shift", "breathe"]
    assert set(shot.micro_actions) <= set(MICRO_ACTIONS)
    with pytest.raises(ValidationError, match="unknown micro-action"):
        ScreenplayShot.model_validate(_shot(micro_actions=["walks to the door"]))


@pytest.mark.parametrize(
    "description",
    [
        "she picks up the phone, then walks to the ledge",
        "拿起手机，然后走向天台边缘",
        "picks it up and then answers it",
    ],
)
def test_two_consecutive_narrative_actions_are_refused(description: str) -> None:
    with pytest.raises(ValidationError, match="sequence of actions"):
        ScreenplayShot.model_validate(
            _shot(action={"actor": "Mira", "verb": "pick_up", "object": "phone", "description": description})
        )


def test_the_protocol_states_the_new_rule_and_not_the_old_one() -> None:
    assert "an \"action\" OR a \"dialogue\", never both" not in SCREENPLAY_PROTOCOL
    assert "ONE dominant visual action" in SCREENPLAY_PROTOCOL
    assert "0..1 short" in SCREENPLAY_PROTOCOL
    assert "micro_actions" in SCREENPLAY_PROTOCOL
    assert "identity_critical_characters" in SCREENPLAY_PROTOCOL
    assert f"at most {MAX_CAST} characters" in SCREENPLAY_PROTOCOL
    assert f"{MIN_SHOT_DURATION_SECONDS:g}-{MAX_SHOT_DURATION_SECONDS:g}" in SCREENPLAY_PROTOCOL
    assert "2-10 seconds" not in SCREENPLAY_PROTOCOL


# --- a line has to fit its shot --------------------------------------------------


def test_speech_estimates_are_deterministic_per_character_and_per_word() -> None:
    assert estimated_speech_seconds("你终于来了") == 1.25
    assert estimated_speech_seconds("You did, Mira. Three days from now.") == 2.8
    assert estimated_speech_seconds("你好 Mira, sit down") == 0.5 + 1.2
    assert usable_dialogue_window(3) == 2.0
    assert usable_dialogue_window(0.5) == 0.0


def test_a_line_that_cannot_be_said_in_its_shot_is_refused() -> None:
    long_line = "这句台词实在太长了，三秒钟的镜头根本说不完，导演必须拆开或者加长镜头才行"
    with pytest.raises(ValidationError, match="cannot carry its line"):
        ScreenplayShot.model_validate(
            _shot(action=None, duration=3, dialogue={"speaker": "Ren", "text": long_line})
        )
    fits = ScreenplayShot.model_validate(
        _shot(action=None, duration=3, dialogue={"speaker": "Ren", "text": "放下它"})
    )
    assert fits.dialogue is not None
    with pytest.raises(ValidationError, match="cannot carry its line"):
        ScreenplayShot.model_validate(
            _shot(
                duration=2,
                dialogue={"speaker": "Ren", "text": "Put it down right now before anyone sees us here."},
            )
        )


def test_the_speech_check_also_guards_user_edits() -> None:
    screenplay = validate_screenplay(
        _screenplay(_shot(duration=6, dialogue={"speaker": "Ren", "text": "Put it down."}))
    )
    with pytest.raises(ScreenplayInvalid) as refused:
        screenplay_module.apply_beat_edits(
            screenplay,
            [{"sequence": 1, "shots": [{"duration": 1.5}]}],
        )
    assert any("cannot carry its line" in item for item in refused.value.details)


# --- present vs identity-critical ------------------------------------------------


def test_identity_critical_characters_are_a_subset_of_the_present_ones() -> None:
    shot = ScreenplayShot.model_validate(
        _shot(present_characters=["Mira", "Ren", "Bao"], identity_critical_characters=["Mira"])
    )
    assert shot.present_characters == ["Mira", "Ren", "Bao"]
    assert shot.identity_critical_characters == ["Mira"]
    with pytest.raises(ValidationError, match="identity-critical but does not list"):
        ScreenplayShot.model_validate(
            _shot(present_characters=["Mira"], identity_critical_characters=["Bao"])
        )
    with pytest.raises(ValidationError):
        ScreenplayShot.model_validate(
            _shot(
                present_characters=["A", "B", "C", "D", "E", "Mira"],
                identity_critical_characters=["A", "B", "C", "D", "E"],
            )
        )


def test_every_present_character_must_be_in_the_cast() -> None:
    with pytest.raises(ScreenplayInvalid) as refused:
        validate_screenplay(_screenplay(_shot(present_characters=["Mira", "Ghost"])))
    assert any("unknown character 'Ghost'" in item for item in refused.value.details)


def test_the_beat_plan_and_the_intent_carry_the_line_and_the_layers() -> None:
    screenplay = validate_screenplay(
        _screenplay(
            _shot(
                duration=6,
                dialogue={"speaker": "Ren", "text": "Put it down."},
                micro_actions=["blink"],
                present_characters=["Mira", "Ren", "Bao"],
                identity_critical_characters=["Mira", "Ren"],
            )
        )
    )
    beat = beats_from_screenplay(screenplay)[0]
    shot = beat["shots"][0]
    assert shot["actor"] == "Mira" and shot["verb"] == "pick_up"
    assert shot["dialogue"] == "Put it down." and shot["speaker"] == "Ren"
    assert shot["micro_actions"] == ["blink"]
    assert shot["present_characters"] == ["Mira", "Ren", "Bao"]
    assert shot["identity_critical_characters"] == ["Mira", "Ren"]
    # The rendered line is the action, for the narrative compiler.
    assert shot["action"].startswith("Mira picks up")

    keys = anchor_keys_for_shot(shot, beat, None)
    assert keys[:2] == ["character:mira", "character:ren"], "identity anchors, not everyone present"
    assert "character:bao" not in keys

    constraints = shot_constraints(screenplay)[0]
    assert constraints.shot_sequence == 1

    intent = director_intent({**shot, "beat_sequence": 1, "shot_sequence": 1})
    assert intent["dialogue"] == "Put it down." and intent["speaker"] == "Ren"
    assert intent["present_characters"] == ["Mira", "Ren", "Bao"]
    assert intent["identity_critical_characters"] == ["Mira", "Ren"]
    assert intent["micro_actions"] == ["blink"]


def test_a_scoped_invariant_applies_to_a_present_character() -> None:
    content = _screenplay(_shot(present_characters=["Mira", "Bao"]))
    content["invariants"] = [{"text": "Bao never smiles", "characters": ["Bao"]}]
    screenplay = validate_screenplay(content)
    assert "Bao never smiles" in shot_constraints(screenplay)[0].invariants


def test_provider_identity_references_narrow_to_the_declared_faces() -> None:
    subjects = [
        {"character_id": "c-mira", "name": "Mira", "master_asset_id": "m-1"},
        {"character_id": "c-lin", "name": "Lin-Jin", "master_asset_id": "m-2"},
        {"character_id": "c-bao", "name": "Bao", "master_asset_id": "m-3"},
    ]
    kept = identity_critical_subjects(subjects, ["Mira", "Lin Jin"])
    assert [row["character_id"] for row in kept] == ["c-mira", "c-lin"]
    # No declaration: every planner subject keeps its plate (older shots).
    assert len(identity_critical_subjects(subjects, [])) == 3
    # A declaration that names nobody the planner knows is a naming mismatch,
    # not permission to render with no face held.
    assert len(identity_critical_subjects(subjects, ["Nobody"])) == 3


def test_legacy_screenplays_still_validate() -> None:
    """Approved revisions on record predate the new fields and must still load."""

    legacy = _screenplay(
        _shot(),
        {
            "sequence": 2,
            "shot_type": "DIALOGUE",
            "duration": 6,
            "dialogue": {"speaker": "Ren", "text": "You did, Mira. Three days from now."},
            "start_state": "call open",
            "end_state": "Mira frozen",
        },
    )
    screenplay = validate_screenplay(copy.deepcopy(legacy))
    second = screenplay.beats[0].shots[1]
    assert second.action is None and second.shot_type == "DIALOGUE"
    assert second.identity_critical_characters == ["Ren"]
