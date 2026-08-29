"""What we asked for, recorded beside what we were charged.

OpenRouter's completed video job returns `usage: {cost, is_byok}` and nothing
else — no billable duration, no resolution echo, no audio flag. Confirmed
against the live API for two real jobs. So the cost is authoritative and the
*reason* for it is not recoverable from the provider at all.

Without the request side recorded next to it, an estimate that misses can only be
explained by arithmetic on two numbers. That is not hypothetical: a 2-second 480p
`alibaba/wan-3.0` clip charged USD 0.2125 against a USD 0.101 estimate, and more
than one arrangement of billable seconds and per-second rate reproduces that
figure exactly, with nothing stored that can choose between them.

Estimates stay on the vendor's **list** price regardless of what reconciliation
shows. `model_pricing_profiles` says why in its own schema — "a promotion has an
end; writing a discounted rate in as the base price is how a temporary number
becomes permanent by accident" — and a list-price estimate can only ever be
generous, where a discounted one silently under-quotes the moment the promotion
lapses. So these fields exist to make the gap *visible and attributable*, never
to talk the base rate down towards whatever was last charged.

They are read from the payload actually dispatched rather than the original
request: a parameter dropped on the way to the wire is exactly the failure this
exists to make visible, and the original request would hide it by still carrying
the value.
"""

from __future__ import annotations

import pytest
from generation_gateway.gateway import _billing_request_facts
from production_domain.models import GenerationJob


def _job(provider_request: dict | None) -> GenerationJob:
    return GenerationJob(provider_request_json=provider_request)


def test_the_parameters_that_decide_the_bill_are_recorded() -> None:
    facts = _billing_request_facts(
        _job(
            {
                "duration": 2.0,
                "resolution": "480p",
                "generate_audio": True,
                "aspect_ratio": "16:9",
                "metadata": {"resolution": "480p"},
            }
        )
    )

    assert facts["requested_duration_seconds"] == 2.0
    assert facts["requested_resolution"] == "480p"
    assert facts["requested_generate_audio"] is True
    assert facts["requested_aspect_ratio"] == "16:9"
    assert facts["requested_resolution_on_wire"] is True


def test_a_resolution_that_never_reached_the_wire_is_visible_as_such() -> None:
    """The failure that cost USD 0.85, made legible after the fact.

    Carrying the resolution only in `metadata` meant the provider was never told
    it and silently chose 1080p. Recording both the value and whether it was
    actually dispatched is what separates "we asked for 480p and were billed for
    1080p" from "we never asked".
    """

    facts = _billing_request_facts(
        _job({"duration": 2.0, "metadata": {"mode": "PASSENGER_SEAT", "resolution": "480p"}})
    )

    assert facts["requested_resolution"] == "480p", "what it was priced at"
    assert facts["requested_resolution_on_wire"] is False, "but the provider was never told"


def test_audio_is_recorded_as_requested_rather_than_assumed() -> None:
    # The production default is audio on, and stays on. What matters for
    # reconciliation is that each bill records the value that request carried,
    # so a later change of default cannot silently reinterpret old evidence.
    on = _billing_request_facts(_job({"generate_audio": True}))
    off = _billing_request_facts(_job({"generate_audio": False}))
    unset = _billing_request_facts(_job({"duration": 2.0}))

    assert on["requested_generate_audio"] is True
    assert off["requested_generate_audio"] is False
    assert unset["requested_generate_audio"] is None, "unset is not the same as false"


@pytest.mark.parametrize("payload", [None, {}, {"metadata": None}])
def test_a_job_that_never_reached_a_provider_records_nothing_invented(payload) -> None:  # type: ignore[no-untyped-def]
    facts = _billing_request_facts(_job(payload))

    assert set(facts) == {
        "requested_duration_seconds",
        "requested_resolution",
        "requested_resolution_on_wire",
        "requested_generate_audio",
        "requested_aspect_ratio",
    }
    assert facts["requested_duration_seconds"] is None
    assert facts["requested_resolution"] is None
    assert facts["requested_resolution_on_wire"] is False
