"""Opt-in live verification of the Alibaba Cloud Wan 2.7 video API.

Skipped unless the operator passes `--run-live-provider` *and* the runtime
three-part gate is satisfied, so it cannot run by accident:

```text
pytest --run-live-provider -m live_provider tests/live/test_wan_video_live.py
```

The tests are deliberately unequal in cost, cheapest first:

- `test_the_reviewed_model_ids_are_the_ones_that_get_posted` is **free** and
  opens no socket. It proves the mode inference, the model-ID mapping, the
  `media[]` shape and the framing parameters against a recording transport, so
  a mistake in any of them is caught before a single billed request.
- `test_rejected_shots_never_reach_the_provider` is **free**. Every fail-closed
  rule that exists to prevent a wasted generation is asserted to hold with no
  request made.
- `test_smallest_t2v_generation_reaches_a_terminal_state` **is billed** — one
  short 480P text-to-video clip, the smallest thing Wan will make. It exists to
  prove the request body, the async task protocol and the poll parsing against
  the real service, which no fixture can establish.

T2V is the only mode testable here without object storage. I2V and R2V each
carry a reference the provider fetches itself, and with `S3_*` unset there is
no URL Alibaba can reach — `scripts/preflight_live.py` reports that directly.

Per `tests/live/README.md`: no credential appears here, output is never
promoted to a canonical asset, and there is no fallback from a missing fixture
into a network call.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from provider_sdk import LiveProviderSettings, ProviderError
from provider_sdk.transport import MockProviderTransport, ProviderHttpResponse
from wan_provider import WanProvider

pytestmark = pytest.mark.live_provider

SYNTHESIS = ("POST", "/services/aigc/video-generation/video-synthesis")

# The smallest thing worth asking for: no references, shortest duration, lowest
# tier. A prompt with one subject and one action, because a multi-beat prompt is
# the documented failure mode and would make a transport test into a quality one.
SMALLEST_T2V = {
    "model": "wan-2.7",
    "prompt": "a plain matte grey ceramic cube rotating slowly on a white surface, flat lighting",
    "duration": 5,
    "resolution": "480p",
    "aspect_ratio": "16:9",
}


def _model_ids() -> dict[str, str]:
    return {
        "t2v": os.environ.get("WAN2_7_T2V_MODEL_ID", ""),
        "i2v": os.environ.get("WAN2_7_I2V_MODEL_ID", ""),
        "r2v": os.environ.get("WAN2_7_R2V_MODEL_ID", ""),
    }


def _recording_provider() -> tuple[WanProvider, MockProviderTransport]:
    """The configured adapter, wired to a transport that records and never sends."""

    transport = MockProviderTransport({SYNTHESIS: ProviderHttpResponse(202, {"output": {"task_id": "x"}})})
    ids = _model_ids()
    return (
        WanProvider(
            video_transport=transport,
            t2v_model_id=ids["t2v"],
            i2v_model_id=ids["i2v"],
            r2v_model_id=ids["r2v"],
            video_model_keys=os.environ.get("WAN_VIDEO_MODEL_KEYS", ""),
        ),
        transport,
    )


@pytest.fixture
def live_wan() -> WanProvider:
    """Built inside the fixture, after the live-isolation fixture has applied."""

    api_key = os.environ.get("WAN_API_KEY", "")
    base_url = os.environ.get("WAN_DASHSCOPE_BASE_URL", "")
    if not api_key.strip() or not base_url.strip():
        pytest.skip("WAN_API_KEY / WAN_DASHSCOPE_BASE_URL are not present in this environment")
    ids = _model_ids()
    if not ids["t2v"].strip():
        pytest.skip("WAN2_7_T2V_MODEL_ID is not configured")
    return WanProvider(
        api_key=api_key,
        dashscope_base_url=base_url,
        t2v_model_id=ids["t2v"],
        i2v_model_id=ids["i2v"],
        r2v_model_id=ids["r2v"],
        video_model_keys=os.environ.get("WAN_VIDEO_MODEL_KEYS", ""),
        transport_settings=LiveProviderSettings(
            provider_mode=os.environ.get("PROVIDER_MODE", "mock"),
            allow_live_provider_calls=os.environ.get("ALLOW_LIVE_PROVIDER_CALLS", "") == "true",
            live_provider_confirmation=os.environ.get("LIVE_PROVIDER_CONFIRMATION", ""),
        ),
    )


@pytest.mark.asyncio
async def test_the_reviewed_model_ids_are_the_ones_that_get_posted() -> None:
    """Free, no socket. What this environment would actually send, per mode."""

    ids = _model_ids()
    cases = (
        ("t2v", {"prompt": "one action"}),
        ("i2v", {"prompt": "one action", "start_frame_url": "https://media.invalid/a.png"}),
        ("r2v", {"prompt": "one action", "reference_urls": ["https://media.invalid/a.png"]}),
    )
    for mode, extra in cases:
        if not ids[mode].strip():
            continue
        provider, transport = _recording_provider()
        await provider.generate_video({"model": "wan-2.7", **extra}, account_id="", worker_id="")
        body = transport.requests[0].json_body
        assert body["model"] == ids[mode], f"{mode} would post {body['model']!r}, not {ids[mode]!r}"
        assert transport.requests[0].headers == {"X-DashScope-Async": "enable"}
        for item in body["input"].get("media", []):
            assert set(item) == {"type", "url"}, "media entries carry only the official fields"

    # Framing: a tier, never a pixel size, and a ratio only where nothing else
    # settles the aspect.
    provider, transport = _recording_provider()
    await provider.generate_video({**SMALLEST_T2V}, account_id="", worker_id="")
    parameters = transport.requests[0].json_body["parameters"]
    assert parameters["resolution"] == "480P"
    assert parameters["ratio"] == "16:9"
    assert "size" not in parameters


@pytest.mark.asyncio
async def test_rejected_shots_never_reach_the_provider() -> None:
    """Free. Every rule that exists to stop a wasted generation, asserted silent."""

    unusable = (
        ("an unfetchable reference", {"reference_images": ["asset-not-a-url"]}),
        ("a voice reference Wan 2.7 does not accept", {"reference_voice": "https://m.invalid/v.wav"}),
        ("continuation plus a reference video", {
            "first_clip": "https://m.invalid/a.mp4",
            "reference_video": "https://m.invalid/b.mp4",
        }),
        ("a sixth reference asset", {
            "reference_urls": [f"https://m.invalid/{index}.png" for index in range(6)]
        }),
        ("explicit pixel dimensions", {"size": "1280*720"}),
    )
    for description, extra in unusable:
        provider, transport = _recording_provider()
        with pytest.raises(ProviderError):
            await provider.generate_video(
                {"model": "wan-2.7", "prompt": "one action", **extra}, account_id="", worker_id=""
            )
        assert not transport.requests, f"{description} opened a request"


@pytest.mark.asyncio
async def test_smallest_t2v_generation_reaches_a_terminal_state(live_wan: WanProvider) -> None:
    """BILLED: one short 480P clip. Requires prior spend approval.

    Wan is asynchronous, so this submits, polls to a terminal state and asserts
    the artefact is reachable. A run that ends `FAILED` still proves the
    transport and the poll parsing; it is the provider's verdict on the prompt,
    reported rather than swallowed.
    """

    submission = await live_wan.generate_video(
        SMALLEST_T2V, account_id="live-canary", worker_id="live-canary"
    )
    assert submission.provider_job_id, "Wan accepted the request but returned no task ID"
    print(f"live wan task: {submission.provider_job_id}")

    deadline = 600
    waited = 0
    job = None
    while waited < deadline:
        job = await live_wan.get_job(
            submission.provider_job_id,
            account_id="live-canary",
            worker_id="live-canary",
            generation_type="video",
        )
        if job.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            break
        await asyncio.sleep(10)
        waited += 10

    assert job is not None and job.status != "QUEUED", f"no terminal state within {deadline}s"
    print(f"live wan status: {job.status} error={job.error} url_present={bool(job.output_url)}")
    if job.status == "COMPLETED":
        assert job.output_url, "a completed Wan job must publish a fetchable artefact"
        assert job.output_mime_type == "video/mp4"
