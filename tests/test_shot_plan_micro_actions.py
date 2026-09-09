"""Unknown micro-actions in a Shot Planner plan are advisory: dropped and recorded, never fatal.

A live run wrote ``slight_head_tilt``, ``slight eyebrow raise``, ``slow shallow
breathe``, ``gaze widening`` and ``finger tremor slightly``; the strict schema
refused those shots and the whole plan fell back to the one-line-per-shot
scaffold. A micro-action rides beside the dominant action and never counts as
a second one, so the planner's staging is not worth losing over it. The
schema itself stays strict: a user edit carrying an unknown value is refused.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from creative_director_core.schemas import ScreenplayShot, normalize_micro_action
from creative_director_core.screenplay import (
    ScreenplayInvalid,
    dropped_micro_actions,
    merge_story_and_shot_plan,
    shot_plan_violations,
    story_from_screenplay,
    validate_screenplay,
    validate_shot_plan,
    validate_story,
)
from pydantic import ValidationError
from test_creative_director import (
    RICH_IDEA,
    SCREENPLAY,
    ScriptedDirector,
    _approve_brief,
    _client,
    _rich_turn,
    _start,
)

#: What the model wrote in the live run, verbatim.
LIVE_VALUES = (
    "slight_head_tilt",
    "slight eyebrow raise",
    "slow shallow breathe",
    "gaze widening",
    "finger tremor slightly",
)


def _plan_payload() -> dict[str, Any]:
    """The fixture's shot half, as the Shot Planner returns it."""

    payload = json.loads(json.dumps(SCREENPLAY))
    return {
        "beats": [{"sequence": b["sequence"], "shots": b["shots"]} for b in payload["beats"]],
        "required_copy": [],
        "mobile_hook_check": "",
        "unresolved": [],
    }


# ------------------------------------------------------------ validate_shot_plan
def test_unknown_micro_actions_are_dropped_and_named_and_the_plan_still_merges() -> None:
    story, _ = validate_story(story_from_screenplay(SCREENPLAY))
    payload = _plan_payload()
    first = payload["beats"][0]["shots"][0]
    first["micro_actions"] = ["blinks", *LIVE_VALUES[:3]]
    second = payload["beats"][1]["shots"][0]
    second["micro_actions"] = [*LIVE_VALUES[3:], "slight_head_tilt"]

    plan, stripped = validate_shot_plan(payload)

    # The alias is kept under its canonical key; the unknown values are gone.
    assert plan.beats[0].shots[0].micro_actions == ["blink"]
    assert plan.beats[1].shots[0].micro_actions == []
    assert [entry for entry in stripped if ".micro_actions[" in entry] == [
        "beats[0].shots[0].micro_actions[slight_head_tilt]",
        "beats[0].shots[0].micro_actions[slight eyebrow raise]",
        "beats[0].shots[0].micro_actions[slow shallow breathe]",
        "beats[1].shots[0].micro_actions[gaze widening]",
        "beats[1].shots[0].micro_actions[finger tremor slightly]",
        "beats[1].shots[0].micro_actions[slight_head_tilt]",
    ]
    dropped, keys = dropped_micro_actions(stripped)
    assert dropped == list(LIVE_VALUES)
    assert keys and all(".micro_actions[" not in path for path in keys)
    # The fixture's WIDE/CLOSE shot sizes are still stripped as before.
    assert any(path.endswith(".shot_type") for path in keys)
    # The planner's raw output is untouched: the runtime keeps it on record.
    assert first["micro_actions"] == ["blinks", *LIVE_VALUES[:3]]

    assert shot_plan_violations(story, plan) == []
    merged = merge_story_and_shot_plan(story, plan)
    assert merged.beats[0].shots[0].micro_actions == ["blink"]
    assert merged.beats[1].shots[0].micro_actions == []


def test_a_dropped_value_is_reported_whitespace_normalised_and_bounded() -> None:
    payload = _plan_payload()
    payload["beats"][2]["shots"][0]["micro_actions"] = [
        "finger   tremor\n slightly",
        "x" * 100,
        "",
        None,
        "breathing",
    ]
    plan, stripped = validate_shot_plan(payload)
    dropped, _ = dropped_micro_actions(stripped)
    assert dropped == ["finger tremor slightly", "x" * 60]
    assert plan.beats[2].shots[0].micro_actions == ["breathe"]


def test_a_plan_without_unknown_micro_actions_reports_nothing_new() -> None:
    payload = _plan_payload()
    payload["beats"][0]["shots"][0]["micro_actions"] = ["blink", "glance"]
    plan, stripped = validate_shot_plan(payload)
    dropped, keys = dropped_micro_actions(stripped)
    assert dropped == [] and keys == stripped
    assert plan.beats[0].shots[0].micro_actions == ["blink", "gaze_shift"]


# ---------------------------------------------------------------- the service
def test_the_service_keeps_the_planners_staging_and_records_the_drop(container, project):  # type: ignore[no-untyped-def]
    def tilting(request: dict[str, Any]) -> dict[str, Any]:
        payload = _plan_payload()
        payload["beats"][0]["shots"][1]["micro_actions"] = ["slight_head_tilt", "blink"]
        payload["beats"][2]["shots"][0]["micro_actions"] = ["gaze widening", "slight_head_tilt"]
        return payload

    container.creative_director.model_roles = ScriptedDirector(_rich_turn, shot_plan=tilting)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        approved = _approve_brief(client, started["session_id"], started["brief_revision"])
    screenplay = approved["screenplay"]
    assert screenplay["reasoner"] == "MODEL:DIRECTOR+MODEL:SHOT_PLANNER"
    assert screenplay["deterministic"] is False and screenplay["skill_driven"] is True
    codes = screenplay["reason_codes"]
    assert "SHOT_PLANNER:MICRO_ACTION_DROPPED:slight_head_tilt" in codes
    assert "SHOT_PLANNER:MICRO_ACTION_DROPPED:gaze widening" in codes
    assert codes.count("SHOT_PLANNER:MICRO_ACTION_DROPPED:slight_head_tilt") == 1
    assert not any("AUTHORITY_VIOLATION" in code for code in codes)
    assert not any("AUTHORITY_STRIPPED:micro_actions" in code for code in codes)
    # Forbidden keys are still stripped and named exactly as before.
    assert "SHOT_PLANNER:AUTHORITY_STRIPPED:shot_type" in codes
    shots = screenplay["skill_invocations"]["shots"]
    assert shots["name"] == "short-drama" and shots["role"] == "shot_planner"
    assert shots["model_invoked"] and shots["execution_mode"] == "MODEL"
    assert shots["fallback_reason"] is None and shots["skill_driven"] is True
    assert "MICRO_ACTION_DROPPED:slight_head_tilt" in shots["reason_codes"]
    assert all("micro_actions" not in item for item in shots["authority_violations"])
    beats = screenplay["content"]["beats"]
    assert beats[0]["shots"][1]["micro_actions"] == ["blink"]
    assert beats[2]["shots"][0]["micro_actions"] == []


# --------------------------------------------------------- the schema is strict
def test_the_schema_and_the_user_edit_path_still_refuse_an_unknown_micro_action() -> None:
    with pytest.raises(ValueError, match="unknown micro-action"):
        normalize_micro_action("slight_head_tilt")
    shot = {
        "sequence": 1,
        "action": {"actor": "Mira", "verb": "enter"},
        "micro_actions": ["slight_head_tilt"],
    }
    with pytest.raises(ValidationError, match="unknown micro-action"):
        ScreenplayShot.model_validate(shot)
    # A whole screenplay - what a user edit goes through - is as strict.
    content = json.loads(json.dumps(SCREENPLAY))
    content["beats"][0]["shots"][0]["micro_actions"] = ["slight eyebrow raise"]
    with pytest.raises(ScreenplayInvalid) as excinfo:
        validate_screenplay(content)
    assert any("unknown micro-action" in detail for detail in excinfo.value.details)


def test_a_malformed_or_overlong_micro_action_list_is_advisory_too() -> None:
    payload = _plan_payload()
    payload["beats"][0]["shots"][0]["micro_actions"] = "blink, breathe; slight head tilt"
    payload["beats"][0]["shots"][1]["micro_actions"] = None
    payload["beats"][1]["shots"][0]["micro_actions"] = {"blink": True}
    payload["beats"][1]["shots"][1]["micro_actions"] = [
        "blink",
        "breathe",
        "gaze_shift",
        "slight_head_turn",
        "mouth_movement",
    ]
    plan, stripped = validate_shot_plan(payload)
    assert plan.beats[0].shots[0].micro_actions == ["blink", "breathe"]
    assert plan.beats[0].shots[1].micro_actions == []
    assert plan.beats[1].shots[0].micro_actions == []
    assert plan.beats[1].shots[1].micro_actions == ["blink", "breathe", "gaze_shift", "slight_head_turn"]
    dropped, _ = dropped_micro_actions(stripped)
    assert dropped == ["slight head tilt", "<dict>", "mouth_movement (surplus)"]
