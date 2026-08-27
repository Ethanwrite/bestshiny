"""The registry must not name a model string its provider does not publish.

This is the defect that started the whole audit: `seedance-2.5` — this platform's
logical name — was posted to Volcengine Ark as a model ID, Ark answered "the model
or endpoint does not exist", a reservation stranded and no clip was produced.
Reading every provider's own documentation on 2026-08-26 found three more of it,
plus one string that no vendor publishes at all.

    grok-video        xAI publishes grok-imagine-video / -1.5
    veo-3.1-quality   Google publishes veo-3.1-generate-preview; the string held
                      here is a Google *Flow UI label*, not an API model id
    wan-3.0           DashScope publishes wan3.0-video / wan3.0-video-prime
    NARWHAL           not a Google-published identifier anywhere

The last one cannot be corrected, only recorded — there is no real id to correct
it to. That distinction matters: the first three were mis-typed, NARWHAL is
unverifiable.

An adjacent finding is deliberately NOT fixed here. `grok-video-official` and
`veo-3.1-quality-official` declare `CAPABILITY_RECORD_ONLY_NO_TRANSPORT` in their
own metadata and their providers are `NotConfiguredProvider` subclasses, yet
`routable(require_live=False)` offers both. Making the declaration binding breaks
`test_router_penalizes_grok_for_rear_view_ending`, which deliberately scores grok
as a candidate — so whether a capability-record-only model may be a *recommendation*
candidate while never being a *dispatch* target is a product decision, not an audit
finding. Recorded, not decided.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DEFAULTS = Path(__file__).resolve().parents[1] / "config" / "model-registry" / "defaults.json"

# What each provider publishes, read from that provider's own documentation.
#
# `grok-video-official` and `veo-3.1-quality-official` were corrected here first
# and have since been retired outright: correcting their IDs revealed that each
# named a model an OpenRouter record already owned, on a provider whose every
# call raised PROVIDER_NOT_CONFIGURED. Their verdicts survive in the External
# Evidence Registry marked RETIRED, with the identity they were recorded under —
# see `test_external_evidence_registry.py`. Their absence from the registry is
# asserted in `test_model_contract_alignment.py`.
PUBLISHED_IDS = {
    "wan-3.0-official": "wan3.0-video",
}

# Strings that were in the registry and are not model IDs anywhere.
RETIRED_IDS = {"grok-video", "veo-3.1-quality", "wan-3.0", "seedance-2.5", "seedream-5-0"}


@pytest.fixture(scope="module")
def defaults() -> dict[str, dict]:
    document = json.loads(DEFAULTS.read_text())
    return {model["logical_name"]: model for model in document["models"]}


@pytest.mark.parametrize("logical_name,published", sorted(PUBLISHED_IDS.items()))
def test_the_registry_names_the_id_the_provider_publishes(
    defaults: dict[str, dict], logical_name: str, published: str
) -> None:
    assert defaults[logical_name]["provider_model_id"] == published


def test_no_retired_string_survives_anywhere_in_the_defaults(defaults: dict[str, dict]) -> None:
    """A logical name must never reach a provider as an API model ID — §20."""

    offenders = [
        (name, model["provider_model_id"])
        for name, model in defaults.items()
        if model["provider_model_id"] in RETIRED_IDS
    ]
    assert not offenders


@pytest.mark.parametrize("logical_name", sorted(PUBLISHED_IDS) + ["flow-narwhal-image-internal"])
def test_a_corrected_or_unverifiable_id_says_where_that_came_from(
    defaults: dict[str, dict], logical_name: str
) -> None:
    """The claim is only as good as the page it was read from."""

    source = defaults[logical_name].get("metadata_json", {}).get("provider_model_id_source", "")
    assert source
    assert "2026-08-26" in source


def test_narwhal_is_recorded_as_unverifiable_rather_than_invented(
    defaults: dict[str, dict],
) -> None:
    """There is no Google-published id to correct it to, so it keeps its string.

    Substituting a plausible-looking identifier would be the same class of error
    as the one this test file exists to prevent, pointed the other way.
    """

    narwhal = defaults["flow-narwhal-image-internal"]
    assert narwhal["provider_model_id"] == "NARWHAL"
    source = narwhal["metadata_json"]["provider_model_id_source"]
    assert "NOT a Google-published identifier" in source
