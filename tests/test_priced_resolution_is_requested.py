"""The resolution a job is priced at is the resolution the provider is asked for.

Found by a bill, not by review. A 2-second `alibaba/wan-3.0` clip was quoted at
480p — OpenRouter prices that SKU at USD 0.05/s, so USD 0.10 — and cost USD 0.85.
The artefact came back 1920x1080 with an AAC track, and OpenRouter's own
published SKUs price 1080p at USD 0.2/s. Nothing had gone wrong at the provider:
the request never named a resolution at all.

`resolution` is carried in the request's `metadata`, and every video adapter
reads it from the top level — OpenRouter filters its payload on
`VIDEO_REQUEST_FIELDS`, Wan reads `request.get("resolution")`, Seedance maps the
same key. So it reached none of them, all three quietly took a provider default,
and the platform quoted one request while being billed for another.

These drive the real gateway rather than a copy of its transform, because a
mirrored helper can agree with itself while the dispatch path drifts.
"""

from __future__ import annotations

import pytest
from platform_contracts import GenerationRequest
from test_provider_gateway import FakeProvider, add_fake_route


async def _submitted(container, project, **request_kwargs):  # type: ignore[no-untyped-def]
    provider = FakeProvider()
    add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="a paper lantern drifting upward over a wet street at night",
            **request_kwargs,
        )
    )
    await container.gateway.process(job.id)
    return provider.submitted_requests[0]


@pytest.mark.asyncio
async def test_the_priced_resolution_is_what_the_provider_is_asked_for(container, project):  # type: ignore[no-untyped-def]
    submitted = await _submitted(
        container,
        project,
        metadata={"mode": "PASSENGER_SEAT", "resolution": "480p"},
        idempotency_key="priced-resolution-reaches-provider",
    )

    assert submitted["resolution"] == "480p", "the quoted SKU is what gets requested"
    # Still where it was, so anything reading metadata is unaffected.
    assert submitted["metadata"]["resolution"] == "480p"


@pytest.mark.asyncio
async def test_an_explicitly_requested_resolution_is_not_overwritten(container, project):  # type: ignore[no-untyped-def]
    """A caller that stated it outranks whatever metadata happens to carry."""

    submitted = await _submitted(
        container,
        project,
        provider_payload={"resolution": "720p"},
        metadata={"resolution": "480p"},
        idempotency_key="explicit-resolution-wins",
    )

    assert submitted["resolution"] == "720p"


@pytest.mark.asyncio
async def test_a_job_with_no_priced_resolution_asks_for_none(container, project):  # type: ignore[no-untyped-def]
    # Not every model takes a resolution. Inventing one would be the same error
    # in the other direction — requesting something nobody priced.
    submitted = await _submitted(
        container,
        project,
        metadata={"mode": "PASSENGER_SEAT"},
        idempotency_key="no-priced-resolution",
    )

    assert "resolution" not in submitted or not submitted["resolution"]


def test_resolution_survives_the_openrouter_payload_filter() -> None:
    """The end the money is spent at.

    `metadata` is not a wire field, which is exactly why carrying the resolution
    there alone was invisible: it was dropped silently rather than rejected.
    """

    from openrouter_provider.adapter import VIDEO_REQUEST_FIELDS

    prepared = {
        "model": "alibaba/wan-3.0",
        "prompt": "a paper lantern",
        "duration": 2.0,
        "resolution": "480p",
        "metadata": {"resolution": "480p"},
    }
    payload = {k: v for k, v in prepared.items() if k in VIDEO_REQUEST_FIELDS and v is not None}

    assert payload["resolution"] == "480p"
    assert "metadata" not in payload
