"""The Cinematography Skill's output contract: limits bound the prompt, not the wording.

A live run lost a whole shot design because `camera.focus` was a 200-character
focus pull against a 160-character limit (MODEL_OUTPUT_INVALID, deterministic
defaults stood in). These tests pin what the contract is for:

1. real photographic language validates at the lengths a model writes it;
2. the limits still exist - an unbounded string is still refused;
3. the one-movement rule and the authority walk are untouched by the widening;
4. the long strings reach the compiler's canonical specs unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest
from platform_contracts import (
    CinematographyPlan,
    cinematography_authority_violations,
)
from platform_contracts.shot import CanonicalCameraSpec, CanonicalLightingSpec
from pydantic import ValidationError


def _photographic(seed: str, length: int) -> str:
    """`seed` carried on in camera language to exactly `length` characters."""

    filler = " the pull eases as she settles, holding the eyes and letting the skyline soften behind her;"
    text = seed
    while len(text) < length:
        text += filler
    return text[:length]


FOCUS_200 = _photographic(
    "rack sharp from the phone screen to Mira's eyes as she looks up, then settle on the held phone at"
    " shot end,",
    200,
)
PATH_350 = _photographic(
    "start three metres behind Mira at knee height, ease forward along the ledge line, drift a hand's"
    " width camera-left to clear her shoulder, and stop a metre short of the phone,",
    350,
)
END_COMPOSITION_700 = _photographic(
    "Mira at the ledge on the right third, phone glowing at her chest, the skyline a soft band across"
    " the upper frame, rain catching the practical,",
    700,
)
DIRECTION_380 = _photographic(
    "city glow from below and camera left, a thin amber rim from the phone on the jaw and the wet"
    " collar, the rest of the face held in the teal spill,",
    380,
)
PRACTICALS_12 = [f"practical {index}" for index in range(12)]


def _long_plan() -> dict[str, Any]:
    return {
        "camera": {
            "framing": "medium close-up",
            "angle": "low angle",
            "height": "knee height",
            "position": "behind Mira, three metres back",
            "dominant_movement": "slow dolly in",
            "speed": "eases in",
            "path": PATH_350,
            "focus": FOCUS_200,
            "screen_axis": "A",
            "lens_intent": "natural perspective",
            "depth_of_field": "shallow",
        },
        "lighting": {
            "direction": DIRECTION_380,
            "quality": "soft",
            "contrast": "high",
            "color_temperature": "teal night with an amber practical",
            "practicals": PRACTICALS_12,
            "motivation": "the city below",
            "exposure_intent": "protect the phone screen",
        },
        "subject_positions": {"Mira": "centre, midground"},
        "atmosphere": "rain",
        "start_composition": "Mira small against the skyline",
        "end_composition": END_COMPOSITION_700,
        "continuity_checks": [f"check {index}" for index in range(20)],
        "unresolved": [f"open {index}" for index in range(20)],
    }


def test_the_fixture_is_as_long_as_it_claims() -> None:
    # The widening is only pinned if the strings really are past the old limits.
    assert len(FOCUS_200) == 200
    assert len(PATH_350) == 350
    assert len(END_COMPOSITION_700) == 700
    assert len(DIRECTION_380) == 380
    assert len(PRACTICALS_12) == 12


def test_real_focus_language_validates_at_its_real_length() -> None:
    plan = CinematographyPlan.model_validate(_long_plan())

    assert plan.camera.focus == FOCUS_200
    assert plan.camera.path == PATH_350
    assert plan.end_composition == END_COMPOSITION_700
    assert plan.lighting.direction == DIRECTION_380
    assert plan.lighting.practicals == PRACTICALS_12
    assert len(plan.continuity_checks) == 20
    assert len(plan.unresolved) == 20


def test_the_limits_still_bound_the_prompt() -> None:
    with pytest.raises(ValidationError):
        CinematographyPlan.model_validate({**_long_plan(), "camera": {"focus": _photographic("x", 401)}})
    with pytest.raises(ValidationError):
        CinematographyPlan.model_validate({**_long_plan(), "end_composition": _photographic("x", 801)})
    with pytest.raises(ValidationError):
        CinematographyPlan.model_validate(
            {**_long_plan(), "lighting": {"practicals": [f"practical {index}" for index in range(13)]}}
        )


def test_one_dominant_movement_is_still_one() -> None:
    with pytest.raises(ValidationError, match="one dominant camera movement"):
        CinematographyPlan.model_validate(
            {**_long_plan(), "camera": {**_long_plan()["camera"], "dominant_movement": "dolly in and orbit"}}
        )


def test_the_authority_walk_still_flags_story_and_routing_keys() -> None:
    assert cinematography_authority_violations({**_long_plan(), "dominant_action": "she jumps"}) == [
        "dominant_action"
    ]
    nested = {**_long_plan(), "lighting": {**_long_plan()["lighting"], "provider": "kling"}}
    assert cinematography_authority_violations(nested) == ["lighting.provider"]
    in_a_list = {**_long_plan(), "continuity_checks": ["fine", {"model": "veo"}]}
    assert cinematography_authority_violations(in_a_list) == ["continuity_checks[1].model"]
    assert cinematography_authority_violations(_long_plan()) == []


def test_the_long_strings_reach_the_canonical_specs_unchanged() -> None:
    plan = CinematographyPlan.model_validate(_long_plan())

    camera_values = plan.camera_values()
    assert camera_values["focus"] == FOCUS_200
    assert camera_values["path"] == PATH_350
    lighting_values = plan.lighting_values()
    assert lighting_values["direction"] == DIRECTION_380
    assert lighting_values["practicals"] == PRACTICALS_12

    # The compiler's specs are plain strings: nothing downstream shortens them.
    camera_spec = CanonicalCameraSpec(**camera_values)
    assert camera_spec.focus == FOCUS_200
    assert camera_spec.path == PATH_350
    lighting_spec = CanonicalLightingSpec.model_validate(lighting_values)
    assert lighting_spec.direction == DIRECTION_380
    assert lighting_spec.practicals == PRACTICALS_12
