"""The three rerouted models, pinned at the wire against OpenRouter's own SKU table.

`grok-video-official` and `veo-3.1-quality-official` were capability records on
providers that could not be called — every method of `NotConfiguredProvider`
raises PROVIDER_NOT_CONFIGURED — and `wan-3.0-official` named a DashScope model
this account has no access to. All three are now served, or superseded, by the
existing OpenRouter video transport. No new adapter was written for any of them.

The facts below were read from `GET https://openrouter.ai/api/v1/videos/models`
on 2026-08-27, which is OpenRouter's own machine-readable SKU descriptor. They
are pinned here because a capability advertised in the registry that the
transport cannot honour is a promise the wire has to keep — and because a
duration or resolution outside the published set is a request that is admitted
locally and refused, after the reservation is taken.

Nothing here opens a socket: the transport is driven with a recording stub.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from openrouter_provider.adapter import VIDEO_REQUEST_FIELDS, OpenRouterProvider

# Straight from OpenRouter's videos/models descriptor, 2026-08-27.
PUBLISHED = {
    "alibaba/wan-3.0": {
        "canonical_slug": "alibaba/wan-3.0-20260824",
        "resolutions": ["480p", "720p", "1080p"],
        "durations": list(range(2, 31)),
        "frame_images": ["first_frame"],
        "generate_audio": True,
        "seed": True,
        "skus": {
            "duration_seconds_480p": "0.05",
            "duration_seconds_720p": "0.1",
            "duration_seconds_1080p": "0.2",
        },
    },
    "google/veo-3.1": {
        "canonical_slug": "google/veo-3.1-20260320",
        "resolutions": ["720p", "1080p", "4K"],
        "durations": [4, 6, 8],
        "frame_images": ["first_frame", "last_frame"],
        "generate_audio": True,
        "seed": True,
        "skus": {
            "duration_seconds_with_audio": "0.40",
            "duration_seconds_with_audio_4k": "0.60",
            "duration_seconds_without_audio": "0.20",
            "duration_seconds_without_audio_4k": "0.40",
        },
    },
    "x-ai/grok-imagine-video": {
        "canonical_slug": "x-ai/grok-imagine-video-20260512",
        "resolutions": ["480p", "720p"],
        "durations": list(range(1, 16)),
        "frame_images": ["first_frame"],
        "generate_audio": None,
        "seed": None,
        "skus": {
            "cents_per_image_input": "0.2",
            "cents_per_video_output_second_480p": "5",
            "cents_per_video_output_second_720p": "7",
        },
    },
}


class _RecordingTransport:
    """Captures the request instead of sending it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append((method, path, json_body))
        return {"id": "rec-video-1", "status": "queued"}


@pytest.fixture
def recorded() -> tuple[OpenRouterProvider, _RecordingTransport]:
    transport = _RecordingTransport()
    provider = OpenRouterProvider(api_key="test-openrouter-key", transport=transport)
    provider.client = transport  # type: ignore[assignment]
    return provider, transport


@pytest.mark.parametrize("model", sorted(PUBLISHED))
@pytest.mark.asyncio
async def test_the_transport_sends_the_model_id_openrouter_publishes(
    recorded: tuple[OpenRouterProvider, _RecordingTransport], model: str
) -> None:
    """A logical name must never reach a provider as an API model ID — §20."""

    provider, transport = recorded
    await provider.generate_video(
        {"model": model, "prompt": "a paper lantern over a wet street", "duration": 2},
        account_id="acct",
        worker_id="worker",
    )
    method, path, body = transport.calls[-1]
    assert (method, path) == ("POST", "/videos")
    assert body is not None
    assert body["model"] == model


@pytest.mark.parametrize("model", sorted(PUBLISHED))
@pytest.mark.asyncio
async def test_the_payload_carries_only_fields_the_transport_declares(
    recorded: tuple[OpenRouterProvider, _RecordingTransport], model: str
) -> None:
    """No tenancy, accounting or audit field may reach a provider — §20."""

    provider, transport = recorded
    await provider.generate_video(
        {
            "model": model,
            "prompt": "a paper lantern",
            "duration": 2,
            "resolution": "480p",
            "aspect_ratio": "16:9",
            # These must be dropped, not forwarded.
            "workspace_id": "ws-1",
            "generation_job_id": "job-1",
            "estimated_credits": 12,
        },
        account_id="acct",
        worker_id="worker",
    )
    _method, _path, body = transport.calls[-1]
    assert body is not None
    assert set(body) <= VIDEO_REQUEST_FIELDS
    serialized = json.dumps(body)
    for leaked in ("ws-1", "job-1", "estimated_credits"):
        assert leaked not in serialized


@pytest.mark.asyncio
async def test_audio_is_stated_rather_than_left_to_the_provider(
    recorded: tuple[OpenRouterProvider, _RecordingTransport],
) -> None:
    """OpenRouter defaults `generate_audio` to true and bills the audio rate.

    On Veo 3.1 that is 0.40/s against 0.20/s silent — a parameter that decides
    the bill, so the quote can only be exact for a request that names it.
    """

    provider, transport = recorded
    await provider.generate_video(
        {"model": "google/veo-3.1", "prompt": "a lantern", "duration": 4},
        account_id="acct",
        worker_id="worker",
    )
    _method, _path, body = transport.calls[-1]
    assert body is not None
    assert "generate_audio" in body


@pytest.mark.parametrize("model", sorted(PUBLISHED))
def test_the_registry_profile_matches_what_openrouter_publishes(model: str) -> None:
    """A capability flag is a promise the wire has to keep."""

    import json as _json
    from pathlib import Path

    defaults = _json.loads(
        (
            Path(__file__).resolve().parents[1] / "config" / "model-registry" / "defaults.json"
        ).read_text()
    )
    entry = next(m for m in defaults["models"] if m["provider_model_id"] == model)
    profile = entry["capability_profile"]
    published = PUBLISHED[model]

    assert set(profile["supported_resolutions"]) == set(published["resolutions"])
    assert profile["min_duration"] == min(published["durations"])
    assert profile["max_duration"] == max(published["durations"])
    # OpenRouter lists which frame images each model accepts; a model offering
    # only `first_frame` must not advertise an end frame it cannot receive.
    assert profile.get("supports_start_frame") is ("first_frame" in published["frame_images"])
    assert bool(profile.get("supports_end_frame")) is ("last_frame" in published["frame_images"])


def test_veo_records_the_discrete_durations_the_profile_cannot_hold() -> None:
    """4/6/8 is a set, and the profile holds only min and max.

    Recorded in `metadata_json`, which is persisted verbatim, so the gap is
    visible to an operator rather than implied by a range that admits 5 and 7.
    Closing it properly needs a schema change and is deliberately not done here.
    """

    import json as _json
    from pathlib import Path

    defaults = _json.loads(
        (
            Path(__file__).resolve().parents[1] / "config" / "model-registry" / "defaults.json"
        ).read_text()
    )
    veo = next(m for m in defaults["models"] if m["provider_model_id"] == "google/veo-3.1")
    assert veo["metadata_json"]["supported_durations_seconds"] == [4, 6, 8]
    assert "duration_admission_gap" in veo["metadata_json"]


def test_wan_3_0_ships_disabled_until_its_payload_contract_is_established() -> None:
    """Its first canary was billed 8.5x the quote, and that is not a pricing bug.

    The route works: OpenRouter accepted the request and completed it. What did
    not work is the request. 2s at 480p was asked for and 5s at 1080p was
    charged — this model's own defaults — so `duration` and `resolution` are not
    reaching `alibaba/wan-3.0` through the video payload.

    A quote that cannot be made exact is not a quote, and on this model the
    error is 8.5x under. It ships `live_enabled` false: the price is recorded and
    sourced, so `pricing_status` is honestly VERIFIED, but nothing may spend
    against it until the payload contract is pinned. Those are different claims
    and the registry keeps them apart.
    """

    import json as _json
    from pathlib import Path

    defaults = _json.loads(
        (
            Path(__file__).resolve().parents[1] / "config" / "model-registry" / "defaults.json"
        ).read_text()
    )
    wan = next(m for m in defaults["models"] if m["provider_model_id"] == "alibaba/wan-3.0")
    assert wan["live_enabled"] is False
    finding = wan["metadata_json"]["live_canary_2026_08_27"]
    assert "0.85" in finding and "0.10" in finding
    assert "arTFehSE3vnrba2KC3hG" in finding
