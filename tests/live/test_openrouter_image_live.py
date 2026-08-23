"""Opt-in live verification of the OpenRouter Image API.

This is the only test in the repository that can spend money. It is skipped
unless the operator passes `--run-live-provider` *and* the runtime three-part
gate is satisfied, so it cannot run by accident:

```text
pytest --run-live-provider -m live_provider tests/live/test_openrouter_image_live.py
```

The two tests are deliberately unequal in cost:

- `test_capability_descriptor_matches_the_reviewed_envelope` is a **free** GET.
  It is the one that protects the platform: it re-reads the model's published
  limits and fails if the envelope compiled into the adapter has drifted from
  them. Run it whenever the provider ships changes.
- `test_smallest_approved_generation_returns_decodable_image_bytes` **is billed**
  — one 1024x1024 low-quality image, roughly USD 0.01 at the recorded rate. It
  exists to prove the request body and response parsing are right against the
  real service, which no fixture can establish.

Per `tests/live/README.md`: no credential appears here, output is never promoted
to a canonical asset, and there is no fallback from a missing fixture into a
network call.
"""

from __future__ import annotations

import base64
import io
import os

import pytest
from openrouter_provider import IMAGE_MODEL_ENVELOPES, OpenRouterProvider
from PIL import Image
from provider_sdk import LiveProviderSettings

GPT_IMAGE_2 = "openai/gpt-image-2"

pytestmark = pytest.mark.live_provider


@pytest.fixture
def live_openrouter() -> OpenRouterProvider:
    """Built inside the fixture, after the live-isolation fixture has applied."""

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key.strip():
        pytest.skip("OPENROUTER_API_KEY is not present in this environment")
    return OpenRouterProvider(
        api_key=api_key,
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        transport_settings=LiveProviderSettings(
            provider_mode=os.environ.get("PROVIDER_MODE", "mock"),
            allow_live_provider_calls=os.environ.get("ALLOW_LIVE_PROVIDER_CALLS", "") == "true",
            live_provider_confirmation=os.environ.get("LIVE_PROVIDER_CONFIRMATION", ""),
        ),
    )


@pytest.mark.asyncio
async def test_capability_descriptor_matches_the_reviewed_envelope(
    live_openrouter: OpenRouterProvider,
) -> None:
    """Free GET. Fails when the provider's published limits move under us."""

    catalog = await live_openrouter.list_image_models()
    rows = catalog.get("data", catalog) if isinstance(catalog, dict) else catalog
    published = next((row for row in rows if row.get("id") == GPT_IMAGE_2), None)
    assert published is not None, f"{GPT_IMAGE_2} is no longer offered by OpenRouter"

    parameters = published["supported_parameters"]
    envelope = IMAGE_MODEL_ENVELOPES[GPT_IMAGE_2]
    assert parameters["n"]["max"] == envelope.max_batch
    assert parameters["input_references"]["max"] == envelope.max_input_references
    assert set(parameters["quality"]["values"]) == set(envelope.qualities)
    assert set(parameters["background"]["values"]) == set(envelope.backgrounds)
    assert set(parameters["aspect_ratio"]["values"]) == set(envelope.aspect_ratios)


@pytest.mark.asyncio
async def test_smallest_approved_generation_returns_decodable_image_bytes(
    live_openrouter: OpenRouterProvider,
) -> None:
    """BILLED: one low-quality 1:1 image. Requires prior spend approval."""

    submission = await live_openrouter.generate_image(
        {
            "model": GPT_IMAGE_2,
            "prompt": "a plain matte grey ceramic cube on a white background, centered, flat lighting",
            "n": 1,
            "quality": "low",
            "aspect_ratio": "1:1",
        },
        account_id="live-canary",
        worker_id="live-canary",
    )

    assert submission.result is not None
    assert submission.result.status == "COMPLETED"
    assert len(submission.result.outputs) == 1
    output = submission.result.outputs[0]
    assert output.mime_type.startswith("image/")
    with Image.open(io.BytesIO(output.content)) as decoded:
        decoded.verify()

    usage = submission.raw.get("usage") or {}
    # Recorded as evidence; the exact figure is the provider's, not ours.
    print(f"live gpt-image-2 usage: {usage}")
    assert base64.b64encode(output.content)
