"""Integrity of the shipped scene-champion table.

The table is hand-authored policy; these tests make its referential integrity
a gate, the way test_model_routing_integrity does for role bindings: every
champion must name a registered, enabled video model, every scene key must be
one the router can actually derive, and known-unroutable models must not be
championed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from model_registry_core import SceneChampionTable, load_scene_champions
from pydantic import ValidationError

CONFIG_ROOT = Path(__file__).resolve().parents[1] / "config" / "model-registry"

# Every scenario router_scenario() can return, in its documented precedence
# order (core/router-evidence/router_evidence_core/routing_context.py). A scene
# key outside this set is dead policy: valid vocabulary the router never
# derives, so its champions would never be consulted.
DERIVABLE_SCENES = {
    "chinese_text",
    "dialogue_lipsync",
    "text_rendering",
    "first_last_frame",
    "reference_adherence",
    "commercial_product",
    "physics",
    "motion",
    "camera_motion",
    "identity",
    "generic",
}


@pytest.fixture(scope="module")
def table() -> SceneChampionTable:
    return load_scene_champions(CONFIG_ROOT / "scene-champions.json")


@pytest.fixture(scope="module")
def registry_models() -> dict[str, dict]:
    payload = json.loads((CONFIG_ROOT / "defaults.json").read_text(encoding="utf-8"))
    return {model["logical_name"]: model for model in payload["models"]}


def test_every_champion_is_a_registered_enabled_video_model(table, registry_models) -> None:
    for scene, entry in table.scenes.items():
        for binding in entry.champions:
            model = registry_models.get(binding.logical_name)
            assert model is not None, f"{scene}: {binding.logical_name} is not in the registry"
            assert model["modality"] == "video", f"{scene}: {binding.logical_name} is not a video model"
            assert model["enabled"] is True, f"{scene}: {binding.logical_name} is disabled"
            operations = model.get("capability_profile", {}).get("supported_operations", [])
            assert "video_generation" in operations, (
                f"{scene}: {binding.logical_name} does not declare video_generation"
            )


def test_every_scene_key_is_derivable_by_the_router(table) -> None:
    dead = sorted(set(table.scenes) - DERIVABLE_SCENES)
    assert not dead, f"scene keys the router never derives: {dead}"


def test_every_scene_names_a_primary_and_at_least_one_fallback(table) -> None:
    thin = sorted(scene for scene, entry in table.scenes.items() if len(entry.champions) < 2)
    assert not thin, f"scenes without a fallback: {thin}"


def test_no_champion_is_a_known_unquotable_route(table) -> None:
    """flow-veo-3.1 and NARWHAL have no verified price (OPEN_ISSUES 2.35) and
    wan-3.0-official is disabled for want of DashScope access (1.9); a
    champion that cannot quote or run turns its scene into a guaranteed
    fallback, silently."""

    unroutable = {"flow-veo-3.1-internal", "flow-narwhal-image-internal", "wan-3.0-official"}
    offenders = sorted(
        f"{scene}:{binding.logical_name}"
        for scene, entry in table.scenes.items()
        for binding in entry.champions
        if binding.logical_name in unroutable
    )
    assert not offenders, f"unroutable champions: {offenders}"


def test_the_loader_refuses_an_unknown_scene_key() -> None:
    with pytest.raises(ValidationError, match="unknown scenario keys"):
        SceneChampionTable.model_validate(
            {
                "version": "x",
                "scenes": {
                    "car_chase": {
                        "champions": [{"logical_name": "m", "rationale": "r"}],
                    }
                },
            }
        )


def test_the_loader_refuses_a_duplicate_champion_within_a_scene() -> None:
    with pytest.raises(ValidationError, match="duplicate champion"):
        SceneChampionTable.model_validate(
            {
                "version": "x",
                "scenes": {
                    "motion": {
                        "champions": [
                            {"logical_name": "m", "rationale": "r"},
                            {"logical_name": "m", "rationale": "again"},
                        ],
                    }
                },
            }
        )


def test_the_loader_refuses_the_any_scenario() -> None:
    """ANY is an aggregation sentinel in the evidence keys, not a scene a shot
    can be; championing it would be a wildcard the precedence chain never
    reaches."""

    with pytest.raises(ValidationError, match="unknown scenario keys"):
        SceneChampionTable.model_validate(
            {
                "version": "x",
                "scenes": {
                    "ANY": {"champions": [{"logical_name": "m", "rationale": "r"}]},
                },
            }
        )
