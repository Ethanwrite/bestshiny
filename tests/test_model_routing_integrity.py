"""Routing-integrity gates: every route must be real, reachable and unambiguous.

These exist because each defect they catch was found by hand at least once:
a logical name posted to a provider as if it were an API model ID, a PRIMARY
binding pointing at an unconfigured stub, and a role whose only binding could
not serve it. A hand audit does not survive the next edit; these do.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from model_registry_core.schemas import ROLE_CAPABILITY, ModelRole

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "model-registry" / "defaults.json"

# Providers wired as NotConfiguredProvider in the container: they have no
# transport, so a PRIMARY binding on one cannot execute.
STUB_PROVIDERS = {"veo_official", "grok", "omni", "kling", "runway"}

def _awaiting_operator_config(model: dict) -> bool:
    """Parked pending an operator-supplied ID, and disabled until then."""

    return model["provider_model_id"].startswith("CONFIGURE_") and not model.get("enabled", False)


@pytest.fixture(scope="module")
def config() -> dict:
    return json.loads(CONFIG_PATH.read_text("utf-8"))


@pytest.fixture(scope="module")
def models(config) -> dict:  # type: ignore[no-untyped-def]
    return {m["logical_name"]: m for m in config["models"]}


def _primary_bindings(config) -> list[dict]:  # type: ignore[no-untyped-def]
    return [
        b
        for b in config["role_bindings"]
        if b.get("binding_kind", "PRIMARY") == "PRIMARY" and "plan_tier" not in b
    ]


def test_every_binding_points_at_a_registered_model(config, models) -> None:  # type: ignore[no-untyped-def]
    for binding in config["role_bindings"]:
        assert binding["model_logical_name"] in models, (
            f"role {binding['role']} binds unregistered model {binding['model_logical_name']}"
        )


def test_every_binding_model_declares_the_roles_capability(config, models) -> None:  # type: ignore[no-untyped-def]
    for binding in config["role_bindings"]:
        role = ModelRole(binding["role"])
        model = models[binding["model_logical_name"]]
        assert ROLE_CAPABILITY[role] in model["capabilities"], (
            f"{model['logical_name']} cannot serve {role.value}: "
            f"missing capability {ROLE_CAPABILITY[role]}"
        )


def test_no_primary_binding_targets_an_unconfigured_stub_provider(config, models) -> None:  # type: ignore[no-untyped-def]
    """A PRIMARY route on a stub provider fails at dispatch, not at config time."""

    offenders = [
        f"{b['role']} -> {b['model_logical_name']} ({models[b['model_logical_name']]['provider']})"
        for b in _primary_bindings(config)
        if models[b["model_logical_name"]]["provider"] in STUB_PROVIDERS
        and not _awaiting_operator_config(models[b["model_logical_name"]])
    ]
    assert not offenders, "PRIMARY bindings on providers with no transport: " + "; ".join(offenders)


def test_no_primary_binding_targets_a_disabled_model(config, models) -> None:  # type: ignore[no-untyped-def]
    offenders = [
        f"{b['role']} -> {b['model_logical_name']}"
        for b in _primary_bindings(config)
        if not models[b["model_logical_name"]].get("enabled", False)
        and not _awaiting_operator_config(models[b["model_logical_name"]])
    ]
    assert not offenders, "PRIMARY bindings on disabled models: " + "; ".join(offenders)


def test_no_enabled_model_carries_a_configuration_placeholder(models) -> None:
    """A CONFIGURE_* value is a prompt to the operator, not an API model ID.

    It is acceptable only while the model stays disabled. Enabling a model that
    still carries one would post the placeholder string to the provider.
    """

    offenders = [
        f"{name} ({model['provider_model_id']})"
        for name, model in models.items()
        if model["provider_model_id"].startswith("CONFIGURE_") and model.get("enabled", False)
    ]
    assert not offenders, "enabled models still carrying a placeholder ID: " + "; ".join(offenders)


def test_every_role_has_exactly_one_unscoped_primary(config) -> None:
    """Two unscoped primaries for one role makes the winner depend on ordering."""

    seen: dict[str, list[str]] = {}
    for binding in _primary_bindings(config):
        seen.setdefault(binding["role"], []).append(binding["model_logical_name"])
    ambiguous = {role: names for role, names in seen.items() if len(names) > 1}
    assert not ambiguous, f"roles with more than one PRIMARY binding: {ambiguous}"


def test_fallback_bindings_rank_after_their_primary(config) -> None:
    by_role: dict[str, dict[str, int]] = {}
    for binding in config["role_bindings"]:
        if "plan_tier" in binding:
            continue
        kind = binding.get("binding_kind", "PRIMARY")
        by_role.setdefault(binding["role"], {})[kind] = binding["priority"]
    for role, kinds in by_role.items():
        if "FALLBACK" in kinds and "PRIMARY" in kinds:
            assert kinds["FALLBACK"] > kinds["PRIMARY"], (
                f"{role} fallback does not rank after its primary"
            )


def test_no_binding_of_any_kind_targets_a_provider_with_no_transport(config, models) -> None:  # type: ignore[no-untyped-def]
    """A FALLBACK on a stub cannot execute either.

    `test_no_primary_binding_targets_an_unconfigured_stub_provider` guards only
    the primary, so `VIDEO_GROK` and `VIDEO_VEO` each kept a fallback route onto
    a transportless stub: reachable-looking, unable to run, and discovered only
    once the primary failed. The model definitions stay registered — the router
    and the capability resolver read them as capability records and the router
    already excludes unconfigured providers — but nothing may *route* to them.
    """

    offenders = [
        f"{b['role']} -> {b['model_logical_name']} ({models[b['model_logical_name']]['provider']})"
        for b in config["role_bindings"]
        if models[b["model_logical_name"]]["provider"] in STUB_PROVIDERS
        and not _awaiting_operator_config(models[b["model_logical_name"]])
    ]
    assert not offenders, "role bindings on providers with no transport: " + "; ".join(offenders)


def test_wan_adapter_bounds_match_the_registry_declaration(models) -> None:  # type: ignore[no-untyped-def]
    """The adapter enforces the bounds; the registry advertises them.

    Two copies of one published limit is exactly the drift this file exists to
    catch — the adapter refusing a sixth reference while the registry routes
    shots that carry one is a failure nobody sees until a generation is billed.
    """

    from wan_provider.adapter import (
        _I2V_COMBINATIONS,
        _MODE_ROLES,
        MAX_DURATION,
        MAX_DURATION_WITH_REFERENCE_VIDEO,
        MAX_FIRST_FRAME,
        MAX_REFERENCE_ASSETS,
        MIN_DURATION,
        MIN_REFERENCE_ASSETS,
    )

    profile = models["wan-2.7-official"]["capability_profile"]
    modes = profile["provider_metadata"]["modes"]
    r2v = modes["r2v"]
    assert profile["max_reference_images"] == MAX_REFERENCE_ASSETS
    assert r2v["max_reference_assets"] == MAX_REFERENCE_ASSETS
    assert r2v["min_reference_assets"] == MIN_REFERENCE_ASSETS
    assert r2v["max_first_frame"] == MAX_FIRST_FRAME
    for mode, declared in modes.items():
        assert {role.value for role in _MODE_ROLES[mode]} == set(declared["accepts"]), (
            f"Wan {mode} accepts different roles in the adapter and the registry"
        )

    # The duration floor was declared as 1 and enforced nowhere; Wan 2.7's is 2.
    # The ceiling is request-dependent, so the profile's single `max_duration`
    # cannot be the whole rule and the mode table has to carry the exception.
    assert profile["min_duration"] == MIN_DURATION
    assert profile["max_duration"] == MAX_DURATION
    for mode, declared in modes.items():
        assert declared["min_duration"] == MIN_DURATION, f"Wan {mode} floor disagrees"
        assert declared["max_duration"] == MAX_DURATION, f"Wan {mode} ceiling disagrees"
    assert r2v["max_duration_with_reference_video"] == MAX_DURATION_WITH_REFERENCE_VIDEO

    # I2V does not accept an arbitrary subset of its roles.
    assert {frozenset(entry) for entry in modes["i2v"]["material_combinations"]} == {
        frozenset(role.value for role in combination) for combination in _I2V_COMBINATIONS
    }


def test_wan_declared_capabilities_are_ones_the_wire_can_actually_carry(models) -> None:  # type: ignore[no-untyped-def]
    """A capability flag is a promise the serializer has to be able to keep.

    Both directions fail here. A profile that claims a capability no mode
    accepts advertises an input the adapter would refuse; a mode that accepts a
    role the profile does not claim sends an input nobody authorised.

    Audio is the live case, and it moved: `supports_audio` is native audio
    *out*, while `supports_reference_voice` is documented on the profile as an
    audio asset the model conditions *on*. Wan 2.7 has two of those — I2V's
    `driving_audio` media entry and R2V's nested `reference_voice` — so the flag
    that was declared false is true, and one flag authorises both.
    """

    from wan_provider.adapter import (
        _MODE_ROLES,
        ROLE_CAPABILITY_FLAG,
        VOICE_CAPABILITY_FLAG,
        WanMedia,
        WanMediaRole,
    )

    profile = models["wan-2.7-official"]["capability_profile"]
    wire = profile["provider_metadata"]["wire_contract"]
    accepted = {role for roles in _MODE_ROLES.values() for role in roles}
    for role, flag in ROLE_CAPABILITY_FLAG.items():
        assert profile.get(flag, False) is (role in accepted), (
            f"{flag} and the modes accepting {role.value} disagree"
        )

    assert profile[VOICE_CAPABILITY_FLAG] is True
    assert WanMediaRole.DRIVING_AUDIO in _MODE_ROLES["i2v"]

    # `media.type` is the role verbatim. The registry used to declare the
    # opposite — "role is never serialized; position is the only signal" — and
    # the adapter posted image/video/audio to match it. Both were wrong, so the
    # declaration is pinned to the enum rather than hand-listed.
    assert set(wire["media_types"]) == {role.value for role in WanMediaRole}
    assert wire["negative_prompt_location"] == "input"
    assert not {"image", "video", "audio"} & set(wire["media_types"])

    for role in WanMediaRole:
        entry = WanMedia(role, "https://media.invalid/asset.bin").as_payload()
        assert entry == {"type": role.value, "url": "https://media.invalid/asset.bin"}
        assert set(entry) == set(wire["media_fields"])

    # A voice reference is a field *on* a reference material, never an entry.
    voiced = WanMedia(
        WanMediaRole.REFERENCE_IMAGE,
        "https://media.invalid/face.png",
        "https://media.invalid/voice.mp3",
    ).as_payload()
    assert voiced == {
        "type": "reference_image",
        "url": "https://media.invalid/face.png",
        "reference_voice": "https://media.invalid/voice.mp3",
    }
    assert set(voiced) - set(wire["media_fields"]) == set(
        wire["media_entry_optional_fields"]
    )
