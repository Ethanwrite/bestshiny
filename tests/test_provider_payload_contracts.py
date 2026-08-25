"""Contract regressions for the Adapter payload -> Provider transport boundary.

Each test here covers one previously unguarded handover defect:

1. an automatic retry reusing the Adapter payload of the previous attempt;
2. a fetchable-URL provider receiving an unusable local/provider media ID;
3. Google Flow silently degrading an unmapped model to a text-to-video key;
4. OpenRouter forwarding internal tenancy/accounting/audit fields to the API.
"""

from __future__ import annotations

from typing import Any

import pytest
from evaluation_core import EvaluationDecision, RetryPlan
from google_flow_provider.mapper import (
    parse_video_model_keys,
    resolve_video_model_key,
    video_payload,
)
from openrouter_provider import OpenRouterProvider
from platform_contracts import GenerationRequest
from platform_shared import Settings
from production_domain.models import (
    Episode,
    GenerationJob,
    Scene,
    Shot,
    TimelineState,
)
from provider_sdk import (
    GenerationProvider,
    ProviderError,
    ProviderHealth,
    ProviderJob,
    ProviderReferenceMode,
    ProviderSubmission,
)
from provider_sdk.transport import MockProviderTransport, ProviderHttpRequest, ProviderHttpResponse


@pytest.fixture
def retry_container(tmp_path):  # type: ignore[no-untyped-def]
    """A container with a second configured video target so a model switch is routable."""

    from production_domain.models import Project
    from video_platform_api.container import build_container

    container = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'retry-payload.db'}",
            storage_root=tmp_path / "media",
            public_base_url="http://testserver",
            flow_project_id="flow-project-test",
            auth_required=False,
            deployment_environment="test",
            ark_api_key="offline-placeholder-never-sent",
            seedance_model_id="doubao-seedance-2-5-260628",
        )
    )
    with container.database.session() as session:
        project = Project(title="Retry payload")
        session.add(project)
        session.flush()
        return container, project


CANONICAL_SPEC: dict[str, Any] = {
    "project_id": "project",
    "shot_id": "shot",
    "intent": "Lin turns once.",
    "dominant_action": "Lin turns once.",
    "duration": 8,
    "aspect_ratio": "9:16",
    "allow_camera_gaze": False,
}


class RecordingProvider(GenerationProvider):
    """Minimal transport double that records exactly what the Gateway submits."""

    name = "recording"

    def __init__(self, *, reference_mode: ProviderReferenceMode) -> None:
        self.reference_mode = reference_mode
        self.submitted: list[dict[str, Any]] = []
        self.upload_count = 0

    async def generate_image(self, request: dict[str, Any], *, account_id: str, worker_id: str):  # type: ignore[no-untyped-def]
        return await self.generate_video(request, account_id=account_id, worker_id=worker_id)

    async def generate_video(self, request: dict[str, Any], *, account_id: str, worker_id: str):  # type: ignore[no-untyped-def]
        del account_id, worker_id
        self.submitted.append(dict(request))
        return ProviderSubmission("recording-job-1")

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        del asset, account_id, worker_id
        self.upload_count += 1
        return "recording-media-1"

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool:
        del provider_media_id, account_id, worker_id
        return True

    async def get_job(self, provider_job_id: str, **_kwargs: Any) -> ProviderJob:  # type: ignore[override]
        return ProviderJob(provider_job_id, "RUNNING", progress=0.5)

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        del provider_job_id, account_id, worker_id
        return True

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        del account_id, worker_id
        return None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, "ok")


def _register_recording_provider(container, provider: RecordingProvider) -> str:  # type: ignore[no-untyped-def]
    from production_domain.models import BrowserWorker, ProviderAccount

    container.providers.register(provider)
    container.providers.register_model(provider.name, "recording-model", "video")
    with container.database.session() as session:
        account = ProviderAccount(
            provider=provider.name,
            account_identifier="recording@example.com",
            credits=100,
            supported_models=["recording-model"],
            video_capacity=2,
            image_capacity=2,
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id="recording-worker",
            provider=provider.name,
            account_id=account.id,
            connection_id="recording-connection",
            capabilities=["image", "video", "upload", "poll"],
            max_jobs=2,
        )
        session.add(worker)
        account.worker_id = worker.id
        return account.id


def _shot(container, project) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Pilot", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Room")
        session.add(scene)
        session.flush()
        input_state = TimelineState(
            project_id=project.id, episode_id=episode.id, scene_id=scene.id, state_kind="SHOT_INPUT"
        )
        output_state = TimelineState(
            project_id=project.id, episode_id=episode.id, scene_id=scene.id, state_kind="SHOT_OUTPUT"
        )
        session.add_all([input_state, output_state])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=1,
            prompt="Lin turns once.",
            user_prompt="Lin turns once.",
            input_state_id=input_state.id,
            output_state_id=output_state.id,
        )
        session.add(shot)
        session.flush()
        input_state.shot_id = shot.id
        output_state.shot_id = shot.id
        return shot.id


def _retry(container, project, shot_id: str, plan: RetryPlan, *, key: str):  # type: ignore[no-untyped-def]
    """Drive one automatic retry and return the persisted retry request."""

    stale_payload = {
        "prompt": "stale prompt compiled for the previous attempt",
        "reference_images": ["stale-reference"],
        "first_frame_image": "stale-frame",
        "stale_only_field": "must not survive a changed target",
    }
    request = {
        "project_id": project.id,
        "shot_id": shot_id,
        "candidate_id": None,
        "type": "video",
        "provider": "google_flow",
        "model": "flow-veo-3.1",
        "prompt": "Lin turns once.",
        "negative_prompt": "identity drift",
        "duration": 8,
        "aspect_ratio": "9:16",
        "reference_asset_ids": [],
        "idempotency_key": f"{key}-origin",
        "provider_payload": stale_payload,
        "metadata": {},
    }
    job = container.visual_runtime._execute_retry(
        f"origin-{key}",
        request,
        {},
        CANONICAL_SPEC,
        plan,
    )
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        assert stored is not None
        return dict(stored.request_json), stale_payload


# --- 1. retry payload recompilation -----------------------------------------


def test_model_switch_retry_recompiles_the_adapter_payload(retry_container):  # type: ignore[no-untyped-def]
    container, project = retry_container
    shot_id = _shot(container, project)
    stored, stale = _retry(
        container,
        project,
        shot_id,
        RetryPlan(
            action=EvaluationDecision.SWITCH_MODEL,
            attempt_number=1,
            terminal=False,
            next_provider="seedance",
            next_model="doubao-seedance-2-5-260628",
            reasons=["switch"],
        ),
        key="switch",
    )
    payload = stored["provider_payload"]
    assert stored["model"] == "doubao-seedance-2-5-260628"
    assert payload != stale
    assert "stale_only_field" not in payload
    # The Seedance adapter owns first_frame_image; the previous attempt's value
    # must not survive into a payload compiled for a different model.
    assert payload.get("first_frame_image") != "stale-frame"
    assert payload["prompt"] == stored["prompt"]


def test_prompt_patch_retry_never_submits_the_previous_prompt(container, project):  # type: ignore[no-untyped-def]
    shot_id = _shot(container, project)
    stored, _stale = _retry(
        container,
        project,
        shot_id,
        RetryPlan(
            action=EvaluationDecision.RETRY_REWRITE_PROMPT,
            attempt_number=1,
            terminal=False,
            next_provider="google_flow",
            next_model="flow-veo-3.1",
            prompt_patch="keep both hands visible",
            inject_stronger_references=False,
            reasons=["patch"],
        ),
        key="patch",
    )
    assert "REPAIR CONSTRAINT: keep both hands visible" in stored["prompt"]
    # The payload carries its own prompt copy; it must equal the patched prompt.
    assert stored["provider_payload"]["prompt"] == stored["prompt"]


def test_reference_injection_retry_drops_the_previous_reference_payload(
    container, project, monkeypatch, register_bytes
):  # type: ignore[no-untyped-def]
    shot_id = _shot(container, project)
    # Strengthened references are canonical project assets; supply one so the
    # retry's reference list genuinely differs from the previous attempt's.
    canonical = register_bytes(container, project.id, "CHARACTER_REFERENCE")
    monkeypatch.setattr(
        container.visual_runtime,
        "_canonical_assets",
        lambda _project_id: ([], [canonical.id]),
    )
    stored, _stale = _retry(
        container,
        project,
        shot_id,
        RetryPlan(
            action=EvaluationDecision.RETRY_SAME_MODEL,
            attempt_number=1,
            terminal=False,
            next_provider="google_flow",
            next_model="flow-veo-3.1",
            inject_stronger_references=True,
            reasons=["references"],
        ),
        key="references",
    )
    payload = stored["provider_payload"]
    assert stored["reference_asset_ids"] == [canonical.id]
    assert payload.get("reference_images") == [canonical.id]
    assert "stale_only_field" not in payload


def test_retry_without_a_compilable_spec_drops_the_payload(retry_container):  # type: ignore[no-untyped-def]
    container, project = retry_container
    shot_id = _shot(container, project)
    request = {
        "project_id": project.id,
        "shot_id": shot_id,
        "type": "video",
        "provider": "google_flow",
        "model": "flow-veo-3.1",
        "prompt": "Lin turns once.",
        "duration": 8,
        "reference_asset_ids": [],
        "idempotency_key": "no-spec-origin",
        "provider_payload": {"first_frame_image": "stale-frame"},
        "metadata": {},
    }
    job = container.visual_runtime._execute_retry(
        "origin-no-spec",
        request,
        {},
        {},  # no canonical shot spec: the payload cannot be recompiled
        RetryPlan(
            action=EvaluationDecision.SWITCH_MODEL,
            attempt_number=1,
            terminal=False,
            next_provider="seedance",
            next_model="doubao-seedance-2-5-260628",
            reasons=["switch"],
        ),
    )
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        assert stored is not None
        # Fail safe: canonical request fields only, never the previous payload.
        assert stored.request_json["provider_payload"] == {}


def _free_workspace_project(container) -> str:  # type: ignore[no-untyped-def]
    from production_domain.models import Project, User, Workspace

    with container.database.session() as session:
        user = User(email="payload-contract@example.com", display_name="Payload Contract")
        session.add(user)
        session.flush()
        workspace = Workspace(
            owner_user_id=user.id,
            name="Free Workspace",
            status="ACTIVE",
            plan_tier="FREE",
        )
        session.add(workspace)
        session.flush()
        project = Project(workspace_id=workspace.id, title="Free Project", status="ACTIVE")
        session.add(project)
        session.flush()
        return project.id


def _autopilot_request(project_id: str) -> GenerationRequest:
    return GenerationRequest(
        project_id=project_id,
        shot_id="shot-1",
        candidate_id="candidate-1",
        type="video",
        provider="google_flow",
        model="flow-veo-3.1",
        prompt="Lin turns once.",
        idempotency_key="free-plan-reroute",
        provider_payload={"first_frame_image": "compiled-for-flow"},
    )


def test_free_plan_rerouting_discards_a_payload_compiled_for_another_model(retry_container):  # type: ignore[no-untyped-def]
    """Admission may re-route the plan after the Adapter payload was compiled."""

    container, _project = retry_container
    project_id = _free_workspace_project(container)

    admitted = container.generation_admission.admit_autopilot(_autopilot_request(project_id))

    assert (admitted.request.provider, admitted.request.model) != ("google_flow", "flow-veo-3.1")
    assert admitted.request.provider_payload == {}


def test_admission_keeps_a_payload_when_it_does_not_reroute(retry_container):  # type: ignore[no-untyped-def]
    container, project = retry_container

    admitted = container.generation_admission.admit_autopilot(_autopilot_request(project.id))

    assert admitted.request.model == "flow-veo-3.1"
    assert admitted.request.provider_payload == {"first_frame_image": "compiled-for-flow"}


# --- 2. provider reference mode ---------------------------------------------


@pytest.mark.asyncio
async def test_url_mode_provider_receives_urls_and_is_never_asked_to_upload(
    container, project, register_bytes
):  # type: ignore[no-untyped-def]
    provider = RecordingProvider(reference_mode=ProviderReferenceMode.FETCHABLE_URL)
    _register_recording_provider(container, provider)
    reference = register_bytes(container, project.id, "CHARACTER_REFERENCE")
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="recording",
            model="recording-model",
            prompt="Canonical prompt",
            start_frame_asset_id=reference.id,
            reference_asset_ids=[reference.id],
            provider_payload={
                "first_frame_image": reference.id,
                "reference_images": [reference.id],
            },
            idempotency_key="url-mode-reference",
        )
    )
    await container.gateway.process(job.id)

    submitted = provider.submitted[0]
    assert provider.upload_count == 0
    resolved = str(submitted["start_frame_url"])
    # A short-lived signed reference, not the stored `public_url`. That field
    # points at this service's *authenticated* route, so an external fetcher
    # would get a 403 — and if it did not, every reference byte would stream
    # through the API process.
    assert resolved != reference.public_url
    assert "/v1/storage/" not in resolved
    assert "signature=" in resolved and "expires=" in resolved
    assert submitted["reference_urls"] == [resolved]
    # The adapter payload's local asset IDs are rewritten to the same URLs.
    assert submitted["first_frame_image"] == resolved
    assert submitted["reference_images"] == [resolved]
    assert "start_frame_provider_media_id" not in submitted
    assert "reference_provider_media_ids" not in submitted


@pytest.mark.asyncio
async def test_provider_media_id_mode_still_uploads_and_resolves_media_ids(
    container, project, register_bytes
):  # type: ignore[no-untyped-def]
    provider = RecordingProvider(reference_mode=ProviderReferenceMode.PROVIDER_MEDIA_ID)
    _register_recording_provider(container, provider)
    reference = register_bytes(container, project.id, "CHARACTER_REFERENCE")
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="recording",
            model="recording-model",
            prompt="Canonical prompt",
            start_frame_asset_id=reference.id,
            reference_asset_ids=[reference.id],
            provider_payload={"first_frame_image": reference.id},
            idempotency_key="media-id-mode-reference",
        )
    )
    await container.gateway.process(job.id)

    submitted = provider.submitted[0]
    assert provider.upload_count == 1
    assert submitted["start_frame_provider_media_id"] == "recording-media-1"
    assert submitted["reference_provider_media_ids"] == ["recording-media-1"]
    assert submitted["first_frame_image"] == "recording-media-1"
    assert "start_frame_url" not in submitted


@pytest.mark.asyncio
async def test_url_mode_fails_closed_when_no_fetchable_url_exists(
    container, project, register_bytes
):  # type: ignore[no-untyped-def]
    provider = RecordingProvider(reference_mode=ProviderReferenceMode.FETCHABLE_URL)
    _register_recording_provider(container, provider)
    reference = register_bytes(container, project.id, "CHARACTER_REFERENCE")
    # The storage backend can no longer hand out a direct URL. That must fail
    # closed before the submission boundary rather than degrade into streaming
    # the object through this service, which is the whole point of resolving
    # references against object storage.
    container.storage.reference_signing_key = ""
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="recording",
            model="recording-model",
            prompt="Canonical prompt",
            reference_asset_ids=[reference.id],
            idempotency_key="url-mode-unavailable",
        )
    )
    failed = await container.gateway.process(job.id)

    assert provider.submitted == []
    assert failed.error_code == "PROVIDER_REFERENCE_URL_UNAVAILABLE"
    assert failed.submission_state == "NOT_SENT"
    assert failed.provider_job_id is None


def test_shipped_url_mode_providers_declare_the_contract() -> None:
    from runapi_provider import RunAPIEdgeProvider
    from seedance_provider import SeedanceProvider
    from wan_provider import WanProvider

    for provider in (
        SeedanceProvider(),
        WanProvider(),
        OpenRouterProvider(),
        RunAPIEdgeProvider(),
    ):
        assert provider.reference_mode is ProviderReferenceMode.FETCHABLE_URL


def test_flow_declares_provider_media_id_uploads() -> None:
    from google_flow_provider import GoogleFlowProvider

    assert GoogleFlowProvider.reference_mode is ProviderReferenceMode.PROVIDER_MEDIA_ID


# --- 3. Google Flow runtime model keys --------------------------------------


def test_flow_passes_an_explicit_runtime_model_key_through() -> None:
    assert resolve_video_model_key("abra_i2v_8s", 8) == "abra_i2v_8s"


def test_flow_maps_the_reviewed_legacy_alias() -> None:
    assert resolve_video_model_key("veo", 5) == "abra_t2v_5s"


def test_flow_rejects_an_undeclared_logical_model_instead_of_degrading() -> None:
    with pytest.raises(ProviderError) as error:
        resolve_video_model_key("flow-veo-3.1", 8)
    assert error.value.code == "FLOW_MODEL_KEY_NOT_MAPPED"
    assert error.value.submitted is False


def test_flow_honours_an_operator_reviewed_mapping() -> None:
    mapping = parse_video_model_keys("flow-veo-3.1=abra_veo31_{duration}s")
    assert resolve_video_model_key("flow-veo-3.1", 8, mapping) == "abra_veo31_8s"
    _endpoint, body = video_payload(
        {"prompt": "One action", "duration": 8, "model": "flow-veo-3.1"},
        "flow-project",
        mapping,
    )
    assert body["requests"][0]["videoModelKey"] == "abra_veo31_8s"


def test_flow_rejects_a_mapping_that_is_not_a_runtime_key() -> None:
    mapping = parse_video_model_keys("flow-veo-3.1=veo-3.1-quality")
    with pytest.raises(ProviderError) as error:
        resolve_video_model_key("flow-veo-3.1", 8, mapping)
    assert error.value.code == "FLOW_MODEL_KEY_INVALID"


def test_flow_rejects_a_malformed_mapping_declaration() -> None:
    with pytest.raises(ValueError):
        parse_video_model_keys("flow-veo-3.1")


def test_flow_video_payload_rejects_an_undeclared_model() -> None:
    with pytest.raises(ProviderError) as error:
        video_payload({"prompt": "One action", "duration": 8, "model": "flow-veo-3.1"}, "flow-project")
    assert error.value.code == "FLOW_MODEL_KEY_NOT_MAPPED"


# --- 4. OpenRouter video payload allowlist ----------------------------------


@pytest.mark.asyncio
async def test_openrouter_video_never_forwards_internal_platform_fields() -> None:
    def handler(request: ProviderHttpRequest) -> ProviderHttpResponse:
        assert request.path == "/videos"
        return ProviderHttpResponse(202, {"id": "video-job-1"})

    transport = MockProviderTransport(handler=handler)
    provider = OpenRouterProvider(transport=transport)
    await provider.generate_video(
        {
            "model": "video-model",
            "prompt": "one action",
            "negative_prompt": "identity drift",
            "duration": 8,
            "aspect_ratio": "9:16",
            "resolution": "720p",
            "image_url": "https://media.invalid/start.png",
            "reference_images": ["https://media.invalid/reference.png"],
            # Internal fields that must never leave the platform.
            "project_id": "project-1",
            "shot_id": "shot-1",
            "candidate_id": "candidate-1",
            "idempotency_key": "secret-idempotency",
            "cost_estimate": 1.25,
            "asset_criticality": "CANONICAL",
            "generation_policy": "TEXT_TO_VIDEO",
            "priority": 5,
            "type": "video",
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
        "negative_prompt",
        "duration",
        "aspect_ratio",
        "resolution",
        "image_url",
        "reference_images",
    }


@pytest.mark.asyncio
async def test_openrouter_video_consumes_gateway_resolved_reference_urls() -> None:
    """A request without an Adapter payload still carries its references."""

    def handler(request: ProviderHttpRequest) -> ProviderHttpResponse:
        return ProviderHttpResponse(202, {"id": "video-job-2"})

    transport = MockProviderTransport(handler=handler)
    provider = OpenRouterProvider(transport=transport)
    await provider.generate_video(
        {
            "model": "video-model",
            "prompt": "one action",
            "start_frame_url": "https://media.invalid/start.png",
            "end_frame_url": "https://media.invalid/end.png",
            "reference_urls": ["https://media.invalid/reference.png"],
        },
        account_id="",
        worker_id="",
    )

    body = transport.requests[0].json_body
    assert body is not None
    assert body["image_url"] == "https://media.invalid/start.png"
    assert body["tail_image_url"] == "https://media.invalid/end.png"
    assert body["reference_images"] == ["https://media.invalid/reference.png"]
    assert "start_frame_url" not in body


@pytest.mark.asyncio
async def test_seedance_video_consumes_gateway_resolved_reference_urls() -> None:
    from seedance_provider import SeedanceProvider

    transport = MockProviderTransport(
        {("POST", "/contents/generations/tasks"): ProviderHttpResponse(202, {"id": "seedance-task-2"})}
    )
    provider = SeedanceProvider(transport=transport, seedance_model_id="doubao-seedance-2-5-260628")
    await provider.generate_video(
        {
            "prompt": "one action",
            "start_frame_url": "https://media.invalid/start.png",
            "reference_urls": ["https://media.invalid/reference.png"],
        },
        account_id="",
        worker_id="",
    )

    body = transport.requests[0].json_body
    assert body is not None
    image_urls = [item["image_url"]["url"] for item in body["content"] if item["type"] == "image_url"]
    assert image_urls == ["https://media.invalid/start.png", "https://media.invalid/reference.png"]


# --- 3b. Wan 2.7 runtime model keys -----------------------------------------
#
# Wan 2.7 ships one DashScope model per mode, so the mode — not the request
# body alone — decides which model ID is posted. These pin the reviewed IDs so
# a rename cannot happen silently, and pin the fail-closed behaviour for a
# family with no reviewed ID at all.


def test_wan_resolves_one_dashscope_model_per_mode() -> None:
    from wan_provider.adapter import resolve_video_model

    assert resolve_video_model("wan-2.7", "t2v", {}) == "wan2.7-t2v-2026-06-12"
    assert resolve_video_model("wan-2.7", "i2v", {}) == "wan2.7-i2v-2026-04-25"
    assert resolve_video_model("wan-2.7", "r2v", {}) == "wan2.7-r2v-2026-06-12"


def test_wan_rejects_the_invitation_only_beta_family_instead_of_guessing() -> None:
    """Wan 3.0 is Beta-gated, so it has no reviewed runtime ID to default to."""

    from wan_provider.adapter import resolve_video_model

    with pytest.raises(ProviderError) as error:
        resolve_video_model("wan-3.0", "t2v", {})
    assert error.value.code == "INVALID_REQUEST"
    assert "WAN_VIDEO_MODEL_KEYS" in str(error.value)


def test_wan_operator_declaration_outranks_the_reviewed_default() -> None:
    from wan_provider.adapter import parse_video_model_keys, resolve_video_model

    mapping = parse_video_model_keys("wan-2.7:i2v=wan2.7-i2v-preview,wan-3.0=wan3.0-beta")
    assert resolve_video_model("wan-2.7", "i2v", mapping) == "wan2.7-i2v-preview"
    assert resolve_video_model("wan-3.0", "t2v", mapping) == "wan3.0-beta"
    # An unmapped mode still falls through to its reviewed default.
    assert resolve_video_model("wan-2.7", "t2v", mapping) == "wan2.7-t2v-2026-06-12"


@pytest.mark.asyncio
async def test_wan_selects_the_mode_model_from_the_request_shape() -> None:
    """The mode is inferred from the payload, and each mode posts its own model."""

    from wan_provider import WanProvider

    def _provider() -> tuple[WanProvider, MockProviderTransport]:
        transport = MockProviderTransport(
            {
                ("POST", "/services/aigc/video-generation/video-synthesis"): ProviderHttpResponse(
                    202, {"output": {"task_id": "wan-mode-task"}}
                )
            }
        )
        return WanProvider(video_transport=transport), transport

    cases = (
        ({"prompt": "one action"}, "wan2.7-t2v-2026-06-12"),
        (
            {"prompt": "one action", "start_frame_url": "https://media.invalid/start.png"},
            "wan2.7-i2v-2026-04-25",
        ),
        (
            {"prompt": "one action", "reference_video_url": "https://media.invalid/ref.mp4"},
            "wan2.7-r2v-2026-06-12",
        ),
    )
    for request, expected_model in cases:
        provider, transport = _provider()
        await provider.generate_video({**request, "model": "wan-2.7"}, account_id="", worker_id="")
        body = transport.requests[0].json_body
        assert body is not None
        assert body["model"] == expected_model


# --- 3c. The Wan 2.7 media plane --------------------------------------------
#
# The defect these started from: `_video_payload` read `reference_video` and a
# first frame and nothing else, so `reference_images` / `reference_urls` — the
# list the Gateway resolves and pays to resolve — never reached DashScope. A
# shot generated on four character plates rendered as if it had none.
#
# They also pin the wire contract, which is narrower than the internal one: an
# entry carries `type` and `url` only. The role decides the model, the limits
# and the ordering, and is then dropped.


def _wan(**kwargs):  # type: ignore[no-untyped-def]
    from wan_provider import WanProvider

    transport = MockProviderTransport(
        {
            ("POST", "/services/aigc/video-generation/video-synthesis"): ProviderHttpResponse(
                202, {"output": {"task_id": "wan-media-task"}}
            )
        }
    )
    return WanProvider(video_transport=transport, **kwargs), transport


@pytest.mark.asyncio
async def test_wan_carries_reference_images_instead_of_dropping_them() -> None:
    provider, transport = _wan()
    await provider.generate_video(
        {
            "model": "wan-2.7",
            "prompt": "one action",
            "reference_urls": [
                "https://media.invalid/face.png",
                "https://media.invalid/wardrobe.png",
            ],
        },
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert body["model"] == "wan2.7-r2v-2026-06-12"
    assert body["input"]["media"] == [
        {"type": "image", "url": "https://media.invalid/face.png"},
        {"type": "image", "url": "https://media.invalid/wardrobe.png"},
    ]


@pytest.mark.asyncio
async def test_wan_media_entries_carry_only_the_official_fields() -> None:
    """The role is canonical internal state and never reaches the provider."""

    provider, transport = _wan()
    await provider.generate_video(
        {
            "model": "wan-2.7",
            "prompt": "one action",
            "start_frame_url": "https://media.invalid/start.png",
            "reference_urls": ["https://media.invalid/face.png"],
        },
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert all(set(item) == {"type", "url"} for item in body["input"]["media"])


@pytest.mark.asyncio
async def test_wan_r2v_takes_a_first_frame_alongside_its_references() -> None:
    """The published R2V matrix: first_frame + reference_image/reference_video.

    Position is the only role signal the provider gets, so the frame leads.
    """

    provider, transport = _wan()
    await provider.generate_video(
        {
            "model": "wan-2.7",
            "prompt": "one action",
            "start_frame_url": "https://media.invalid/start.png",
            "reference_video": "https://media.invalid/style.mp4",
            "reference_urls": ["https://media.invalid/face.png"],
        },
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert body["model"] == "wan2.7-r2v-2026-06-12"
    assert body["input"]["media"] == [
        {"type": "image", "url": "https://media.invalid/start.png"},
        {"type": "video", "url": "https://media.invalid/style.mp4"},
        {"type": "image", "url": "https://media.invalid/face.png"},
    ]


@pytest.mark.asyncio
async def test_wan_start_frame_alone_is_i2v() -> None:
    provider, transport = _wan()
    await provider.generate_video(
        {
            "model": "wan-2.7",
            "prompt": "one action",
            "start_frame_url": "https://media.invalid/start.png",
        },
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert body["model"] == "wan2.7-i2v-2026-04-25"


@pytest.mark.asyncio
async def test_wan_enforces_the_published_reference_bounds_before_billing() -> None:
    provider, transport = _wan()
    with pytest.raises(ProviderError) as error:
        await provider.generate_video(
            {
                "model": "wan-2.7",
                "prompt": "one action",
                "reference_urls": [f"https://media.invalid/ref{index}.png" for index in range(6)],
            },
            account_id="",
            worker_id="",
        )
    assert error.value.code == "INVALID_REQUEST"
    assert "at most 5 reference assets" in str(error.value)
    assert not transport.requests

    provider, transport = _wan()
    await provider.generate_video(
        {
            "model": "wan-2.7",
            "prompt": "one action",
            "start_frame_url": "https://media.invalid/start.png",
            "reference_urls": [f"https://media.invalid/ref{index}.png" for index in range(4)],
            "reference_video": "https://media.invalid/style.mp4",
        },
        account_id="",
        worker_id="",
    )
    # One first frame plus five reference assets is exactly the bound.
    assert transport.requests[0].json_body["model"] == "wan2.7-r2v-2026-06-12"
    assert len(transport.requests[0].json_body["input"]["media"]) == 6


@pytest.mark.asyncio
async def test_wan_continuation_from_a_clip_is_an_i2v_shot() -> None:
    """A clip the shot grows out of is I2V's job, like a first frame."""

    provider, transport = _wan()
    await provider.generate_video(
        {
            "model": "wan-2.7",
            "prompt": "one action",
            "first_clip": "https://media.invalid/previous.mp4",
        },
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert body["model"] == "wan2.7-i2v-2026-04-25"
    assert body["input"]["media"] == [
        {"type": "video", "url": "https://media.invalid/previous.mp4"}
    ]


@pytest.mark.asyncio
async def test_wan_continuation_and_a_reference_video_are_not_the_same_request() -> None:
    """Continuing from footage and referencing it select different models."""

    provider, transport = _wan()
    await provider.generate_video(
        {"model": "wan-2.7", "prompt": "one action", "first_clip": "https://media.invalid/a.mp4"},
        account_id="",
        worker_id="",
    )
    assert transport.requests[0].json_body["model"] == "wan2.7-i2v-2026-04-25"

    provider, transport = _wan()
    await provider.generate_video(
        {
            "model": "wan-2.7",
            "prompt": "one action",
            "reference_video": "https://media.invalid/a.mp4",
        },
        account_id="",
        worker_id="",
    )
    assert transport.requests[0].json_body["model"] == "wan2.7-r2v-2026-06-12"

    # And asking for both is refused rather than silently resolved to one: I2V
    # carries no reference video, R2V carries no clip to continue from.
    provider, transport = _wan()
    with pytest.raises(ProviderError) as error:
        await provider.generate_video(
            {
                "model": "wan-2.7",
                "prompt": "one action",
                "first_clip": "https://media.invalid/a.mp4",
                "reference_video": "https://media.invalid/b.mp4",
            },
            account_id="",
            worker_id="",
        )
    assert "reference_video" in str(error.value)
    assert not transport.requests


@pytest.mark.asyncio
async def test_wan_refuses_a_voice_reference_it_does_not_declare() -> None:
    """`supports_reference_voice` is false, so no mode may carry one.

    The serializer can express it — a voice reference is `type: audio` and
    still only type + url — but the capability declaration is the gate, and a
    request carrying one fails before it is billed rather than arriving with
    the voice quietly missing.
    """

    provider, transport = _wan()
    with pytest.raises(ProviderError) as error:
        await provider.generate_video(
            {
                "model": "wan-2.7",
                "prompt": "one action",
                "reference_voice": "https://media.invalid/voice.wav",
            },
            account_id="",
            worker_id="",
        )
    assert error.value.code == "INVALID_REQUEST"
    assert "reference_voice" in str(error.value)
    assert not transport.requests


@pytest.mark.asyncio
async def test_wan_refuses_a_reference_the_provider_cannot_fetch() -> None:
    """An unresolved asset ID would spend a generation on an unreadable input."""

    provider, transport = _wan()
    with pytest.raises(ProviderError) as error:
        await provider.generate_video(
            {"model": "wan-2.7", "prompt": "one action", "reference_images": ["asset-abc123"]},
            account_id="",
            worker_id="",
        )
    assert error.value.code == "INVALID_REQUEST"
    assert "must be a URL" in str(error.value)
    assert not transport.requests


@pytest.mark.asyncio
async def test_wan_sends_a_resolution_tier_and_never_a_pixel_size() -> None:
    from wan_provider.adapter import _resolution

    assert _resolution("720p") == "720P"
    assert _resolution("1080P") == "1080P"
    for rejected in ("1280*720", "1280x720", "cinema"):
        with pytest.raises(ProviderError):
            _resolution(rejected)

    provider, transport = _wan()
    with pytest.raises(ProviderError) as error:
        await provider.generate_video(
            {"model": "wan-2.7", "prompt": "one action", "size": "1280*720"},
            account_id="",
            worker_id="",
        )
    assert "not a pixel size" in str(error.value)
    assert not transport.requests


@pytest.mark.asyncio
async def test_wan_sends_ratio_only_where_nothing_else_fixes_the_aspect() -> None:
    """t2v takes a ratio; i2v never does; r2v only without a first frame."""

    provider, transport = _wan()
    await provider.generate_video(
        {"model": "wan-2.7", "prompt": "one action", "resolution": "1080p", "aspect_ratio": "9:16"},
        account_id="",
        worker_id="",
    )
    assert transport.requests[0].json_body["parameters"] == {
        "resolution": "1080P",
        "ratio": "9:16",
        "watermark": False,
    }

    provider, transport = _wan()
    await provider.generate_video(
        {
            "model": "wan-2.7",
            "prompt": "one action",
            "resolution": "1080p",
            "aspect_ratio": "9:16",
            "start_frame_url": "https://media.invalid/start.png",
        },
        account_id="",
        worker_id="",
    )
    # I2V: the frame fixes the aspect, so no ratio travels beside it.
    assert transport.requests[0].json_body["parameters"] == {
        "resolution": "1080P",
        "watermark": False,
    }

    provider, transport = _wan()
    await provider.generate_video(
        {
            "model": "wan-2.7",
            "prompt": "one action",
            "resolution": "1080p",
            "aspect_ratio": "9:16",
            "reference_urls": ["https://media.invalid/face.png"],
        },
        account_id="",
        worker_id="",
    )
    # R2V with references only: nothing fixes the aspect, so the ratio applies.
    assert transport.requests[0].json_body["parameters"]["ratio"] == "9:16"

    provider, transport = _wan()
    await provider.generate_video(
        {
            "model": "wan-2.7",
            "prompt": "one action",
            "resolution": "1080p",
            "aspect_ratio": "9:16",
            "reference_urls": ["https://media.invalid/face.png"],
            "start_frame_url": "https://media.invalid/start.png",
        },
        account_id="",
        worker_id="",
    )
    # R2V carrying a first frame: the frame wins, as it does on I2V.
    assert "ratio" not in transport.requests[0].json_body["parameters"]


@pytest.mark.asyncio
async def test_wan_honours_an_explicit_mode_over_the_inferred_one() -> None:
    provider, transport = _wan()
    await provider.generate_video(
        {
            "model": "wan-2.7",
            "mode": "t2v",
            "prompt": "one action",
            "reference_urls": ["https://media.invalid/face.png"],
        },
        account_id="",
        worker_id="",
    )
    body = transport.requests[0].json_body
    assert body is not None
    assert body["model"] == "wan2.7-t2v-2026-06-12"


@pytest.mark.asyncio
async def test_wan_video_consumes_gateway_resolved_reference_urls() -> None:
    from wan_provider import WanProvider

    transport = MockProviderTransport(
        {
            ("POST", "/services/aigc/video-generation/video-synthesis"): ProviderHttpResponse(
                202, {"output": {"task_id": "wan-task-2"}}
            )
        }
    )
    provider = WanProvider(
        video_transport=transport,
        t2v_model_id="wan-t2v",
        i2v_model_id="wan-i2v",
    )
    await provider.generate_video(
        {
            "prompt": "one action",
            "start_frame_url": "https://media.invalid/start.png",
            "end_frame_url": "https://media.invalid/end.png",
        },
        account_id="",
        worker_id="",
    )

    body = transport.requests[0].json_body
    assert body is not None
    assert body["model"] == "wan-i2v"
    assert body["input"]["media"] == [
        {"type": "image", "url": "https://media.invalid/start.png"},
        {"type": "image", "url": "https://media.invalid/end.png"},
    ]


@pytest.mark.asyncio
async def test_runapi_video_never_forwards_internal_platform_fields() -> None:
    from decimal import Decimal

    from provider_sdk import (
        AssetCriticality,
        EdgeTask,
        EdgeTaskRole,
        InMemoryProviderBudgetRepository,
    )
    from runapi_provider import RunAPIEdgeProvider

    transport = MockProviderTransport({("POST", "/v1/videos"): ProviderHttpResponse(202, {"id": "edge-1"})})
    provider = RunAPIEdgeProvider(
        transport=transport,
        model_id="edge-video-model",
        allow_edge_calls=True,
        budget_repository=InMemoryProviderBudgetRepository({"runapi": 10}),
    )
    await provider.generate_video(
        {
            "model": "edge-video-model",
            "prompt": "one action",
            "duration": 8,
            "start_frame_url": "https://media.invalid/start.png",
            "asset_criticality": AssetCriticality.TEMPORARY.value,
            "project_id": "project-1",
            "idempotency_key": "secret-idempotency",
            "style_control": {"embedding": [0.1]},
            "metadata": {"canonical_shot_spec": {"intent": "internal"}},
            "_generation_job_id": "job-1",
            "_edge_task": EdgeTask(
                task_id="edge-payload-contract",
                role=EdgeTaskRole.TEMPORARY_PLACEHOLDER_ASSET,
                asset_criticality=AssetCriticality.TEMPORARY,
                estimated_cost_usd=Decimal("0.10"),
            ),
        },
        account_id="",
        worker_id="",
    )

    body = transport.requests[0].json_body
    assert body is not None
    assert set(body) == {"model", "prompt", "duration", "image_url"}
    assert body["image_url"] == "https://media.invalid/start.png"


# --- 3b. Google Flow image model names --------------------------------------


def test_flow_accepts_the_reviewed_image_model() -> None:
    from google_flow_provider.mapper import image_payload, resolve_image_model_name

    assert resolve_image_model_name("NARWHAL") == "NARWHAL"
    _endpoint, body = image_payload(
        {"prompt": "One image", "model": "NARWHAL", "aspect_ratio": "9:16"},
        "flow-project",
    )
    assert body["requests"][0]["imageModelName"] == "NARWHAL"


def test_flow_rejects_an_unreviewed_image_model_instead_of_defaulting() -> None:
    from google_flow_provider.mapper import image_payload, resolve_image_model_name

    with pytest.raises(ProviderError) as error:
        resolve_image_model_name("imagen-4")
    assert error.value.code == "FLOW_IMAGE_MODEL_NOT_REVIEWED"

    with pytest.raises(ProviderError) as missing:
        image_payload({"prompt": "One image"}, "flow-project")
    assert missing.value.code == "FLOW_IMAGE_MODEL_MISSING"


# --- 5. OpenRouter video job lifecycle --------------------------------------


@pytest.mark.asyncio
async def test_openrouter_never_calls_an_undocumented_video_cancel_endpoint() -> None:
    """`DELETE /videos/{id}` is not an OpenRouter endpoint.

    Reporting a cancellation that never reached the provider would release local
    capacity and stop polling while the remote job kept generating and billing.
    """

    transport = MockProviderTransport()
    provider = OpenRouterProvider(transport=transport)
    assert await provider.cancel_job("video-job-1", account_id="", worker_id="") is False
    assert transport.requests == []


@pytest.mark.asyncio
async def test_completed_openrouter_video_reads_its_published_output_url() -> None:
    """A completed job publishes its artefact in `unsigned_urls`.

    Reading only the older aliases left a finished, already-billed generation
    with no output URL, which the Gateway can only report as OUTPUT_URL_MISSING.
    """

    transport = MockProviderTransport(
        {
            ("GET", "/videos/video-job-1"): ProviderHttpResponse(
                200,
                {
                    "id": "video-job-1",
                    "status": "completed",
                    "unsigned_urls": ["https://openrouter.ai/api/v1/videos/video-job-1/content?index=0"],
                    "usage": {"cost": 0.25},
                },
            )
        }
    )
    provider = OpenRouterProvider(transport=transport)
    job = await provider.get_job(
        "video-job-1", account_id="", worker_id="", generation_type="video"
    )
    assert job.status == "COMPLETED"
    assert job.output_url == "https://openrouter.ai/api/v1/videos/video-job-1/content?index=0"


@pytest.mark.asyncio
async def test_in_progress_openrouter_video_is_reported_as_running() -> None:
    transport = MockProviderTransport(
        {
            ("GET", "/videos/video-job-2"): ProviderHttpResponse(
                200, {"id": "video-job-2", "status": "in_progress", "progress": 40}
            )
        }
    )
    provider = OpenRouterProvider(transport=transport)
    job = await provider.get_job(
        "video-job-2", account_id="", worker_id="", generation_type="video"
    )
    assert job.status == "RUNNING"
    assert job.progress == pytest.approx(0.4)
