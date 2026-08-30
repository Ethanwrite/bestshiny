"""Ark's images API is synchronous; the submission must carry its result.

Observed live on 2026-08-30 (production job e2d97342): the submit succeeded
and billed, the response held the finished artefact's URL, and the Gateway
then tried to *poll* Ark — whose poller rightly serves Seedance video tasks
only — so a paid image died as ``INVALID_REQUEST`` with its credits stranded
in reconciliation. The fix mirrors the OpenRouter image path: the adapter
hands the synchronous result to the Gateway's held-result completion path.
"""

from __future__ import annotations

import base64

import pytest
from provider_sdk.transport import MockProviderTransport, ProviderHttpResponse
from seedance_provider.adapter import ArkProvider


def _provider(payload: dict) -> ArkProvider:
    return ArkProvider(
        doubao_model_id="doubao-seed-2-0-lite-260428",
        seedance_model_id="doubao-seedance-2-5-260628",
        transport=MockProviderTransport(
            {("POST", "/images/generations"): ProviderHttpResponse(200, payload)}
        ),
    )


@pytest.mark.asyncio
async def test_url_form_response_completes_synchronously() -> None:
    url = "https://ark-acg-cn-beijing.tos-cn-beijing.volces.com/doubao-seedream/example.png"
    provider = _provider({"data": [{"url": url}], "model": "doubao-seedream-5-0-260128"})

    submission = await provider.generate_image(
        {"model": "doubao-seedream-5-0-260128", "prompt": "a rooftop at night"},
        account_id="",
        worker_id="",
    )

    assert submission.provider_job_id == url
    assert submission.result is not None
    assert submission.result.status == "COMPLETED"
    assert submission.result.output_url == url
    assert submission.result.outputs == []


@pytest.mark.asyncio
async def test_b64_form_response_carries_inline_bytes() -> None:
    content = b"not-really-a-png-but-bytes"
    provider = _provider(
        {"data": [{"b64_json": base64.b64encode(content).decode("ascii"), "id": "img-1"}]}
    )

    submission = await provider.generate_image(
        {"model": "doubao-seedream-5-0-260128", "prompt": "a rooftop at night"},
        account_id="",
        worker_id="",
    )

    assert submission.result is not None
    assert submission.result.status == "COMPLETED"
    assert submission.result.output_url is None
    assert [output.content for output in submission.result.outputs] == [content]


@pytest.mark.asyncio
async def test_response_without_url_or_bytes_is_refused() -> None:
    provider = _provider({"data": [{"id": "img-1"}]})

    from provider_sdk import ProviderError

    with pytest.raises(ProviderError):
        await provider.generate_image(
            {"model": "doubao-seedream-5-0-260128", "prompt": "a rooftop at night"},
            account_id="",
            worker_id="",
        )
