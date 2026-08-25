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

    from wan_provider.adapter import _MODE_ROLES, MAX_FIRST_FRAME, MAX_REFERENCE_ASSETS

    profile = models["wan-2.7-official"]["capability_profile"]
    r2v = profile["provider_metadata"]["modes"]["r2v"]
    assert profile["max_reference_images"] == MAX_REFERENCE_ASSETS
    assert r2v["max_reference_assets"] == MAX_REFERENCE_ASSETS
    assert r2v["max_first_frame"] == MAX_FIRST_FRAME
    for mode, declared in profile["provider_metadata"]["modes"].items():
        assert {role.value for role in _MODE_ROLES[mode]} == set(declared["accepts"]), (
            f"Wan {mode} accepts different roles in the adapter and the registry"
        )


def test_wan_declared_capabilities_are_ones_the_wire_can_actually_carry(models) -> None:  # type: ignore[no-untyped-def]
    """A capability flag is a promise the serializer has to be able to keep.

    Both directions fail here. A profile that claims a capability no mode
    accepts advertises an input the adapter would refuse; a mode that accepts a
    role the profile does not claim sends an input nobody authorised. Voice is
    the live case: `supports_audio` means native audio *out*, so a voice
    reference carried *in* needed its own flag rather than riding on that one.
    """

    from wan_provider.adapter import _MODE_ROLES, ROLE_CAPABILITY_FLAG, WanMedia, WanMediaRole

    profile = models["wan-2.7-official"]["capability_profile"]
    accepted = {role for roles in _MODE_ROLES.values() for role in roles}
    for role, flag in ROLE_CAPABILITY_FLAG.items():
        assert profile.get(flag, False) is (role in accepted), (
            f"{flag} and the modes accepting {role.value} disagree"
        )

    # Wan 2.7 declares no voice reference, so nothing may route one.
    assert profile["supports_reference_voice"] is False
    assert WanMediaRole.REFERENCE_VOICE not in accepted

    # The serializer is nonetheless ready for the day the flag flips: a voice
    # reference has a wire form, and it is still just type + url.
    voiced = WanMedia(WanMediaRole.REFERENCE_VOICE, "https://media.invalid/voice.wav").as_payload()
    assert voiced == {"type": "audio", "url": "https://media.invalid/voice.wav"}
    assert set(voiced) == set(profile["provider_metadata"]["wire_contract"]["media_fields"])
    assert voiced["type"] in profile["provider_metadata"]["wire_contract"]["media_types"]
