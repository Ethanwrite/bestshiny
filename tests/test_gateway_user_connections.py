"""Generation jobs paid by a workspace's own connection, through the one gateway.

What these tests pin:

1. who pays - a connection job reserves no credits, carries no quote, takes no
   spend authorization and needs no canary permit even in live mode, and its
   billing evidence names the workspace's account as the payer;
2. whose key - a job is scheduled only on its own connection's capacity, never
   another connection's, and a connection another workspace owns is refused;
3. the boundary - a connection removed before submission stops the job with
   nothing sent, and the provider name and connection always travel together;
4. the wire - through a connection, the reviewed OpenRouter, Ark and DashScope
   request builders produce exactly the requests they produce for the platform,
   on the connection's host with the connection's key;
5. the media fence - a connection job's artefact is fetched from any public
   host but never from a private one, and its key reaches only the API host.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket

import httpx
import pytest
from connection_core import generation as connection_generation
from fastapi.testclient import TestClient
from generation_gateway import IdempotencyConflict
from generation_gateway.providers import GenerationTargetError
from generation_gateway.worker import process_next_job
from media_service import registry as registry_module
from media_service.registry import RemoteMediaSecurityError
from platform_contracts import GenerationRequest
from production_domain.models import (
    GenerationJob,
    GenerationSpendAuthorization,
    JobStatus,
    Project,
    ProviderAccount,
    ProviderBillingEvidence,
    User,
    Workspace,
    WorkspaceCreditEntry,
)
from provider_sdk import ProviderMode, RemoteMediaFetchPolicy
from sqlalchemy import select

KEY = "sk-or-v1-" + "0123456789abcdef"


def _workspace_project(container, *, plan: str = "PRO", credits: int = 500) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        user = User(email=f"gw-{plan}-{credits}-{id(session)}@example.com", display_name="GW")
        session.add(user)
        session.flush()
        workspace = Workspace(owner_user_id=user.id, name="GW", plan_tier=plan, credit_balance=credits)
        session.add(workspace)
        session.flush()
        project = Project(title="Canvas", workspace_id=workspace.id)
        session.add(project)
        session.flush()
        return project.id, workspace.id


def _mock_connection(container, workspace_id: str, name: str = "Mock") -> str:  # type: ignore[no-untyped-def]
    container.connections.mock_protocol_enabled = True
    return container.connections.create_connection(
        workspace_id, user_id=None, name=name, protocol="mock", api_key=""
    ).id


def _image_request(project_id: str, key: str = "canvas-test-image") -> GenerationRequest:
    return GenerationRequest(
        project_id=project_id,
        type="image",
        provider="byok",
        model="mock-image",
        prompt="a lighthouse at dusk",
        aspect_ratio="16:9",
        idempotency_key=key,
    )


async def _drain(container, limit: int = 20) -> None:  # type: ignore[no-untyped-def]
    for _ in range(limit):
        if not await process_next_job(container):
            return


# ---------------------------------------------------------------------------
# 1. Who pays
# ---------------------------------------------------------------------------


async def test_a_connection_job_charges_nothing_and_completes_through_the_gateway(container):  # type: ignore[no-untyped-def]
    project_id, workspace_id = _workspace_project(container)
    connection_id = _mock_connection(container, workspace_id)

    job, replayed = container.visual_runtime.submit(
        _image_request(project_id), mode="CANVAS", connection_id=connection_id
    )
    assert replayed is False
    assert job.provider == "byok" and job.connection_id == connection_id
    assert job.workspace_credit_required is False
    assert job.quoted_credits == 0

    await _drain(container)

    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        assert stored.status == JobStatus.COMPLETED.value, (stored.error_code, stored.error_message)
        assert stored.output_asset_id
        assert stored.account_id == container.connections.resource_account_id(connection_id)
        assert session.get(Workspace, workspace_id).credit_balance == 500
        assert (
            session.scalar(
                select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
            )
            is None
        )
        evidence = session.scalar(
            select(ProviderBillingEvidence).where(ProviderBillingEvidence.generation_job_id == job.id)
        )
        assert evidence is not None
        assert evidence.metadata_json["billing_owner"] == "USER_CONNECTION"
        account = session.get(ProviderAccount, stored.account_id)
        assert account.provider == "byok"
        assert account.image_inflight == 0, "capacity is released when the job completes"


async def test_live_mode_fences_do_not_apply_to_a_connection_job(container):  # type: ignore[no-untyped-def]
    project_id, workspace_id = _workspace_project(container)
    connection_id = _mock_connection(container, workspace_id)
    container.gateway.provider_mode = ProviderMode.LIVE
    # Without a permit service every platform live generation is refused; a
    # connection job spends no platform money and needs none.
    container.gateway.live_canary = None

    job, _ = container.visual_runtime.submit(
        _image_request(project_id, "live-connection"), mode="CANVAS", connection_id=connection_id
    )
    await _drain(container)
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        assert stored.status == JobStatus.COMPLETED.value, (stored.error_code, stored.error_message)
        assert (
            session.scalar(
                select(GenerationSpendAuthorization).where(
                    GenerationSpendAuthorization.generation_job_id == job.id
                )
            )
            is None
        )


def test_a_caller_cannot_attach_a_credit_quote_to_a_connection_job(container):  # type: ignore[no-untyped-def]
    from entitlement_core import WorkspaceCreditConflict

    project_id, workspace_id = _workspace_project(container)
    connection_id = _mock_connection(container, workspace_id)
    with pytest.raises(WorkspaceCreditConflict):
        container.gateway.create(
            _image_request(project_id), estimated_credits=10, connection_id=connection_id
        )


# ---------------------------------------------------------------------------
# 2. Whose key
# ---------------------------------------------------------------------------


async def test_a_job_is_scheduled_only_on_its_own_connections_capacity(container):  # type: ignore[no-untyped-def]
    project_id, workspace_id = _workspace_project(container)
    mine = _mock_connection(container, workspace_id, "Mine")
    other_project, other_workspace = _workspace_project(container, credits=100)
    theirs = _mock_connection(container, other_workspace, "Theirs")
    container.connections.ensure_resources(theirs)
    my_account = container.connections.ensure_resources(mine)
    with container.database.session() as session:
        account = session.get(ProviderAccount, my_account)
        account.image_inflight = account.image_capacity

    job, _ = container.visual_runtime.submit(
        _image_request(project_id, "pinned"), mode="CANVAS", connection_id=mine
    )
    await container.gateway.process(job.id)
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        assert stored.status == JobStatus.RETRY_WAIT.value
        assert stored.error_code == "NO_ACCOUNT"
        assert stored.account_id is None, "a full connection waits; it never borrows another workspace's key"
        their_account = session.get(ProviderAccount, container.connections.resource_account_id(theirs))
        assert their_account.image_inflight == 0


def test_a_connection_of_another_workspace_is_refused_at_creation(container):  # type: ignore[no-untyped-def]
    project_id, _workspace_id = _workspace_project(container)
    _other_project, other_workspace = _workspace_project(container, credits=10)
    foreign = _mock_connection(container, other_workspace, "Foreign")
    with pytest.raises(GenerationTargetError) as refused:
        container.visual_runtime.submit(
            _image_request(project_id, "foreign"), mode="CANVAS", connection_id=foreign
        )
    assert refused.value.code == "CONNECTION_WORKSPACE_MISMATCH"
    with container.database.session() as session:
        assert session.scalar(select(GenerationJob).where(GenerationJob.project_id == project_id)) is None


def test_a_connection_not_enabled_for_the_media_type_is_refused(container):  # type: ignore[no-untyped-def]
    project_id, workspace_id = _workspace_project(container)
    container.connections.mock_protocol_enabled = True
    chat_only = container.connections.create_connection(
        workspace_id, user_id=None, name="Chat", protocol="mock", api_key="", capabilities=["chat"]
    ).id
    with pytest.raises(GenerationTargetError) as refused:
        container.visual_runtime.submit(
            _image_request(project_id, "chat-only"), mode="CANVAS", connection_id=chat_only
        )
    assert refused.value.code == "CONNECTION_CAPABILITY_MISSING"


# ---------------------------------------------------------------------------
# 3. The boundary
# ---------------------------------------------------------------------------


async def test_a_connection_removed_before_submission_stops_the_job_unsent(container):  # type: ignore[no-untyped-def]
    project_id, workspace_id = _workspace_project(container)
    connection_id = _mock_connection(container, workspace_id)
    job, _ = container.visual_runtime.submit(
        _image_request(project_id, "removed"), mode="CANVAS", connection_id=connection_id
    )
    container.connections.delete_connection(workspace_id, connection_id, user_id=None)
    await container.gateway.process(job.id)
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        assert stored.status == JobStatus.FAILED.value
        assert stored.error_code == "CONNECTION_UNAVAILABLE"
        assert stored.submission_state == "NOT_SENT"
        assert stored.provider_job_id is None


def test_the_provider_name_and_the_connection_travel_together(container):  # type: ignore[no-untyped-def]
    project_id, workspace_id = _workspace_project(container)
    connection_id = _mock_connection(container, workspace_id)
    with pytest.raises(GenerationTargetError) as unnamed:
        container.gateway.create(_image_request(project_id, "no-connection"))
    assert unnamed.value.code == "CONNECTION_TARGET_MISMATCH"
    platform_named = _image_request(project_id, "platform-name").model_copy(
        update={"provider": "openrouter", "model": "openai/gpt-image-2"}
    )
    with pytest.raises(GenerationTargetError) as mismatched:
        container.gateway.create(platform_named, connection_id=connection_id)
    assert mismatched.value.code == "CONNECTION_TARGET_MISMATCH"


def test_one_idempotency_key_cannot_name_two_connections(container):  # type: ignore[no-untyped-def]
    project_id, workspace_id = _workspace_project(container)
    first = _mock_connection(container, workspace_id, "First")
    second = _mock_connection(container, workspace_id, "Second")
    job, _ = container.gateway.create(_image_request(project_id, "shared-key"), connection_id=first)
    replay, replayed = container.gateway.create(_image_request(project_id, "shared-key"), connection_id=first)
    assert replayed and replay.id == job.id
    with pytest.raises(IdempotencyConflict):
        container.gateway.create(_image_request(project_id, "shared-key"), connection_id=second)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the mock video protocol renders with FFmpeg")
async def test_a_connection_video_is_polled_to_completion(container, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(connection_generation, "MOCK_VIDEO_RENDER_SECONDS", 0.0)
    project_id, workspace_id = _workspace_project(container)
    connection_id = _mock_connection(container, workspace_id)
    job, _ = container.visual_runtime.submit(
        GenerationRequest(
            project_id=project_id,
            type="video",
            provider="byok",
            model="mock-video",
            prompt="a slow push-in on a lighthouse",
            duration=2,
            aspect_ratio="16:9",
            idempotency_key="connection-video",
        ),
        mode="CANVAS",
        resolution="720p",
        connection_id=connection_id,
    )
    container.gateway.poll_interval_seconds = 0.01
    for _ in range(40):
        await _drain(container)
        with container.database.session() as session:
            if session.get(GenerationJob, job.id).status in {"COMPLETED", "FAILED"}:
                break
        await asyncio.sleep(0.05)
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        assert stored.status == JobStatus.COMPLETED.value, (stored.error_code, stored.error_message)
        assert stored.output_asset_id


# ---------------------------------------------------------------------------
# 4. The wire through a connection
# ---------------------------------------------------------------------------


def _capture(container, responses):  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses(request)

    container.connections._client_factory = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    )
    return seen


async def test_openrouter_video_through_a_connection(container):  # type: ignore[no-untyped-def]
    project_id, workspace_id = _workspace_project(container)
    connection_id = container.connections.create_connection(
        workspace_id, user_id=None, name="OpenRouter", protocol="openrouter", api_key=KEY
    ).id
    seen = _capture(
        container,
        lambda request: (
            httpx.Response(200, json={"id": "vid-1"})
            if request.method == "POST"
            else httpx.Response(
                200,
                json={
                    "status": "completed",
                    "unsigned_urls": ["https://openrouter.ai/api/v1/videos/vid-1/content"],
                },
            )
        ),
    )
    provider = container.connections.generation_provider(
        connection_id, project_id=project_id, media_type="video"
    )
    assert provider.name == "byok"
    submission = await provider.generate_video(
        {
            "model": "kwaivgi/kling-v3.0-pro",
            "prompt": "a slow push-in",
            "duration": 5,
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "metadata": {"connection_options": {"generate_audio": False}},
        },
        account_id="a",
        worker_id="w",
    )
    assert submission.provider_job_id == "vid-1"
    request = seen[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/videos"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "kwaivgi/kling-v3.0-pro"
    assert body["generate_audio"] is False, "the node's audio choice reaches the wire"
    job = await provider.get_job("vid-1", account_id="a", worker_id="w", generation_type="video")
    assert job.status == "COMPLETED" and job.output_url.endswith("/vid-1/content")
    policy = provider.media_fetch_policy
    assert policy.allowed_host_patterns is None
    assert policy.credential_host == "openrouter.ai"
    assert KEY not in repr(policy)


async def test_ark_and_dashscope_video_through_connections(container):  # type: ignore[no-untyped-def]
    project_id, workspace_id = _workspace_project(container)
    ark = container.connections.create_connection(
        workspace_id, user_id=None, name="Ark", protocol="volcengine_ark", api_key=KEY
    ).id
    wan = container.connections.create_connection(
        workspace_id,
        user_id=None,
        name="Wan",
        protocol="alibaba_dashscope",
        api_key=KEY,
        base_url="https://dashscope-intl.aliyuncs.com",
    ).id
    seen = _capture(
        container,
        lambda request: (
            httpx.Response(200, json={"id": "cgt-1"})
            if "volces" in request.url.host
            else httpx.Response(200, json={"output": {"task_id": "task-1", "task_status": "PENDING"}})
        ),
    )
    ark_provider = container.connections.generation_provider(ark, project_id=project_id, media_type="video")
    await ark_provider.generate_video(
        {
            "model": "doubao-seedance-2-5-260628",
            "prompt": "rain on glass",
            "duration": 5,
            "aspect_ratio": "16:9",
        },
        account_id="a",
        worker_id="w",
    )
    assert str(seen[0].url) == "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
    ark_body = json.loads(seen[0].content)
    assert ark_body["model"] == "doubao-seedance-2-5-260628"
    assert ark_body["content"][0] == {"type": "text", "text": "rain on glass"}
    assert ark_provider.media_fetch_policy.credential_header is None

    wan_provider = container.connections.generation_provider(wan, project_id=project_id, media_type="video")
    await wan_provider.generate_video(
        {"model": "wan2.7-t2v-2026-06-12", "prompt": "rain on glass", "duration": 5, "resolution": "720p"},
        account_id="a",
        worker_id="w",
    )
    wan_request = seen[1]
    assert (
        str(wan_request.url)
        == "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
    )
    assert wan_request.headers["x-dashscope-async"] == "enable"
    assert json.loads(wan_request.content)["model"] == "wan2.7-t2v-2026-06-12", (
        "a connection's DashScope model id is sent as chosen, not mapped from a logical name"
    )


def test_a_connection_that_cannot_generate_says_so(container):  # type: ignore[no-untyped-def]
    project_id, workspace_id = _workspace_project(container)
    anthropic = container.connections.create_connection(
        workspace_id, user_id=None, name="Claude", protocol="anthropic", api_key=KEY
    ).id
    from connection_core import ConnectionUnavailable

    with pytest.raises(ConnectionUnavailable):
        container.connections.generation_provider(anthropic, project_id=project_id, media_type="video")


# ---------------------------------------------------------------------------
# 5. The media fence for a connection job
# ---------------------------------------------------------------------------


def _resolve_to(monkeypatch, address: str) -> None:  # type: ignore[no-untyped-def]
    def fake_getaddrinfo(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(registry_module.socket, "getaddrinfo", fake_getaddrinfo)


async def test_policy_admits_any_public_host_but_keeps_every_other_check(container, monkeypatch):  # type: ignore[no-untyped-def]
    media = container.media
    policy = RemoteMediaFetchPolicy(allowed_host_patterns=None)
    _resolve_to(monkeypatch, "93.184.216.34")
    await media._validate_remote_url("https://cdn.provider.example/v.mp4", provider="byok", policy=policy)
    with pytest.raises(RemoteMediaSecurityError, match="not allowlisted"):
        await media._validate_remote_url("https://cdn.provider.example/v.mp4", provider="byok")
    with pytest.raises(RemoteMediaSecurityError, match="HTTPS"):
        await media._validate_remote_url("http://cdn.provider.example/v.mp4", provider="byok", policy=policy)
    with pytest.raises(RemoteMediaSecurityError, match="authority"):
        await media._validate_remote_url(
            "https://cdn.provider.example:8443/v.mp4", provider="byok", policy=policy
        )
    _resolve_to(monkeypatch, "10.0.0.7")
    with pytest.raises(RemoteMediaSecurityError, match="non-public"):
        await media._validate_remote_url("https://cdn.provider.example/v.mp4", provider="byok", policy=policy)


async def test_a_connection_key_reaches_only_its_api_host(container, monkeypatch, tmp_path):  # type: ignore[no-untyped-def]
    _resolve_to(monkeypatch, "93.184.216.34")
    seen: list[httpx.Request] = []
    video = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "openrouter.ai":
            return httpx.Response(302, headers={"location": "https://cdn.storage.example/signed.mp4"})
        return httpx.Response(200, content=video, headers={"content-type": "video/mp4"})

    monkeypatch.setattr(
        registry_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    policy = RemoteMediaFetchPolicy(
        allowed_host_patterns=None,
        credential_header=("Authorization", f"Bearer {KEY}"),
        credential_host="openrouter.ai",
    )
    with open(tmp_path / "out.bin", "wb") as sink:
        await container.media._download_provider_media(
            sink, "https://openrouter.ai/api/v1/videos/vid-1/content", provider="byok", policy=policy
        )
    assert seen[0].headers.get("authorization") == f"Bearer {KEY}"
    assert "authorization" not in seen[1].headers, "the redirect target never receives the key"

    seen.clear()
    with open(tmp_path / "out2.bin", "wb") as sink:
        await container.media._download_provider_media(
            sink, "https://cdn.storage.example/other.mp4", provider="byok", policy=policy
        )
    assert "authorization" not in seen[0].headers, "a URL on any other host is fetched without the key"


def test_connection_jobs_appear_in_productions_like_any_other(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = False
    project_id, workspace_id = _workspace_project(container)
    connection_id = _mock_connection(container, workspace_id)
    job, _ = container.visual_runtime.submit(
        _image_request(project_id, "listed"), mode="CANVAS", connection_id=connection_id
    )
    from video_platform_api.main import create_app

    with TestClient(create_app(container)) as client:
        listing = client.get("/v1/generations", params={"project_id": project_id})
        assert listing.status_code == 200, listing.text
        assert job.id in [item["id"] for item in listing.json()["jobs"]]
