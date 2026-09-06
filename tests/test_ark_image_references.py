"""Reference images reach Seedream.

The Gateway resolves ``reference_asset_ids`` into short-lived object-storage
URLs and emits them as ``reference_urls`` (plus ``start_frame_url`` /
``end_frame_url``). The Ark image adapter filtered its payload through an
allowlist that never mapped those onto Seedream's ``image`` parameter, so every
reference-bearing image request was billed and executed as plain
text-to-image — a wrong image, not an error, and invisible to a suite whose
URL-mode contract test recorded the Gateway's output through a fake provider.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from platform_contracts import GenerationRequest
from production_domain.models import BrowserWorker, JobStatus, ProviderAccount
from provider_sdk import ProviderError
from provider_sdk.transport import MockProviderTransport, ProviderHttpResponse
from seedance_provider.adapter import (
    IMAGE_REFERENCE_SOURCES,
    SEEDREAM_MAX_REFERENCE_IMAGES,
    ArkProvider,
)
from video_platform_api import create_app

SEEDREAM = "doubao-seedream-5-0-260128"
REGISTRY = Path(__file__).resolve().parents[1] / "config" / "model-registry" / "defaults.json"


def _png(color: tuple[int, int, int] = (18, 92, 210)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _inline_response() -> ProviderHttpResponse:
    return ProviderHttpResponse(
        200,
        {
            "model": SEEDREAM,
            "created": 1_782_264_714,
            "data": [{"id": "img-1", "b64_json": base64.b64encode(_png()).decode("ascii")}],
            "usage": {"generated_images": 1},
        },
    )


def _provider() -> tuple[ArkProvider, MockProviderTransport]:
    transport = MockProviderTransport({("POST", "/images/generations"): _inline_response()})
    provider = ArkProvider(seedance_model_id="doubao-seedance-2-5-260628", transport=transport)
    return provider, transport


# --- 1. adapter: the Gateway's URLs become Seedream's `image` ----------------


@pytest.mark.asyncio
async def test_gateway_resolved_reference_urls_reach_seedream_as_image() -> None:
    provider, transport = _provider()
    await provider.generate_image(
        {
            "model": SEEDREAM,
            "prompt": "keep the character, change the season to winter",
            "reference_urls": ["https://media.invalid/character.png", "https://media.invalid/plate.png"],
        },
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert body["image"] == ["https://media.invalid/character.png", "https://media.invalid/plate.png"]
    # Platform field names never reach the wire.
    assert "reference_urls" not in body


@pytest.mark.asyncio
async def test_a_single_reference_is_sent_as_one_url_and_frames_lead() -> None:
    provider, transport = _provider()
    await provider.generate_image(
        {"model": SEEDREAM, "prompt": "an edit", "reference_urls": ["https://media.invalid/one.png"]},
        account_id="",
        worker_id="",
    )
    assert transport.requests[0].json_body["image"] == "https://media.invalid/one.png"  # type: ignore[index]

    provider, transport = _provider()
    await provider.generate_image(
        {
            "model": SEEDREAM,
            "prompt": "an edit anchored on its start frame",
            "start_frame_url": "https://media.invalid/start.png",
            "end_frame_url": "https://media.invalid/end.png",
            "reference_urls": ["https://media.invalid/character.png"],
        },
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert body["image"] == [
        "https://media.invalid/start.png",
        "https://media.invalid/end.png",
        "https://media.invalid/character.png",
    ]
    assert IMAGE_REFERENCE_SOURCES == ("start_frame_url", "end_frame_url", "reference_urls")
    assert "start_frame_url" not in body and "end_frame_url" not in body


@pytest.mark.asyncio
async def test_declared_and_resolved_references_merge_without_duplicates() -> None:
    provider, transport = _provider()
    await provider.generate_image(
        {
            "model": SEEDREAM,
            "prompt": "an edit",
            "image": ["https://media.invalid/a.png", "https://media.invalid/b.png"],
            "reference_urls": ["https://media.invalid/b.png", "https://media.invalid/c.png"],
        },
        account_id="",
        worker_id="",
    )
    assert transport.requests[0].json_body["image"] == [  # type: ignore[index]
        "https://media.invalid/a.png",
        "https://media.invalid/b.png",
        "https://media.invalid/c.png",
    ]


@pytest.mark.asyncio
async def test_no_reference_means_no_image_parameter() -> None:
    provider, transport = _provider()
    await provider.generate_image(
        {"model": SEEDREAM, "prompt": "text to image", "reference_urls": []},
        account_id="",
        worker_id="",
    )
    assert "image" not in transport.requests[0].json_body  # type: ignore[operator]


@pytest.mark.asyncio
async def test_a_local_asset_id_is_never_submitted_as_a_reference() -> None:
    provider, transport = _provider()
    with pytest.raises(ProviderError) as refused:
        await provider.generate_image(
            {"model": SEEDREAM, "prompt": "an edit", "reference_urls": ["asset-0123456789"]},
            account_id="",
            worker_id="",
        )
    assert refused.value.code == "PROVIDER_REFERENCE_URL_UNAVAILABLE"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_more_references_than_declared_are_refused_before_billing() -> None:
    provider, transport = _provider()
    urls = [f"https://media.invalid/{index}.png" for index in range(SEEDREAM_MAX_REFERENCE_IMAGES + 1)]
    with pytest.raises(ProviderError, match="at most"):
        await provider.generate_image(
            {"model": SEEDREAM, "prompt": "too many", "reference_urls": urls},
            account_id="",
            worker_id="",
        )
    assert transport.requests == []

    provider, transport = _provider()
    await provider.generate_image(
        {"model": SEEDREAM, "prompt": "just enough", "reference_urls": urls[:-1]},
        account_id="",
        worker_id="",
    )
    assert len(transport.requests[0].json_body["image"]) == SEEDREAM_MAX_REFERENCE_IMAGES  # type: ignore[index]


def test_the_adapter_bound_is_the_registry_declaration() -> None:
    """The registry admits requests up to `max_reference_images`; the adapter must carry them."""

    config = json.loads(REGISTRY.read_text())
    seedream = next(item for item in config["models"] if item["logical_name"] == "seedream-5.0-ark")
    profile = seedream["capability_profile"]
    assert profile["supports_reference_image"] is True
    assert profile["max_reference_images"] == SEEDREAM_MAX_REFERENCE_IMAGES


# --- 2. end to end: upload -> reference_asset_ids -> Gateway -> Ark request --


@pytest.fixture
def ark_container(tmp_path, database_url):  # type: ignore[no-untyped-def]
    """A container whose Ark credential is present, so Seedream is a registered target."""

    from platform_shared import Settings
    from video_platform_api.container import build_container

    built = build_container(
        Settings(
            _env_file=None,
            database_url=database_url,
            storage_root=tmp_path / "media",
            public_base_url="http://testserver",
            flow_project_id="flow-project-test",
            auth_required=False,
            deployment_environment="test",
            ark_api_key="offline-placeholder-never-sent",
            seedance_model_id="doubao-seedance-2-5-260628",
            local_reference_signing_key="test-reference-signing-key",
        )
    )
    try:
        yield built
    finally:
        built.database.engine.dispose()


@pytest.fixture
def ark_project(ark_container):  # type: ignore[no-untyped-def]
    from production_domain.models import Project

    with ark_container.database.session() as session:
        item = Project(title="Reference plate")
        session.add(item)
        session.flush()
        return item


def _bind_seedream(container) -> MockProviderTransport:  # type: ignore[no-untyped-def]
    provider = container.providers.get("seedance")
    assert isinstance(provider, ArkProvider)
    transport = MockProviderTransport({("POST", "/images/generations"): _inline_response()})
    provider.client.transport = transport
    provider.configured = True
    container.providers.register_model("seedance", SEEDREAM, "image", available=True)
    with container.database.session() as session:
        account = ProviderAccount(
            provider="seedance",
            account_identifier="ark@example.com",
            tier="PRO",
            credits=100,
            image_capacity=2,
            video_capacity=2,
            supported_models=[SEEDREAM],
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id="ark-worker",
            provider="seedance",
            account_id=account.id,
            connection_id="ark-connection",
            capabilities=["image", "poll"],
            max_jobs=2,
        )
        session.add(worker)
        account.worker_id = worker.id
        session.flush()
    return transport


@pytest.mark.asyncio
async def test_an_uploaded_reference_reaches_the_seedream_request(ark_container, ark_project) -> None:  # type: ignore[no-untyped-def]
    """The whole chain: multipart upload, reference_asset_ids, Gateway URL, Ark `image`."""

    container, project = ark_container, ark_project
    transport = _bind_seedream(container)
    with TestClient(create_app(container)) as client:
        uploaded = client.post(
            "/v1/assets",
            data={"project_id": project.id, "asset_type": "REFERENCE"},
            files={"file": ("plate.png", io.BytesIO(_png((200, 40, 40))), "image/png")},
        )
    assert uploaded.status_code == 200, uploaded.text
    asset_id = uploaded.json()["id"]

    job, _replayed = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="image",
            provider="seedance",
            model=SEEDREAM,
            prompt="the same plate, at dusk",
            aspect_ratio="9:16",
            reference_asset_ids=[asset_id],
            idempotency_key="seedream-reference-e2e",
        )
    )
    completed = await container.gateway.process(job.id)

    assert completed.status == JobStatus.COMPLETED.value, completed.error
    assert len(transport.requests) == 1
    body = transport.requests[0].json_body
    assert body is not None
    sent = body["image"]
    assert isinstance(sent, str), "one reference is sent as one URL"
    parsed = urlsplit(sent)
    query = dict(parse_qsl(parsed.query))
    # A short-lived signed reference minted by the platform's storage, not the
    # authenticated API route and never the local asset id.
    assert parsed.scheme in {"http", "https"}
    assert "/v1/storage/" not in sent
    assert query.get("signature") and query.get("expires")
    assert asset_id not in sent
    # And it is the uploaded object: the stored job records the resolved URL
    # under the Gateway's field name with the same path.
    with container.database.session() as session:
        from production_domain.models import GenerationJob

        stored = session.get(GenerationJob, job.id)
        assert stored is not None
        resolved = stored.provider_request_json["reference_urls"]
    assert [urlsplit(item).path for item in resolved] == [parsed.path]
