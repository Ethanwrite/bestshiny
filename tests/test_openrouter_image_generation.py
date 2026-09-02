"""The OpenRouter Image API path, end to end and at its edges.

`openai/gpt-image-2` is the project's image model. It is reached through
`POST /images`, which is *synchronous*: the response body carries the finished
images as base64 rather than a job to poll. Everything unusual in this path
follows from that one fact, so each test below pins one consequence:

1. the request body carries only documented transport fields;
2. Gateway-resolved reference URLs survive into `input_references`, so an edit
   stays an edit even when no Adapter payload was compiled;
3. a request outside the model's reviewed envelope is rejected locally, before
   it is billed;
4. an unreviewed model is rejected rather than submitted on guessed limits;
5. the returned bytes complete a Generation Job through the ordinary path;
6. a lost synchronous result is reconciled, never silently refunded or faked.
"""

from __future__ import annotations

import base64
import io

import pytest
from openrouter_provider import (
    IMAGE_MODEL_ENVELOPES,
    OpenRouterProvider,
    parse_image_model_envelopes,
)
from PIL import Image
from platform_contracts import GenerationRequest
from production_domain.models import JobStatus
from provider_sdk import ProviderError
from provider_sdk.transport import MockProviderTransport, ProviderHttpResponse

GPT_IMAGE_2 = "openai/gpt-image-2"


def _png(color: tuple[int, int, int] = (18, 92, 210)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _image_response(count: int = 1) -> ProviderHttpResponse:
    return ProviderHttpResponse(
        200,
        {
            "created": 1_782_264_714,
            "data": [
                {
                    "b64_json": base64.b64encode(_png((10 * index, 90, 200))).decode(),
                    "media_type": "image/png",
                }
                for index in range(count)
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4175, "total_tokens": 4187, "cost": 0.125},
        },
    )


def _provider(
    response: ProviderHttpResponse | None = None,
) -> tuple[OpenRouterProvider, MockProviderTransport]:
    transport = MockProviderTransport({("POST", "/images"): response or _image_response()})
    return OpenRouterProvider(transport=transport), transport


# --- 1. Reviewed envelope is the real capability descriptor -----------------


def test_shipped_envelope_matches_the_published_gpt_image_2_capabilities() -> None:
    """Recorded from GET /api/v1/images/models on 2026-08-22.

    The envelope exists so a request is rejected before it is billed. If it
    drifts from the provider's descriptor it stops protecting anything, so the
    numbers are asserted rather than merely written down.
    """

    envelope = IMAGE_MODEL_ENVELOPES[GPT_IMAGE_2]
    assert envelope.max_batch == 10
    assert envelope.max_input_references == 16
    assert envelope.qualities == frozenset({"auto", "low", "medium", "high"})
    # gpt-image-2 publishes no transparent background.
    assert envelope.backgrounds == frozenset({"auto", "opaque"})
    assert "9:16" in envelope.aspect_ratios and "21:9" in envelope.aspect_ratios


def test_operator_declared_envelopes_extend_but_never_replace_the_reviewed_table() -> None:
    parsed = parse_image_model_envelopes("vendor/some-image=4:2")
    assert parsed["vendor/some-image"].max_batch == 4
    assert parsed["vendor/some-image"].max_input_references == 2
    provider = OpenRouterProvider(
        transport=MockProviderTransport(), image_model_envelopes="vendor/some-image=4:2"
    )
    assert GPT_IMAGE_2 in provider.image_envelopes
    assert provider.image_envelopes["vendor/some-image"].max_batch == 4


@pytest.mark.parametrize(
    "declaration", ["broken", "model=", "model=0:1", "model=2:-1", "model=a:b", "model=3"]
)
def test_invalid_envelope_declaration_is_rejected_at_construction(declaration: str) -> None:
    with pytest.raises(ValueError):
        parse_image_model_envelopes(declaration)


# --- 2. Request body allowlist ----------------------------------------------


@pytest.mark.asyncio
async def test_image_request_never_forwards_internal_platform_fields() -> None:
    provider, transport = _provider()
    await provider.generate_image(
        {
            "model": GPT_IMAGE_2,
            "prompt": "a lantern-lit alley after rain",
            "n": 2,
            "aspect_ratio": "9:16",
            "quality": "high",
            "background": "opaque",
            "output_compression": 90,
            # Internal fields that must never leave the platform.
            "project_id": "project-1",
            "shot_id": "shot-1",
            "candidate_id": "candidate-1",
            "idempotency_key": "secret-idempotency",
            "cost_estimate": 1.25,
            "asset_criticality": "CANONICAL",
            "generation_policy": "TEXT_TO_IMAGE",
            "type": "image",
            "provider": "openrouter",
            "style_control": {"embedding": [0.1, 0.2]},
            "metadata": {"canonical_shot_spec": {"intent": "internal"}},
            "reference_provider_media_ids": ["internal-media"],
            "_generation_job_id": "job-1",
        },
        account_id="",
        worker_id="",
    )

    body = transport.requests[0].json_body
    assert body is not None
    assert set(body) == {
        "model",
        "prompt",
        "n",
        "aspect_ratio",
        "quality",
        "background",
        "output_compression",
    }


@pytest.mark.asyncio
async def test_image_request_requires_a_model_and_a_prompt() -> None:
    provider, _ = _provider()
    for request in ({"prompt": "no model"}, {"model": GPT_IMAGE_2, "prompt": "  "}):
        with pytest.raises(ProviderError) as error:
            await provider.generate_image(request, account_id="", worker_id="")
        assert error.value.code == "INVALID_REQUEST"


# --- 3. Editing: Gateway-resolved references reach the model ----------------


@pytest.mark.asyncio
async def test_image_edit_consumes_gateway_resolved_reference_urls() -> None:
    """A Passenger request carries no Adapter payload but still edits.

    Dropping these would silently downgrade an edit to a text-to-image
    generation against no reference — a wrong image, not an error.
    """

    provider, transport = _provider()
    await provider.generate_image(
        {
            "model": GPT_IMAGE_2,
            "prompt": "keep the character, change the season to winter",
            "start_frame_url": "https://media.invalid/start.png",
            "end_frame_url": "https://media.invalid/end.png",
            "reference_urls": ["https://media.invalid/character.png"],
        },
        account_id="",
        worker_id="",
    )

    body = transport.requests[0].json_body
    assert body is not None
    assert [entry["image_url"]["url"] for entry in body["input_references"]] == [
        "https://media.invalid/start.png",
        "https://media.invalid/end.png",
        "https://media.invalid/character.png",
    ]
    assert all(entry["type"] == "image_url" for entry in body["input_references"])
    assert "start_frame_url" not in body


@pytest.mark.asyncio
async def test_declared_and_resolved_references_are_merged_without_duplicates() -> None:
    provider, transport = _provider()
    await provider.generate_image(
        {
            "model": GPT_IMAGE_2,
            "prompt": "same character, new pose",
            "input_references": [{"type": "image_url", "image_url": {"url": "https://media.invalid/a.png"}}],
            "reference_urls": ["https://media.invalid/a.png", "https://media.invalid/b.png"],
        },
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert [entry["image_url"]["url"] for entry in body["input_references"]] == [
        "https://media.invalid/a.png",
        "https://media.invalid/b.png",
    ]


@pytest.mark.asyncio
async def test_a_local_asset_id_is_never_submitted_as_a_reference() -> None:
    """OpenRouter fetches references itself, so an unresolvable one fails closed."""

    provider, transport = _provider()
    with pytest.raises(ProviderError) as error:
        await provider.generate_image(
            {
                "model": GPT_IMAGE_2,
                "prompt": "edit",
                "reference_urls": ["a1b2c3-local-asset-id"],
            },
            account_id="",
            worker_id="",
        )
    assert error.value.code == "PROVIDER_REFERENCE_URL_UNAVAILABLE"
    assert transport.requests == []


# --- 4. Envelope enforcement happens before the paid call -------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"n": 11}, "IMAGE_ENVELOPE_EXCEEDED"),
        ({"n": 0}, "IMAGE_ENVELOPE_EXCEEDED"),
        ({"quality": "ultra"}, "IMAGE_ENVELOPE_EXCEEDED"),
        ({"background": "transparent"}, "IMAGE_ENVELOPE_EXCEEDED"),
        ({"aspect_ratio": "5:1"}, "IMAGE_ENVELOPE_EXCEEDED"),
    ],
)
async def test_request_outside_the_model_envelope_is_rejected_before_submission(
    overrides: dict[str, object], code: str
) -> None:
    provider, transport = _provider()
    with pytest.raises(ProviderError) as error:
        await provider.generate_image(
            {"model": GPT_IMAGE_2, "prompt": "one image", **overrides},
            account_id="",
            worker_id="",
        )
    assert error.value.code == code
    # Nothing was sent, so nothing can be billed.
    assert transport.requests == []


@pytest.mark.asyncio
async def test_seventeen_references_are_rejected_and_sixteen_are_accepted() -> None:
    provider, transport = _provider()
    urls = [f"https://media.invalid/{index}.png" for index in range(17)]
    with pytest.raises(ProviderError) as error:
        await provider.generate_image(
            {"model": GPT_IMAGE_2, "prompt": "edit", "reference_urls": urls},
            account_id="",
            worker_id="",
        )
    assert error.value.code == "IMAGE_ENVELOPE_EXCEEDED"
    assert transport.requests == []

    await provider.generate_image(
        {"model": GPT_IMAGE_2, "prompt": "edit", "reference_urls": urls[:16]},
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert len(body["input_references"]) == 16


@pytest.mark.asyncio
async def test_an_unreviewed_image_model_fails_closed() -> None:
    """The Flow/Wan rule, applied to images: never guess a model's limits."""

    provider, transport = _provider()
    with pytest.raises(ProviderError) as error:
        await provider.generate_image(
            {"model": "vendor/unknown-image", "prompt": "one image"},
            account_id="",
            worker_id="",
        )
    assert error.value.code == "OPENROUTER_IMAGE_MODEL_NOT_REVIEWED"
    assert transport.requests == []


# --- 5. Response handling ---------------------------------------------------


@pytest.mark.asyncio
async def test_submission_carries_the_terminal_result_and_every_batch_image() -> None:
    provider, _ = _provider(_image_response(count=3))
    submission = await provider.generate_image(
        {"model": GPT_IMAGE_2, "prompt": "three variants", "n": 3},
        account_id="",
        worker_id="",
    )
    assert submission.result is not None
    assert submission.result.status == "COMPLETED"
    assert submission.result.progress == 1.0
    assert len(submission.result.outputs) == 3
    assert submission.result.outputs[0].mime_type == "image/png"
    assert submission.result.outputs[0].content.startswith(b"\x89PNG")
    # Base64 never reaches the platform's own surfaces as an output URL.
    assert submission.result.output_url is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"created": 1, "data": []}, "MISSING_PROVIDER_OUTPUT"),
        ({"created": 1, "data": [{"b64_json": "not-base64!!"}]}, "INVALID_PROVIDER_IMAGE"),
    ],
)
async def test_an_unusable_image_response_fails_as_a_submitted_call(body: dict, code: str) -> None:
    provider, _ = _provider(ProviderHttpResponse(200, body))
    with pytest.raises(ProviderError) as error:
        await provider.generate_image(
            {"model": GPT_IMAGE_2, "prompt": "one image"}, account_id="", worker_id=""
        )
    assert error.value.code == code
    # The provider already ran, so the call must stay in reconciliation.
    assert error.value.submitted is True


# --- 6. A lost synchronous result is reconciled, not invented ---------------


@pytest.mark.asyncio
async def test_polling_an_image_job_reports_that_the_result_cannot_be_refetched() -> None:
    provider, _ = _provider()
    with pytest.raises(ProviderError) as error:
        await provider.get_job(
            "openai/gpt-image-2:1782264714",
            account_id="",
            worker_id="",
            generation_type="image",
        )
    assert error.value.code == "OPENROUTER_IMAGE_RESULT_NOT_RETRIEVABLE"
    assert error.value.submitted is True


@pytest.mark.asyncio
async def test_health_reports_the_image_capability_and_its_reviewed_models() -> None:
    provider, _ = _provider()
    health = await provider.health()
    assert "image" in health.metadata["capabilities"]
    assert GPT_IMAGE_2 in health.metadata["reviewed_image_models"]


# --- 7. Gateway completion from inline bytes --------------------------------


@pytest.fixture
def image_container(tmp_path):  # type: ignore[no-untyped-def]
    """A container whose OpenRouter credential is present but offline.

    The model registry disables a model whose provider has no credential, so
    the IMAGE_GENERATION primary only resolves once one is configured. The
    transport stays Mock, so nothing here can reach the network.
    """

    from platform_shared import Settings
    from video_platform_api.container import build_container

    return build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'platform.db'}",
            storage_root=tmp_path / "media",
            public_base_url="http://testserver",
            openrouter_api_key="offline-placeholder-never-sent",
            auth_required=False,
            deployment_environment="test",
        )
    )


@pytest.fixture
def image_project(image_container):  # type: ignore[no-untyped-def]
    from production_domain.models import Project

    with image_container.database.session() as session:
        item = Project(title="Image Episode")
        session.add(item)
        session.flush()
        return item


def _openrouter_image_target(container) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    from model_registry_core import ModelRole

    resolved = container.model_infrastructure.resolve_role(ModelRole.IMAGE_GENERATION)
    return resolved.provider, resolved.provider_model_id


def test_image_generation_role_resolves_to_gpt_image_2(image_container) -> None:  # type: ignore[no-untyped-def]
    provider_name, model_id = _openrouter_image_target(image_container)
    assert (provider_name, model_id) == ("openrouter", GPT_IMAGE_2)


def test_image_role_steps_aside_to_a_fallback_when_openrouter_has_no_credential(
    container,  # type: ignore[no-untyped-def]
) -> None:
    """A model whose provider has no credential must not stay the primary."""

    provider_name, _ = _openrouter_image_target(container)
    assert provider_name != "openrouter"


@pytest.mark.asyncio
async def test_inline_image_result_completes_the_job_and_stores_every_paid_image(
    image_container,  # type: ignore[no-untyped-def]
    image_project,  # type: ignore[no-untyped-def]
) -> None:
    """The Gateway finishes a synchronous provider through its ordinary path."""

    from production_domain.models import (
        BrowserWorker,
        MediaAsset,
        ProviderAccount,
        ProviderSynchronousResult,
        ProviderSynchronousResultOutput,
    )
    from sqlalchemy import func, select

    container, project = image_container, image_project
    provider_name, model_id = _openrouter_image_target(container)
    provider = container.providers.get(provider_name)
    assert isinstance(provider, OpenRouterProvider)
    provider.client.transport = MockProviderTransport({("POST", "/images"): _image_response(count=3)})
    provider.configured = True
    container.providers.register_model(provider_name, model_id, "image", available=True)

    with container.database.session() as session:
        account = ProviderAccount(
            provider=provider_name,
            account_identifier="openrouter@example.com",
            tier="PRO",
            credits=100,
            image_capacity=2,
            video_capacity=2,
            supported_models=[model_id],
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id="openrouter-worker",
            provider=provider_name,
            account_id=account.id,
            connection_id="openrouter-connection",
            capabilities=["image", "poll"],
            max_jobs=2,
        )
        session.add(worker)
        account.worker_id = worker.id
        session.flush()

    job, _replayed = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="image",
            provider=provider_name,
            model=model_id,
            prompt="a lantern-lit alley after rain",
            aspect_ratio="9:16",
            idempotency_key="inline-image-1",
        )
    )
    completed = await container.gateway.process(job.id)

    assert completed.status == JobStatus.COMPLETED.value
    assert completed.output_asset_id is not None
    output = container.media.get(completed.output_asset_id)
    assert output is not None
    assert output.mime_type == "image/png"
    assert output.provider == provider_name
    assert output.size_bytes > 0

    with container.database.session() as session:
        assets = session.scalars(
            select(MediaAsset).where(MediaAsset.project_id == project.id)
        ).all()
    # The batch's other two images were paid for and are kept as project media,
    # but only the first is the job's output asset.
    assert len(assets) == 3
    assert sum(1 for asset in assets if asset.id == completed.output_asset_id) == 1

    events = {event.event_type for event in container.gateway.events(job.id)}
    assert {"MEDIA_DOWNLOADED", "MEDIA_BATCH_SIBLINGS_REGISTERED", "JOB_COMPLETED"} <= events
    # The held result is consumed, not left behind for a later attempt to reuse
    # — and, unlike the process dictionary it replaced, it was durable while it
    # existed, so a worker that died between confirmation and poll would not
    # have lost a paid artefact.
    with container.database.session() as session:
        assert (
            session.scalar(
                select(func.count(ProviderSynchronousResult.id)).where(
                    ProviderSynchronousResult.generation_job_id == job.id
                )
            )
            == 0
        )
        assert session.scalar(select(func.count(ProviderSynchronousResultOutput.id))) == 0


@pytest.mark.asyncio
async def test_a_held_synchronous_result_survives_the_process_that_received_it(
    image_container,  # type: ignore[no-untyped-def]
    image_project,  # type: ignore[no-untyped-def]
) -> None:
    """The gap between a confirmed submission and its poll is crash-safe.

    A synchronous provider hands over the artefact once, in the response body.
    While it was held in a Gateway attribute, a worker dying in that window lost
    bytes the workspace had already been billed for — recoverable only as
    RECONCILIATION_REQUIRED, never as a refund or a silent success.

    The crash is simulated the only way that actually proves the point: the poll
    is driven by a **different** Gateway object, which shares nothing with the
    one that took the submission except the database.
    """

    from generation_gateway import GenerationGateway
    from production_domain.models import (
        BrowserWorker,
        GenerationJob,
        ProviderAccount,
        ProviderSynchronousResult,
        ProviderSynchronousResultOutput,
    )
    from sqlalchemy import func, select

    container, project = image_container, image_project
    provider_name, model_id = _openrouter_image_target(container)
    provider = container.providers.get(provider_name)
    assert isinstance(provider, OpenRouterProvider)
    provider.client.transport = MockProviderTransport({("POST", "/images"): _image_response(count=1)})
    provider.configured = True
    container.providers.register_model(provider_name, model_id, "image", available=True)

    with container.database.session() as session:
        account = ProviderAccount(
            provider=provider_name,
            account_identifier="crash@example.com",
            tier="PRO",
            credits=100,
            image_capacity=2,
            video_capacity=2,
            supported_models=[model_id],
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id="crash-worker",
            provider=provider_name,
            account_id=account.id,
            connection_id="crash-connection",
            capabilities=["image", "poll"],
            max_jobs=2,
        )
        session.add(worker)
        account.worker_id = worker.id
        session.flush()

    job, _replayed = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="image",
            provider=provider_name,
            model=model_id,
            prompt="a lantern-lit alley after rain",
            aspect_ratio="9:16",
            idempotency_key="inline-image-crash",
        )
    )

    # Leave the original Gateway at the instant the submission is confirmed and
    # the result is held, without polling — the exact window that used to be
    # fatal. Refusing the poll claim is how that window is reached from the
    # outside, and it is the same branch a worker takes when a competitor holds
    # the claim.
    original_claim = GenerationGateway._claim_for_polling
    GenerationGateway._claim_for_polling = lambda self, job_id: None  # type: ignore[method-assign]
    try:
        handed_off = await container.gateway.process(job.id)
    finally:
        GenerationGateway._claim_for_polling = original_claim  # type: ignore[method-assign]
    assert handed_off.status == JobStatus.SUBMITTED.value
    assert handed_off.output_asset_id is None

    # The artefact outlived the process that received it.
    with container.database.session() as session:
        held = session.scalar(
            select(ProviderSynchronousResult).where(
                ProviderSynchronousResult.generation_job_id == job.id
            )
        )
        assert held is not None
        assert held.status == "COMPLETED"
        assert session.scalar(
            select(func.count(ProviderSynchronousResultOutput.id)).where(
                ProviderSynchronousResultOutput.result_id == held.id
            )
        ) == 1

    # Reading does not consume. A poll that reads the bytes and then dies before
    # its completion commits must find them again — otherwise the fix would only
    # have moved the fatal window from "before the poll" to "mid-completion".
    first_read = container.gateway._read_synchronous_result(job.id, held.provider_job_id)
    second_read = container.gateway._read_synchronous_result(job.id, held.provider_job_id)
    assert first_read is not None and second_read is not None
    assert first_read.outputs[0].content == second_read.outputs[0].content

    # A result from a different attempt is never handed to this poll.
    assert container.gateway._read_synchronous_result(job.id, "some-other-provider-job") is None
    with container.database.session() as session:
        assert session.scalar(select(func.count(ProviderSynchronousResult.id))) == 0

    # ...which leaves nothing for the successor to complete from, so put the
    # submission back the way the crash left it.
    with container.database.session() as session:
        crashed_job = session.get(GenerationJob, job.id)
        container.gateway._hold_synchronous_result(
            session,
            crashed_job,
            provider_job_id=held.provider_job_id,
            result=first_read,
        )

    # A different Gateway — nothing shared but the database — finishes the job.
    successor = GenerationGateway(
        container.database,
        container.providers,
        container.media,
        container.gateway.scheduler,
        continuity=container.gateway.continuity,
        retry_policy=container.gateway.retry_policy,
        workspace_credits=container.gateway.workspace_credits,
        model_infrastructure=container.gateway.model_infrastructure,
        provider_mode=container.gateway.provider_mode,
        flow_affinity=container.gateway.flow_affinity,
        live_canary=container.gateway.live_canary,
    )
    completed = await successor.process(job.id)

    assert completed.status == JobStatus.COMPLETED.value
    assert completed.output_asset_id is not None
    output = container.media.get(completed.output_asset_id)
    assert output is not None
    assert output.mime_type == "image/png"
    assert output.size_bytes > 0

    with container.database.session() as session:
        assert session.scalar(select(func.count(ProviderSynchronousResult.id))) == 0
        assert session.scalar(select(func.count(ProviderSynchronousResultOutput.id))) == 0


# --- 8. The HTTP entry point defaults to the role, not to a constant ---------


def test_image_endpoint_defaults_to_the_image_generation_role(
    image_container,  # type: ignore[no-untyped-def]
    image_project,  # type: ignore[no-untyped-def]
) -> None:
    """`POST /v1/images/generations` must not hard-code a model.

    The default previously named Google Flow's NARWHAL in the handler itself, so
    changing the project's image model meant editing a route. It now resolves
    through IMAGE_GENERATION, which is the only place that choice is recorded.
    """

    from fastapi.testclient import TestClient
    from production_domain.models import GenerationJob
    from sqlalchemy import select
    from video_platform_api.main import create_app

    provider_name, model_id = _openrouter_image_target(image_container)
    image_container.providers.register_model(provider_name, model_id, "image", available=True)

    with TestClient(create_app(image_container)) as client:
        response = client.post(
            "/v1/images/generations",
            headers={"Idempotency-Key": "role-default-image-1"},
            json={"project_id": image_project.id, "prompt": "a lantern-lit alley after rain"},
        )

    assert response.status_code == 202, response.text
    with image_container.database.session() as session:
        job = session.scalar(select(GenerationJob).where(GenerationJob.id == response.json()["id"]))
        assert job is not None
        assert (job.provider, job.model) == ("openrouter", GPT_IMAGE_2)


def test_image_endpoint_still_honours_an_explicit_target(
    image_container,  # type: ignore[no-untyped-def]
    image_project,  # type: ignore[no-untyped-def]
) -> None:
    from fastapi.testclient import TestClient
    from production_domain.models import GenerationJob
    from sqlalchemy import select
    from video_platform_api.main import create_app

    with TestClient(create_app(image_container)) as client:
        response = client.post(
            "/v1/images/generations",
            headers={"Idempotency-Key": "explicit-image-1"},
            json={
                "project_id": image_project.id,
                "provider": "google_flow",
                "model": "NARWHAL",
                "prompt": "a storyboard frame",
            },
        )

    assert response.status_code == 202, response.text
    with image_container.database.session() as session:
        job = session.scalar(select(GenerationJob).where(GenerationJob.id == response.json()["id"]))
        assert job is not None
        assert (job.provider, job.model) == ("google_flow", "NARWHAL")


# --- 9. A paid batch becomes selectable candidates --------------------------
#
# `n > 1` is an opt-in that costs real money: four images are four charges. The
# workspace therefore reserves all four up front and gets four things it can
# choose between, not one result and three loose files beside it.


def test_a_batch_is_priced_for_every_image_before_the_call(image_container) -> None:  # type: ignore[no-untyped-def]
    provider_name, model_id = _openrouter_image_target(image_container)
    one = image_container.credit_pricing.estimate(
        provider=provider_name, model=model_id, media_type="image", image_count=1
    )
    four = image_container.credit_pricing.estimate(
        provider=provider_name, model=model_id, media_type="image", image_count=4
    )
    assert four.image_count == 4
    assert four.provider_cost_usd == pytest.approx(one.provider_cost_usd * 4)
    assert four.credits > one.credits


def test_video_cannot_request_a_batch(image_container) -> None:  # type: ignore[no-untyped-def]
    from platform_contracts import GenerationRequest

    with pytest.raises(ValueError, match="image generation only"):
        GenerationRequest(
            project_id="project",
            type="video",
            prompt="one action",
            image_count=3,
            idempotency_key="video-batch",
        )


@pytest.mark.asyncio
async def test_the_batch_size_reaches_the_provider_as_n() -> None:
    provider, transport = _provider(_image_response(count=3))
    await provider.generate_image(
        {"model": GPT_IMAGE_2, "prompt": "three variants", "image_count": 3},
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert body["n"] == 3
    # The platform's own field name never leaves the platform.
    assert "image_count" not in body


@pytest.mark.asyncio
async def test_every_image_in_a_batch_becomes_its_own_candidate(
    image_container,  # type: ignore[no-untyped-def]
    image_project,  # type: ignore[no-untyped-def]
) -> None:
    """Three images, three candidates, one output asset each."""

    from production_domain.models import (
        BrowserWorker,
        CandidateStatus,
        Episode,
        GenerationCandidate,
        ProviderAccount,
        Scene,
        Shot,
    )
    from sqlalchemy import select

    container, project = image_container, image_project
    provider_name, model_id = _openrouter_image_target(container)
    provider = container.providers.get(provider_name)
    provider.client.transport = MockProviderTransport({("POST", "/images"): _image_response(count=3)})
    provider.configured = True
    container.providers.register_model(provider_name, model_id, "image", available=True)

    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Episode", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="alley")
        session.add(scene)
        session.flush()
        shot = Shot(scene_id=scene.id, sequence=1, prompt="a lantern-lit alley", duration=4)
        session.add(shot)
        session.flush()
        primary = GenerationCandidate(shot_id=shot.id, attempt_number=1, status="CREATED")
        session.add(primary)
        session.flush()
        shot_id, primary_id = shot.id, primary.id

        account = ProviderAccount(
            provider=provider_name,
            account_identifier="openrouter@example.com",
            tier="PRO",
            credits=100,
            image_capacity=2,
            video_capacity=2,
            supported_models=[model_id],
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id="batch-worker",
            provider=provider_name,
            account_id=account.id,
            connection_id="batch-connection",
            capabilities=["image", "poll"],
            max_jobs=2,
        )
        session.add(worker)
        account.worker_id = worker.id
        session.flush()

    job, _replayed = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            shot_id=shot_id,
            candidate_id=primary_id,
            type="image",
            provider=provider_name,
            model=model_id,
            prompt="a lantern-lit alley after rain",
            image_count=3,
            idempotency_key="batch-candidates-1",
        )
    )
    completed = await container.gateway.process(job.id)
    assert completed.status == JobStatus.COMPLETED.value

    with container.database.session() as session:
        candidates = (
            session.scalars(
                select(GenerationCandidate)
                .where(GenerationCandidate.shot_id == shot_id)
                .order_by(GenerationCandidate.attempt_number)
            )
        ).all()
        assert len(candidates) == 3
        assert [item.attempt_number for item in candidates] == [1, 2, 3]
        outputs = [item.output_asset_id for item in candidates]
        assert all(outputs), "every image in a paid batch must be a selectable candidate"
        assert len(set(outputs)) == 3, "candidates must not share one artefact"
        assert all(item.status == CandidateStatus.VALIDATING.value for item in candidates)
        # Each sibling records the job that paid for it.
        assert {item.generation_job_id for item in candidates} == {job.id}

    events = {event.event_type for event in container.gateway.events(job.id)}
    assert "MEDIA_BATCH_SIBLINGS_REGISTERED" in events


@pytest.mark.asyncio
async def test_the_gateways_priced_resolution_never_reaches_the_images_api() -> None:
    """`resolution` is stated on every job so the bill matches the quote (gateway
    `_submit`); it is a video parameter, and gpt-image-2's descriptor lists no
    `resolution`, `size`, `output_format` or `seed`. They are dropped before the
    paid call instead of being sent for the provider to refuse after it."""

    provider, transport = _provider()
    await provider.generate_image(
        {
            "model": GPT_IMAGE_2,
            "prompt": "a lantern-lit alley after rain",
            "resolution": "720p",
            "size": "1024x1024",
            "output_format": "png",
            "seed": 7,
            "quality": "low",
            "aspect_ratio": "1:1",
        },
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert set(body) == {"model", "prompt", "quality", "aspect_ratio"}


@pytest.mark.asyncio
async def test_an_operator_declared_model_keeps_the_generic_field_set() -> None:
    transport = MockProviderTransport({("POST", "/images"): _image_response()})
    provider = OpenRouterProvider(transport=transport, image_model_envelopes="vendor/image-x=4:2")
    await provider.generate_image(
        {
            "model": "vendor/image-x",
            "prompt": "one declared model",
            "resolution": "1080p",
            "quality": "low",
        },
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert body["resolution"] == "1080p"
