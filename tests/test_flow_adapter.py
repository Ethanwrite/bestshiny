from __future__ import annotations

import base64

import pytest
from google_flow_provider import GoogleFlowProvider
from platform_shared import Settings
from production_domain.models import GenerationJob, JobStatus, ProviderAccount
from provider_sdk import LIVE_PROVIDER_CONFIRMATION, ProviderError


def flow_settings(**values):  # type: ignore[no-untyped-def]
    return Settings(
        _env_file=None,
        flow_project_id="flow-project",
        provider_mode="live",
        allow_live_provider_calls=True,
        live_provider_confirmation=LIVE_PROVIDER_CONFIRMATION,
        **values,
    )


class FakeRuntime:
    def __init__(self):
        self.calls = []

    async def dispatch(self, worker_id, message_type, payload, **kwargs):
        self.calls.append((worker_id, message_type, payload, kwargs))
        if message_type == "provider.media_url":
            return {"url": "https://example.test/video.mp4"}
        if "batchCheck" in payload["url"]:
            return {
                "status": 200,
                "data": {
                    "media": [
                        {
                            "mediaMetadata": {
                                "mediaStatus": {"mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"}
                            }
                        }
                    ]
                },
            }
        if "uploadImage" in payload["url"]:
            return {"status": 200, "data": {"mediaId": "uploaded-media"}}
        return {"status": 200, "data": {"media": [{"name": "flow-job-1"}]}}

    def available_workers(self, provider):
        return [type("Worker", (), {"id": "worker"})()]


@pytest.mark.asyncio
async def test_flow_adapter_maps_video_without_leaking_upstream_domain(tmp_path):
    runtime = FakeRuntime()
    provider = GoogleFlowProvider(runtime, flow_settings())
    submission = await provider.generate_video(
        {
            "prompt": "A person takes one step.",
            "duration": 8,
            "aspect_ratio": "9:16",
            "model": "veo",
            "start_frame_provider_media_id": "start-media",
        },
        account_id="account",
        worker_id="worker",
    )
    assert submission.provider_job_id == "flow-job-1"
    payload = runtime.calls[0][2]
    assert payload["provider"] == "google_flow"
    assert payload["url"].startswith("https://aisandbox-pa.googleapis.com/")
    assert payload["body"]["requests"][0]["startImage"]["mediaId"] == "start-media"


@pytest.mark.asyncio
async def test_flow_upload_and_poll(tmp_path):
    runtime = FakeRuntime()
    provider = GoogleFlowProvider(runtime, flow_settings())
    image = tmp_path / "reference.png"
    image.write_bytes(b"image-bytes")
    media_id = await provider.upload_asset(
        {"local_path": str(image), "mime_type": "image/png"},
        account_id="account",
        worker_id="worker",
    )
    assert media_id == "uploaded-media"
    upload_body = runtime.calls[0][2]["body"]
    assert base64.b64decode(upload_body["imageBytes"]) == b"image-bytes"
    result = await provider.get_job(
        "flow-job-1", account_id="account", worker_id="worker", generation_type="video"
    )
    assert result.status == "COMPLETED"
    assert result.output_url == "https://example.test/video.mp4"


@pytest.mark.asyncio
async def test_flow_uses_account_project_id_for_submit_upload_and_poll(container, tmp_path):
    runtime = FakeRuntime()
    with container.database.session() as session:
        account = ProviderAccount(
            provider="google_flow",
            account_identifier="project-specific@example.com",
            credits=10,
            metadata_json={"project_id": "account-project"},
        )
        session.add(account)
        session.flush()
        account_id = account.id
    provider = GoogleFlowProvider(
        runtime,
        container.settings.model_copy(
            update={
                "provider_mode": "live",
                "allow_live_provider_calls": True,
                "live_provider_confirmation": LIVE_PROVIDER_CONFIRMATION,
            }
        ),
        container.database,
    )
    await provider.generate_video(
        {"prompt": "One action", "duration": 8, "model": "veo"},
        account_id=account_id,
        worker_id="worker",
    )
    submit_body = runtime.calls[-1][2]["body"]
    assert submit_body["clientContext"]["projectId"] == "account-project"
    image = tmp_path / "reference.png"
    image.write_bytes(b"image-bytes")
    await provider.upload_asset({"local_path": str(image)}, account_id=account_id, worker_id="worker")
    assert runtime.calls[-1][2]["body"]["clientContext"]["projectId"] == "account-project"
    await provider.get_job(
        "flow-job-1",
        account_id=account_id,
        worker_id="worker",
        generation_type="video",
    )
    poll_body = next(call[2]["body"] for call in runtime.calls if "batchCheck" in call[2]["url"])
    assert poll_body["media"][0]["projectId"] == "account-project"


@pytest.mark.asyncio
async def test_flow_poll_reuses_persisted_submission_project_binding(container, project):
    runtime = FakeRuntime()
    with container.database.session() as session:
        account = ProviderAccount(
            provider="google_flow",
            account_identifier="bound-project@example.com",
            credits=10,
            metadata_json={"project_id": "account-default-project"},
        )
        session.add(account)
        session.flush()
        job = GenerationJob(
            project_id=project.id,
            generation_type="video",
            provider="google_flow",
            model="veo",
            status=JobStatus.SUBMITTED.value,
            request_json={"prompt": "One action"},
            provider_request_json={"_provider_project_id": "persisted-binding-project"},
            request_hash="a" * 64,
            provider_job_id="flow-job-bound-project",
            account_id=account.id,
        )
        session.add(job)
        session.flush()
        account_id = account.id
    provider = GoogleFlowProvider(
        runtime,
        container.settings.model_copy(
            update={
                "provider_mode": "live",
                "allow_live_provider_calls": True,
                "live_provider_confirmation": LIVE_PROVIDER_CONFIRMATION,
            }
        ),
        container.database,
    )

    await provider.get_job(
        "flow-job-bound-project",
        account_id=account_id,
        worker_id="worker",
        generation_type="video",
    )

    poll_body = next(call[2]["body"] for call in runtime.calls if "batchCheck" in call[2]["url"])
    assert poll_body["media"][0]["projectId"] == "persisted-binding-project"


@pytest.mark.asyncio
async def test_flow_image_poll_returns_image_media_url():
    runtime = FakeRuntime()
    provider = GoogleFlowProvider(runtime, flow_settings())
    result = await provider.get_job(
        "flow-image-1", account_id="account", worker_id="worker", generation_type="image"
    )
    assert result.status == "COMPLETED"
    assert result.output_mime_type == "image/png"


@pytest.mark.asyncio
async def test_flow_explicit_bad_request_is_safe_to_retry():
    class BadRequestRuntime(FakeRuntime):
        async def dispatch(self, worker_id, message_type, payload, **kwargs):
            return {"status": 400, "data": {"error": "bad input"}}

    provider = GoogleFlowProvider(BadRequestRuntime(), flow_settings())
    with pytest.raises(ProviderError) as error:
        await provider.generate_video(
            {"prompt": "One action", "duration": 8, "model": "veo"},
            account_id="account",
            worker_id="worker",
        )
    assert error.value.submitted is False


@pytest.mark.parametrize("mode", ["mock", "recorded"])
@pytest.mark.asyncio
async def test_flow_browser_dispatch_never_runs_outside_live_gate(mode):
    runtime = FakeRuntime()
    provider = GoogleFlowProvider(
        runtime,
        Settings(
            _env_file=None,
            flow_project_id="flow-project",
            flow_api_key="configured-key-is-not-call-authority",
            provider_mode=mode,
            allow_live_provider_calls=True,
            live_provider_confirmation=LIVE_PROVIDER_CONFIRMATION,
        ),
    )
    with pytest.raises(ProviderError) as exc:
        await provider.generate_video(
            {"prompt": "One action", "duration": 8, "model": "veo"},
            account_id="account",
            worker_id="worker",
        )
    assert exc.value.code == "LIVE_PROVIDER_CALL_DENIED"
    assert runtime.calls == []
