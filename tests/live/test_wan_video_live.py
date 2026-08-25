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
  5-second 720P text-to-video clip. It exists to prove the request body, the
  async task protocol and the poll parsing against the real service, which no
  fixture can establish.
- `test_smallest_i2v_generation_reaches_a_terminal_state` and
  `test_smallest_r2v_generation_reaches_a_terminal_state` **are billed** — one
  2-second 720P clip each, at the published duration floor. They are the only
  tests that exercise `input.media` against the real service, so they are the
  only ones that can confirm `media.type` carries the semantic role. They need
  object storage: `reference_plate` writes one plate, presigns a GET the
  provider fetches, and deletes it.

T2V is the only mode testable here without object storage. I2V and R2V each
carry a reference the provider fetches itself, and with `S3_*` unset there is
no URL Alibaba can reach — `scripts/preflight_live.py` reports that directly,
and the I2V/R2V tests skip rather than inventing a URL.

Per `tests/live/README.md`: no credential appears here, output is never
promoted to a canonical asset, and there is no fallback from a missing fixture
into a network call.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import urllib.request

import pytest
from provider_sdk import LiveProviderSettings, ProviderError
from provider_sdk.transport import MockProviderTransport, ProviderHttpResponse
from wan_provider import WanProvider
from wan_provider.adapter import WanMediaRole

pytestmark = pytest.mark.live_provider

SYNTHESIS = ("POST", "/services/aigc/video-generation/video-synthesis")

# The smallest thing worth asking for: no references, shortest duration, and the
# lowest tier Wan 2.7 publishes — 720P; it rejects anything below. One subject,
# one action, because a multi-beat prompt is the documented failure mode and
# would turn a transport test into a quality one.
SMALLEST_T2V = {
    "model": "wan-2.7",
    "prompt": "a plain matte grey ceramic cube rotating slowly on a white surface, flat lighting",
    "duration": 5,
    "resolution": "720p",
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
            assert set(item) <= {"type", "url", "reference_voice"}, (
                "media entries carry only the official fields"
            )
            # `type` is the semantic role verbatim. Posting a media *category*
            # here — `image`/`video`/`audio`, which this adapter used to send —
            # is a request DashScope refuses, and finding that out from a billed
            # call is exactly what this free test exists to prevent.
            assert item["type"] in {role.value for role in WanMediaRole}
        assert "audio" not in body["parameters"], "there is no `parameters.audio` in any mode"
        assert "negative_prompt" not in body["parameters"], "it belongs to `input`"

    # Framing: a tier, never a pixel size, and a ratio only where nothing else
    # settles the aspect.
    provider, transport = _recording_provider()
    await provider.generate_video({**SMALLEST_T2V}, account_id="", worker_id="")
    parameters = transport.requests[0].json_body["parameters"]
    assert parameters["resolution"] == "720P"
    assert parameters["ratio"] == "16:9"
    assert "size" not in parameters


@pytest.mark.asyncio
async def test_rejected_shots_never_reach_the_provider() -> None:
    """Free. Every rule that exists to stop a wasted generation, asserted silent."""

    unusable = (
        ("an unfetchable reference", {"reference_images": ["asset-not-a-url"]}),
        ("a voice reference with no material to attach it to", {
            "reference_voice": "https://m.invalid/v.wav"
        }),
        ("a voice reference that could belong to either of two plates", {
            "reference_urls": ["https://m.invalid/a.png", "https://m.invalid/b.png"],
            "reference_voice": "https://m.invalid/v.wav",
        }),
        ("continuation plus a reference video", {
            "first_clip": "https://m.invalid/a.mp4",
            "reference_video": "https://m.invalid/b.mp4",
        }),
        ("a sixth reference asset", {
            "reference_urls": [f"https://m.invalid/{index}.png" for index in range(6)]
        }),
        ("explicit pixel dimensions", {"size": "1280*720"}),
        # T2V has no media plane, so a reference still has nowhere to go. The
        # adapter used to accept this and post the references anyway.
        ("reference stills on the text-only mode", {
            "mode": "t2v",
            "reference_urls": ["https://m.invalid/a.png"],
        }),
        # I2V's material combinations are a closed list.
        ("a reference image on i2v", {
            "start_frame_url": "https://m.invalid/a.png",
            "reference_urls": ["https://m.invalid/b.png"],
            "mode": "i2v",
        }),
        ("a last frame with nothing to grow from", {"end_frame_url": "https://m.invalid/z.png"}),
        # Published duration bounds: the floor is 2, and a reference video caps
        # R2V at 10 rather than 15.
        ("a one-second shot", {"duration": 1}),
        ("twelve seconds beside a reference video", {
            "reference_video": "https://m.invalid/a.mp4",
            "duration": 12,
        }),
        ("the shot's audio design posted as a parameter", {"audio": {"dialogue": "none"}}),
        ("a t2v audio track on an i2v shot", {
            "start_frame_url": "https://m.invalid/a.png",
            "audio_url": "https://m.invalid/track.mp3",
        }),
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
    """BILLED: one short 720P clip — the lowest tier Wan 2.7 publishes.

    Requires prior spend approval.

    Wan is asynchronous, so this submits, polls to a terminal state and asserts
    the artefact is reachable. A run that ends `FAILED` still proves the
    transport and the poll parsing; it is the provider's verdict on the prompt,
    reported rather than swallowed.
    """

    job = await _submit_and_poll(live_wan, SMALLEST_T2V, label="t2v")
    if job.status == "COMPLETED":
        assert job.output_url, "a completed Wan job must publish a fetchable artefact"
        assert job.output_mime_type == "video/mp4"


async def _submit_and_poll(provider: WanProvider, request: dict, *, label: str):  # type: ignore[no-untyped-def]
    """Submit one billed generation and poll it to a terminal state.

    A run that ends `FAILED` still proves the transport, the request body's
    *acceptance* and the poll parsing; it is the provider's verdict on the
    prompt, reported rather than swallowed. What it must not do is stay
    `QUEUED` — that means the async protocol was not understood.
    """

    submission = await provider.generate_video(
        request, account_id="live-canary", worker_id="live-canary"
    )
    assert submission.provider_job_id, "Wan accepted the request but returned no task ID"
    print(f"live wan {label} task: {submission.provider_job_id}")

    deadline = 900
    waited = 0
    job = None
    while waited < deadline:
        job = await provider.get_job(
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
    print(
        f"live wan {label} status: {job.status} error={job.error} "
        f"url_present={bool(job.output_url)}"
    )
    return job


def _reference_plate_bytes() -> bytes:
    """One deterministic 16:9 still: a matte grey cube on a white surface.

    Deliberately the same subject as `SMALLEST_T2V`, so a poor result is about
    the transport rather than about a prompt and a plate disagreeing. 1280x720
    keeps the plate's aspect identical to the requested 720P tier, which is one
    fewer thing for the provider to reconcile.
    """

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1280, 720), (244, 244, 242))
    draw = ImageDraw.Draw(image)
    draw.ellipse((430, 545, 850, 620), fill=(226, 226, 224))
    draw.polygon([(640, 210), (860, 300), (640, 390), (420, 300)], fill=(186, 186, 184))
    draw.polygon([(420, 300), (640, 390), (640, 580), (420, 490)], fill=(140, 140, 138))
    draw.polygon([(860, 300), (860, 490), (640, 580), (640, 390)], fill=(112, 112, 110))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def reference_plate():  # type: ignore[no-untyped-def]
    """A real object in the real bucket, presigned so DashScope can fetch it.

    This is the fixture that did not exist, and its absence is why I2V and R2V
    had never been exercised live: Wan fetches every reference itself, so a
    `localhost` URL or an asset ID is refused by Alibaba's fetcher *after*
    submission. `scripts/verify_object_storage.py` proves the same presigned GET
    independently and for free.

    Storage configuration is read through `Settings` — i.e. from `.env` — rather
    than `os.environ` like the Wan credentials above. That is deliberate and
    narrow: this is the one part of the environment where a quiet skip is worse
    than an explicit export, and it matches `scripts/verify_object_storage.py`.
    The object is written under `_live/` and deleted afterwards; it is never
    registered as a MediaAsset and never promoted to anything canonical.
    """

    from platform_shared import S3CompatibleStorage, Settings

    settings = Settings()
    if settings.storage_backend != "s3" or not settings.s3_bucket.strip():
        pytest.skip("object storage is not configured; I2V and R2V have no fetchable reference")

    storage = S3CompatibleStorage(
        bucket=settings.s3_bucket,
        cache_root=settings.storage_root,
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
        public_base_url=settings.public_base_url,
        max_object_bytes=settings.max_upload_bytes,
        addressing_style=settings.s3_addressing_style,
        enforce_checksum=settings.s3_enforce_upload_checksum,
    )
    payload = _reference_plate_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    key = f"_live/{digest}.png"
    # A presigned PUT, not `client.put_object`: this store answers boto3's
    # chunked upload with `NotImplemented (Aws MultiChunkedEncoding
    # STREAMING-UNSIGNED-PAYLOAD-TRAILER is not supported)`, and the presigned
    # transfer is the path the platform itself uses anyway.
    presigned = storage.presigned_upload(key, sha256=digest, mime_type="image/png", expires_in=600)
    if presigned is None:
        pytest.skip("this store would not issue a presigned PUT for the reference plate")
    put = urllib.request.Request(
        presigned.url, data=payload, method="PUT", headers=presigned.headers
    )
    with urllib.request.urlopen(put, timeout=60) as response:
        assert int(response.status) < 300, f"reference plate upload returned {response.status}"
    try:
        url = storage.presigned_reference_url(key, expires_in=1800, mime_type="image/png")
        if not url:
            pytest.skip("this store issues no presigned GET; a provider cannot fetch a reference")
        assert url.lower().startswith("https://"), "live mode refuses a non-HTTPS reference"
        yield url
    finally:
        try:
            storage.client.delete_object(Bucket=storage.bucket, Key=key)
        except Exception as exc:  # pragma: no cover - cleanup is best effort
            print(f"live wan: could not delete {key}: {exc}")


@pytest.mark.asyncio
async def test_smallest_i2v_generation_reaches_a_terminal_state(  # type: ignore[no-untyped-def]
    live_wan: WanProvider, reference_plate: str
) -> None:
    """BILLED: one 2-second 720P clip from a first frame.

    The first live exercise of `input.media` in this platform's history, and
    therefore the first real test of the field that was wrong: `media.type`
    carries `first_frame`, not the media category `image` this adapter used to
    send. It also carries `input.negative_prompt`, which used to travel in
    `parameters`. A malformed body is rejected before a task is created, so a
    protocol error here costs nothing — which is the point of running it.
    """

    if not os.environ.get("WAN2_7_I2V_MODEL_ID", "").strip():
        pytest.skip("WAN2_7_I2V_MODEL_ID is not configured")

    job = await _submit_and_poll(
        live_wan,
        {
            "model": "wan-2.7",
            "prompt": "the grey cube rotates slowly in place on the white surface, flat even lighting",
            "negative_prompt": "camera cuts, extra objects, text, watermark",
            "start_frame_url": reference_plate,
            "duration": 2,
            "resolution": "720p",
        },
        label="i2v",
    )
    if job.status == "COMPLETED":
        assert job.output_url, "a completed Wan job must publish a fetchable artefact"
        assert job.output_mime_type == "video/mp4"


@pytest.mark.asyncio
async def test_smallest_r2v_generation_reaches_a_terminal_state(  # type: ignore[no-untyped-def]
    live_wan: WanProvider, reference_plate: str
) -> None:
    """BILLED: one 2-second 720P clip from a reference still.

    R2V is the mode whose references this adapter used to resolve, pay for and
    then drop entirely, and whose `media.type` was then sent as `image`. It is
    also the mode that carries a `ratio`, because no first frame fixes the
    aspect here.
    """

    if not os.environ.get("WAN2_7_R2V_MODEL_ID", "").strip():
        pytest.skip("WAN2_7_R2V_MODEL_ID is not configured")

    job = await _submit_and_poll(
        live_wan,
        {
            "model": "wan-2.7",
            "prompt": (
                "the same grey cube rests on a white surface as the camera drifts "
                "left, flat even lighting"
            ),
            "negative_prompt": "camera cuts, extra objects, text, watermark",
            "reference_urls": [reference_plate],
            "duration": 2,
            "resolution": "720p",
            "aspect_ratio": "16:9",
        },
        label="r2v",
    )
    if job.status == "COMPLETED":
        assert job.output_url, "a completed Wan job must publish a fetchable artefact"
        assert job.output_mime_type == "video/mp4"
