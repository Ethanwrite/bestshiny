from __future__ import annotations

from typing import Any

import pytest
from generation_gateway.gateway import UnsafeRetry
from platform_contracts import GenerationRequest
from production_domain.models import BrowserWorker, ProviderAccount, RetryCategory
from provider_sdk import GenerationProvider, ProviderError, ProviderHealth, ProviderJob, ProviderSubmission


class FakeProvider(GenerationProvider):
    name = "fake"

    def __init__(
        self,
        *,
        fail_uncertain: bool = False,
        fail_poll_once: bool = False,
        uncertain_category: RetryCategory = RetryCategory.WORKER_DISCONNECT,
    ):
        self.submit_count = 0
        self.upload_count = 0
        self.validate_count = 0
        self.fail_uncertain = fail_uncertain
        self.fail_poll_once = fail_poll_once
        self.poll_count = 0
        self.uncertain_category = uncertain_category

    async def generate_image(self, request: dict[str, Any], *, account_id: str, worker_id: str):
        return await self.generate_video(request, account_id=account_id, worker_id=worker_id)

    async def generate_video(self, request: dict[str, Any], *, account_id: str, worker_id: str):
        self.submit_count += 1
        if self.fail_uncertain:
            raise ProviderError(
                "timeout after send",
                self.uncertain_category,
                code="UNCERTAIN_SUBMISSION",
                submitted=True,
            )
        return ProviderSubmission("provider-job-1")

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str):
        self.upload_count += 1
        return "provider-media-1"

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str):
        self.validate_count += 1
        return True

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
    ):
        self.poll_count += 1
        if self.fail_poll_once and self.poll_count == 1:
            raise ProviderError(
                "temporary poll failure",
                RetryCategory.TRANSIENT_NETWORK,
                code="POLL_NETWORK_ERROR",
                submitted=True,
            )
        return ProviderJob(provider_job_id, "RUNNING")

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str):
        return False

    async def get_credits(self, *, account_id: str, worker_id: str):
        return 100

    async def health(self):
        return ProviderHealth(True, "ready")


def add_fake_route(container, provider: FakeProvider, model: str = "fake-model"):
    container.providers.register(provider)
    with container.database.session() as session:
        account = ProviderAccount(
            provider="fake",
            account_identifier="fake@example.com",
            credits=100,
            supported_models=[model],
            video_capacity=2,
            image_capacity=2,
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id="fake-worker",
            provider="fake",
            account_id=account.id,
            connection_id="fake-connection",
            capabilities=["image", "video", "upload", "poll"],
            max_jobs=2,
        )
        session.add(worker)
        account.worker_id = worker.id
        return account.id


@pytest.mark.asyncio
async def test_paid_timeout_is_not_submitted_twice(container, project):
    provider = FakeProvider(fail_uncertain=True)
    add_fake_route(container, provider)
    request = GenerationRequest(
        project_id=project.id,
        type="video",
        provider="fake",
        model="fake-model",
        prompt="One action",
        idempotency_key="timeout-paid-1",
    )
    job, _ = container.gateway.create(request)
    failed = await container.gateway.process(job.id)
    assert provider.submit_count == 1
    assert failed.status == "WORKER_NEEDS_USER_ACTION"
    assert failed.safe_to_retry is False
    await container.gateway.process(job.id)
    assert provider.submit_count == 1
    with pytest.raises(UnsafeRetry):
        container.gateway.retry(job.id)
    replay, replayed = container.gateway.create(request)
    assert replayed is True and replay.id == job.id
    assert provider.submit_count == 1


@pytest.mark.asyncio
async def test_uncertain_provider_busy_response_is_never_auto_resubmitted(container, project):
    provider = FakeProvider(
        fail_uncertain=True,
        uncertain_category=RetryCategory.PROVIDER_BUSY,
    )
    add_fake_route(container, provider)
    request = GenerationRequest(
        project_id=project.id,
        type="video",
        provider="fake",
        model="fake-model",
        prompt="One action",
        idempotency_key="uncertain-503-paid-1",
    )
    job, _ = container.gateway.create(request)
    uncertain = await container.gateway.process(job.id)
    assert uncertain.status == "WORKER_NEEDS_USER_ACTION"
    await container.gateway.process(job.id)
    assert provider.submit_count == 1


@pytest.mark.asyncio
async def test_poll_retry_never_resubmits_paid_generation(container, project):
    provider = FakeProvider(fail_poll_once=True)
    add_fake_route(container, provider)
    request = GenerationRequest(
        project_id=project.id,
        type="video",
        provider="fake",
        model="fake-model",
        prompt="One action",
        idempotency_key="poll-retry-paid-1",
    )
    job, _ = container.gateway.create(request)
    submitted = await container.gateway.process(job.id)
    assert submitted.provider_job_id == "provider-job-1"
    retry_wait = await container.gateway.process(job.id)
    assert retry_wait.status == "RETRY_WAIT"
    with container.database.session() as session:
        stored = session.get(type(retry_wait), retry_wait.id)
        stored.next_retry_at = None
    running = await container.gateway.process(job.id)
    assert running.status == "RUNNING"
    assert provider.submit_count == 1
    assert provider.poll_count == 2


@pytest.mark.asyncio
async def test_provider_media_binding_reuses_one_upload(container, project, register_bytes):
    provider = FakeProvider()
    account_id = add_fake_route(container, provider)
    asset = register_bytes(container, project.id, "CHARACTER_REFERENCE")
    for _ in range(20):
        media_id, _ = await container.media.resolve_provider_media(
            asset.id,
            provider,
            account_id=account_id,
            worker_id="fake-worker",
        )
        assert media_id == "provider-media-1"
    assert provider.upload_count == 1


def test_provider_router_has_future_slots(container):
    assert set(container.providers.list()) == {
        "google_flow",
        "grok",
        "kling",
        "omni",
        "runway",
        "seedance",
        "veo_official",
    }
