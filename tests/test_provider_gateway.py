from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest
from generation_gateway.gateway import UnsafeRetry
from generation_gateway.worker import process_next_job
from media_service import ProviderMediaReconciliationRequired
from platform_contracts import GenerationRequest
from production_domain.models import (
    BillingEvidenceSource,
    BrowserWorker,
    CostRecord,
    GenerationJob,
    JobStatus,
    MediaProviderBinding,
    Project,
    ProviderAccount,
    ProviderBillingEvidence,
    RetryCategory,
    utcnow,
)
from provider_sdk import GenerationProvider, ProviderError, ProviderHealth, ProviderJob, ProviderSubmission
from sqlalchemy import event, func, select


class FakeProvider(GenerationProvider):
    name = "fake"

    def __init__(
        self,
        *,
        fail_uncertain: bool = False,
        fail_poll_once: bool = False,
        cancel_succeeds: bool = False,
        uncertain_category: RetryCategory = RetryCategory.WORKER_DISCONNECT,
    ):
        self.submit_count = 0
        self.submitted_requests: list[dict[str, Any]] = []
        self.upload_count = 0
        self.validate_count = 0
        self.uploaded_assets: list[dict[str, Any]] = []
        self.fail_uncertain = fail_uncertain
        self.fail_poll_once = fail_poll_once
        self.poll_count = 0
        self.cancel_count = 0
        self.cancel_succeeds = cancel_succeeds
        self.uncertain_category = uncertain_category

    async def generate_image(self, request: dict[str, Any], *, account_id: str, worker_id: str):
        return await self.generate_video(request, account_id=account_id, worker_id=worker_id)

    async def generate_video(self, request: dict[str, Any], *, account_id: str, worker_id: str):
        self.submit_count += 1
        self.submitted_requests.append(dict(request))
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
        self.uploaded_assets.append(asset)
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
        self.cancel_count += 1
        return self.cancel_succeeds

    async def get_credits(self, *, account_id: str, worker_id: str):
        return 100

    async def health(self):
        return ProviderHealth(True, "ready")


class BlockingFakeProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release_submission = asyncio.Event()

    async def generate_video(self, request: dict[str, Any], *, account_id: str, worker_id: str):
        self.submit_count += 1
        self.entered.set()
        await self.release_submission.wait()
        return ProviderSubmission("provider-job-1")


class BlockingAssetUploadProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.upload_entered = asyncio.Event()
        self.release_upload = asyncio.Event()

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str):
        self.upload_count += 1
        self.uploaded_assets.append(asset)
        self.upload_entered.set()
        await self.release_upload.wait()
        return "provider-media-fenced"


class FailingAssetUploadProvider(FakeProvider):
    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str):
        self.upload_count += 1
        self.uploaded_assets.append(asset)
        raise RuntimeError("connection failed after upload dispatch")


class UnknownAfterSendProvider(FakeProvider):
    async def generate_video(self, request: dict[str, Any], *, account_id: str, worker_id: str):
        self.submit_count += 1
        raise RuntimeError("connection disappeared after request dispatch")


class BlockingCompletionProvider(FakeProvider):
    def __init__(self, raw: dict[str, Any] | None = None):
        super().__init__()
        self.poll_entered = asyncio.Event()
        self.release_poll = asyncio.Event()
        self.raw = raw or {}

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
    ):
        self.poll_count += 1
        self.poll_entered.set()
        await self.release_poll.wait()
        return ProviderJob(
            provider_job_id,
            "COMPLETED",
            output_url="https://provider.invalid/completed.mp4",
            raw=self.raw,
        )


def add_fake_route(container, provider: FakeProvider, model: str = "fake-model"):
    container.providers.register(provider)
    container.providers.register_model("fake", model, "image")
    container.providers.register_model("fake", model, "video")
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


def test_idempotency_keys_are_isolated_by_project(container, project) -> None:  # type: ignore[no-untyped-def]
    add_fake_route(container, FakeProvider())
    with container.database.session() as session:
        second_project = Project(title="Second tenant project")
        session.add(second_project)
        session.flush()
        second_project_id = second_project.id

    first, first_replayed = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="First tenant",
            idempotency_key="shared-client-request-1",
        )
    )
    second, second_replayed = container.gateway.create(
        GenerationRequest(
            project_id=second_project_id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="Second tenant",
            idempotency_key="shared-client-request-1",
        )
    )

    assert first_replayed is False
    assert second_replayed is False
    assert first.id != second.id


def test_concurrent_same_project_idempotency_returns_one_job(container, project) -> None:  # type: ignore[no-untyped-def]
    add_fake_route(container, FakeProvider())
    request = GenerationRequest(
        project_id=project.id,
        type="video",
        provider="fake",
        model="fake-model",
        prompt="One paid request",
        idempotency_key="concurrent-idempotency-key",
    )
    barrier = threading.Barrier(2)
    seen = threading.local()

    def synchronize_initial_lookup(execute_state) -> None:  # type: ignore[no-untyped-def]
        if (
            execute_state.is_select
            and "generation_idempotency" in str(execute_state.statement)
            and not getattr(seen, "initial_lookup", False)
        ):
            seen.initial_lookup = True
            barrier.wait(timeout=5)

    event.listen(container.database.Session, "do_orm_execute", synchronize_initial_lookup)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: container.gateway.create(request), range(2)))
    finally:
        event.remove(container.database.Session, "do_orm_execute", synchronize_initial_lookup)

    assert results[0][0].id == results[1][0].id
    assert sorted(replayed for _job, replayed in results) == [False, True]
    with container.database.session() as session:
        assert session.scalar(select(func.count(GenerationJob.id))) == 1


@pytest.mark.asyncio
async def test_adapter_payload_reaches_provider_with_resolved_asset_ids(
    container,
    project,
    register_bytes,
):
    provider = FakeProvider()
    add_fake_route(container, provider)
    reference = register_bytes(container, project.id, "CHARACTER_REFERENCE")
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="Canonical prompt",
            start_frame_asset_id=reference.id,
            reference_asset_ids=[reference.id],
            provider_payload={
                "provider": "untrusted-provider-override",
                "model": "untrusted-model-override",
                "prompt": "untrusted prompt override",
                "first_frame_image": reference.id,
                "reference_images": [reference.id],
                "resolution": "720p",
            },
            idempotency_key="adapter-payload-handoff",
        )
    )

    await container.gateway.process(job.id)

    submitted = provider.submitted_requests[0]
    assert submitted["provider"] == "fake"
    assert submitted["model"] == "fake-model"
    assert submitted["prompt"] == "Canonical prompt"
    assert submitted["first_frame_image"] == "provider-media-1"
    assert submitted["reference_images"] == ["provider-media-1"]
    assert submitted["resolution"] == "720p"
    assert "provider_payload" not in submitted


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
async def test_concurrent_processors_cross_paid_submission_boundary_only_once(container, project):
    provider = BlockingFakeProvider()
    add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One action",
            idempotency_key="concurrent-paid-claim",
        )
    )

    owner_task = asyncio.create_task(container.gateway.process(job.id))
    await asyncio.wait_for(provider.entered.wait(), timeout=2)
    non_owner_result = await container.gateway.process(job.id)
    assert non_owner_result.status == JobStatus.RESERVED.value
    assert non_owner_result.submission_state == "SENT_UNCONFIRMED"
    assert provider.submit_count == 1

    provider.release_submission.set()
    owner_result = await asyncio.wait_for(owner_task, timeout=2)
    assert owner_result.status == JobStatus.SUBMITTED.value
    assert owner_result.provider_job_id == "provider-job-1"
    assert provider.submit_count == 1


def test_expired_owner_token_cannot_start_paid_submission_after_reclaim(container, project):
    provider = FakeProvider()
    add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One action",
            idempotency_key="stale-claim-token",
        )
    )
    stale_token = container.gateway._claim_for_submission(job.id)
    assert stale_token
    with container.database.session() as session:
        stored = session.get(type(job), job.id)
        stored.claim_expires_at = utcnow() - timedelta(seconds=1)

    current_token = container.gateway._claim_for_submission(job.id)
    assert current_token and current_token != stale_token
    assert (
        container.gateway._begin_provider_submission(
            job.id,
            stale_token,
            {"prompt": "must not be sent"},
            "fake",
        )
        is False
    )
    assert (
        container.gateway._begin_provider_submission(
            job.id,
            current_token,
            {"prompt": "owned request"},
            "fake",
        )
        is True
    )


@pytest.mark.asyncio
async def test_expired_claim_after_submission_boundary_requires_reconciliation(container, project):
    provider = FakeProvider()
    add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One action",
            idempotency_key="expired-uncertain-claim",
        )
    )
    claim_token = container.gateway._claim_for_submission(job.id)
    assert claim_token
    assert container.gateway._begin_provider_submission(
        job.id,
        claim_token,
        {"prompt": "possibly dispatched"},
        "fake",
    )
    with container.database.session() as session:
        stored = session.get(type(job), job.id)
        stored.claim_expires_at = utcnow() - timedelta(seconds=1)

    quarantined = await container.gateway.process(job.id)
    assert quarantined.status == JobStatus.WORKER_NEEDS_USER_ACTION.value
    assert quarantined.error_code == "SUBMISSION_CLAIM_EXPIRED"
    assert quarantined.safe_to_retry is False
    assert quarantined.submission_state == "SENT_UNCONFIRMED"


@pytest.mark.asyncio
async def test_worker_quarantines_expired_uncertain_claim_then_continues_queue(container, project):
    provider = FakeProvider()
    add_fake_route(container, provider)
    uncertain_job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="Possibly dispatched action",
            idempotency_key="expired-claim-queue-head",
            priority=10,
        )
    )
    next_job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="Next safe action",
            idempotency_key="claim-queue-next",
        )
    )
    claim_token = container.gateway._claim_for_submission(uncertain_job.id)
    assert claim_token
    assert container.gateway._begin_provider_submission(
        uncertain_job.id,
        claim_token,
        {"prompt": "possibly dispatched"},
        "fake",
    )
    with container.database.session() as session:
        session.get(GenerationJob, uncertain_job.id).claim_expires_at = utcnow() - timedelta(seconds=1)

    assert await process_next_job(container) is True
    assert container.gateway.get(uncertain_job.id).status == JobStatus.WORKER_NEEDS_USER_ACTION.value
    assert await process_next_job(container) is True
    assert container.gateway.get(next_job.id).status == JobStatus.SUBMITTED.value
    assert provider.submit_count == 1


@pytest.mark.asyncio
async def test_unknown_exception_after_submission_boundary_never_becomes_retryable(container, project):
    provider = UnknownAfterSendProvider()
    add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One action",
            idempotency_key="unknown-after-send",
        )
    )
    uncertain = await container.gateway.process(job.id)
    assert uncertain.status == JobStatus.WORKER_NEEDS_USER_ACTION.value
    assert uncertain.submission_state == "SENT_UNCONFIRMED"
    assert uncertain.safe_to_retry is False
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
    assert submitted.next_retry_at is not None

    not_due = await container.gateway.process(job.id)
    assert not_due.status == JobStatus.SUBMITTED.value
    assert provider.poll_count == 0
    with container.database.session() as session:
        stored = session.get(type(submitted), submitted.id)
        stored.next_retry_at = utcnow() - timedelta(seconds=1)
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
async def test_worker_defers_provider_polls_and_gives_new_jobs_a_turn(container, project):
    provider = FakeProvider()
    add_fake_route(container, provider)
    oldest_poll, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="Old high-priority action",
            idempotency_key="deferred-poll-oldest",
            priority=10,
        )
    )
    new_job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="New lower-priority action",
            idempotency_key="deferred-poll-new-job",
        )
    )

    assert await process_next_job(container) is True
    first = container.gateway.get(oldest_poll.id)
    assert first.status == JobStatus.SUBMITTED.value
    assert first.next_retry_at is not None
    assert provider.poll_count == 0

    # The oldest high-priority job is not due, so it cannot starve new work.
    assert await process_next_job(container) is True
    second = container.gateway.get(new_job.id)
    assert second.status == JobStatus.SUBMITTED.value
    assert provider.submit_count == 2
    assert provider.poll_count == 0

    # No job is due, so another worker turn performs no provider call.
    assert await process_next_job(container) is False
    assert provider.poll_count == 0

    with container.database.session() as session:
        stored = session.get(GenerationJob, oldest_poll.id)
        stored.next_retry_at = utcnow() - timedelta(seconds=1)

    assert await process_next_job(container) is True
    running = container.gateway.get(oldest_poll.id)
    assert running.status == JobStatus.RUNNING.value
    assert running.next_retry_at is not None
    assert running.claim_token is None
    assert running.claim_expires_at is None
    assert provider.poll_count == 1

    # RUNNING also receives a future poll time, preventing a busy loop.
    assert await process_next_job(container) is False
    assert provider.poll_count == 1


@pytest.mark.asyncio
async def test_concurrent_completion_is_downloaded_finalized_and_released_once(
    container,
    project,
    register_bytes,
    monkeypatch,
):
    provider = BlockingCompletionProvider()
    account_id = add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One action",
            idempotency_key="concurrent-completion",
        )
    )
    submitted = await container.gateway.process(job.id)
    assert submitted.status == JobStatus.SUBMITTED.value
    with container.database.session() as session:
        session.get(GenerationJob, job.id).next_retry_at = utcnow() - timedelta(seconds=1)
    output = register_bytes(container, project.id, "VIDEO", b"completed-video")
    download_count = 0

    async def download_once(*_args, **_kwargs):
        nonlocal download_count
        download_count += 1
        return output

    monkeypatch.setattr(container.media, "download_and_register", download_once)
    owner_task = asyncio.create_task(container.gateway.process(job.id))
    await asyncio.wait_for(provider.poll_entered.wait(), timeout=2)
    non_owner_result = await container.gateway.process(job.id)
    assert non_owner_result.status == JobStatus.RESERVED.value
    assert provider.poll_count == 1
    assert download_count == 0

    provider.release_poll.set()
    completed = await asyncio.wait_for(owner_task, timeout=2)
    assert completed.status == JobStatus.COMPLETED.value
    assert completed.output_asset_id == output.id
    assert provider.poll_count == 1
    assert download_count == 1
    replay = await container.gateway.process(job.id)
    assert replay.status == JobStatus.COMPLETED.value
    assert provider.poll_count == 1
    assert download_count == 1
    assert [event.event_type for event in container.gateway.events(job.id)].count("JOB_COMPLETED") == 1
    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        evidence = session.scalar(
            select(ProviderBillingEvidence).where(ProviderBillingEvidence.generation_job_id == job.id)
        )
        cost = session.scalar(select(CostRecord).where(CostRecord.generation_job_id == job.id))
        stored_job = session.get(GenerationJob, job.id)
        assert account.success_count == 1
        assert account.video_inflight == 0
        assert account.pending_jobs == 0
        assert evidence is not None
        assert evidence.source == BillingEvidenceSource.UNKNOWN.value
        assert evidence.actual_cost_usd is None
        assert stored_job.actual_cost is None
        assert cost is not None and cost.actual_cost is None


@pytest.mark.asyncio
async def test_mock_provider_reported_cost_cannot_create_verified_billing_evidence(
    container,
    project,
    register_bytes,
    monkeypatch,
):
    provider = BlockingCompletionProvider(raw={"usage": {"cost": "0.42", "credits_used": "3"}})
    add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One billed action",
            idempotency_key="verified-provider-billing",
            cost_estimate=0.5,
        )
    )
    submitted = await container.gateway.process(job.id)
    assert submitted.status == JobStatus.SUBMITTED.value
    with container.database.session() as session:
        session.get(GenerationJob, job.id).next_retry_at = utcnow() - timedelta(seconds=1)
    output = register_bytes(container, project.id, "VIDEO", b"billed-video")

    async def download_once(*_args, **_kwargs):
        return output

    monkeypatch.setattr(container.media, "download_and_register", download_once)
    provider.release_poll.set()
    completed = await container.gateway.process(job.id)
    assert completed.status == JobStatus.COMPLETED.value

    with container.database.session() as session:
        evidence = session.scalar(
            select(ProviderBillingEvidence).where(ProviderBillingEvidence.generation_job_id == job.id)
        )
        cost = session.scalar(select(CostRecord).where(CostRecord.generation_job_id == job.id))
        stored_job = session.get(GenerationJob, job.id)
        assert evidence is not None
        assert evidence.source == BillingEvidenceSource.UNKNOWN.value
        assert evidence.actual_cost_usd is None
        assert evidence.provider_credits is None
        assert float(evidence.estimated_cost_usd) == 0.5
        assert evidence.metadata_json["actual_field"] == "usage.cost"
        assert evidence.metadata_json["provider_mode"] == "mock"
        assert evidence.metadata_json["reported_actual_cost_ignored"] is True
        assert evidence.metadata_json["reported_provider_credits_ignored"] is True
        assert stored_job.actual_cost is None
        assert cost is not None and cost.actual_cost is None


@pytest.mark.asyncio
async def test_confirmed_remote_cancel_is_provider_backed_and_releases_capacity_once(
    container,
    project,
):
    provider = FakeProvider(cancel_succeeds=True)
    account_id = add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One action",
            idempotency_key="provider-backed-cancel",
        )
    )
    submitted = await container.gateway.process(job.id)
    assert submitted.status == JobStatus.SUBMITTED.value

    cancelled = await container.gateway.cancel(job.id)
    repeated = await container.gateway.cancel(job.id)

    assert cancelled.status == JobStatus.CANCELLED.value
    assert repeated.status == JobStatus.CANCELLED.value
    assert provider.cancel_count == 1
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, "fake-worker")
        assert stored.reservation_released_at is not None
        assert (account.video_inflight, account.pending_jobs) == (0, 0)
        assert (account.success_count, account.error_count) == (0, 0)
        assert worker.current_jobs == 0


@pytest.mark.asyncio
async def test_unconfirmed_provider_cancel_keeps_remote_tracking_and_capacity(
    container,
    project,
):
    provider = FakeProvider(cancel_succeeds=False)
    account_id = add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One action",
            idempotency_key="provider-cancel-not-confirmed",
        )
    )
    await container.gateway.process(job.id)

    tracked = await container.gateway.cancel(job.id)

    assert tracked.status == JobStatus.SUBMITTED.value
    assert tracked.error_code == "PROVIDER_CANCEL_UNCONFIRMED"
    assert provider.cancel_count == 1
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, "fake-worker")
        assert stored.reservation_released_at is None
        assert (account.video_inflight, account.pending_jobs) == (1, 1)
        assert worker.current_jobs == 1


def test_expired_pre_submit_reservation_is_released_once_during_restart_recovery(
    container,
    project,
):
    provider = FakeProvider()
    account_id = add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One action",
            idempotency_key="crash-after-account-reserve",
        )
    )
    claim_token = container.gateway._claim_for_submission(job.id)
    assert claim_token
    container.scheduler.select_account(
        "fake",
        "video",
        "fake-model",
        project_id=project.id,
        generation_job_id=job.id,
        claim_token=claim_token,
    )
    with container.database.session() as session:
        session.get(GenerationJob, job.id).claim_expires_at = utcnow() - timedelta(seconds=1)

    assert container.gateway.recover_after_restart() == 1
    assert container.gateway.recover_after_restart() == 0
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, "fake-worker")
        assert stored.status == JobStatus.RETRY_WAIT.value
        assert stored.account_id is None and stored.worker_id is None
        assert stored.reservation_released_at is not None
        assert (account.video_inflight, account.pending_jobs) == (0, 0)
        assert (account.success_count, account.error_count) == (0, 0)
        assert worker.current_jobs == 0


def test_job_owned_release_is_concurrently_idempotent(container, project):
    provider = FakeProvider()
    account_id = add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One action",
            idempotency_key="concurrent-job-release",
        )
    )
    claim_token = container.gateway._claim_for_submission(job.id)
    assert claim_token
    container.scheduler.select_account(
        "fake",
        "video",
        "fake-model",
        project_id=project.id,
        generation_job_id=job.id,
        claim_token=claim_token,
    )
    barrier = threading.Barrier(4)

    def release_once(_index):
        barrier.wait(timeout=5)
        return container.scheduler.release_job(job.id, success=False, error="one failure")

    with ThreadPoolExecutor(max_workers=4) as pool:
        released = list(pool.map(release_once, range(4)))

    assert released.count(True) == 1
    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, "fake-worker")
        assert (account.video_inflight, account.pending_jobs, account.error_count) == (0, 0, 1)
        assert worker.current_jobs == 0


@pytest.mark.parametrize("terminal_status", [JobStatus.COMPLETED.value, JobStatus.CANCELLED.value])
def test_reconcile_never_regresses_terminal_generation(container, project, terminal_status):
    provider = FakeProvider()
    add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One action",
            idempotency_key=f"terminal-reconcile-{terminal_status.lower()}",
        )
    )
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        stored.status = terminal_status
        stored.submission_state = "CONFIRMED"
        stored.provider_job_id = "already-final-provider-job"

    unchanged = container.gateway.reconcile(job.id)

    assert unchanged.status == terminal_status
    assert unchanged.submission_state == "CONFIRMED"


@pytest.mark.asyncio
async def test_poll_target_removed_after_submission_fails_and_releases_owned_capacity(
    container,
    project,
):
    provider = FakeProvider()
    account_id = add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One action",
            idempotency_key="poll-target-removed",
        )
    )
    await container.gateway.process(job.id)
    container.providers.mark_model_unavailable("fake", "fake-model", "video")
    with container.database.session() as session:
        session.get(GenerationJob, job.id).next_retry_at = None

    failed = await container.gateway.process(job.id)

    assert failed.status == JobStatus.FAILED.value
    assert failed.error_code == "MODEL_NOT_AVAILABLE"
    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, "fake-worker")
        assert (account.video_inflight, account.pending_jobs, account.error_count) == (0, 0, 1)
        assert worker.current_jobs == 0


@pytest.mark.asyncio
async def test_submitted_job_with_incomplete_routing_requires_manual_repair(
    container,
    project,
):
    provider = FakeProvider()
    account_id = add_fake_route(container, provider)
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="fake",
            model="fake-model",
            prompt="One action",
            idempotency_key="submitted-routing-incomplete",
        )
    )
    await container.gateway.process(job.id)
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        stored.worker_id = None
        stored.next_retry_at = None

    quarantined = await container.gateway.process(job.id)

    assert quarantined.status == JobStatus.WORKER_NEEDS_USER_ACTION.value
    assert quarantined.error_code == "JOB_STATE_INVALID"
    assert quarantined.safe_to_retry is False
    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, "fake-worker")
        assert (account.video_inflight, account.pending_jobs) == (1, 1)
        assert worker.current_jobs == 1


def test_fail_processing_never_regresses_a_terminal_job(container, project):
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="google_flow",
            model="flow-veo-3.1",
            prompt="One action",
            idempotency_key="terminal-fail-processing",
        )
    )
    with container.database.session() as session:
        session.get(GenerationJob, job.id).status = JobStatus.COMPLETED.value
    unchanged = container.gateway.fail_processing(job.id, RuntimeError("late worker exception"))
    assert unchanged.status == JobStatus.COMPLETED.value
    assert unchanged.error_code is None


@pytest.mark.asyncio
async def test_provider_media_binding_reuses_one_upload(container, project, register_bytes):
    provider = FakeProvider()
    account_id = add_fake_route(container, provider)
    asset = register_bytes(container, project.id, "CHARACTER_REFERENCE")
    for _ in range(20):
        media_id, _ = await container.media.resolve_provider_media(
            asset.id,
            provider,
            project_id=project.id,
            account_id=account_id,
            worker_id="fake-worker",
            provider_project_id="provider-project-1",
        )
        assert media_id == "provider-media-1"
    assert provider.upload_count == 1
    assert provider.uploaded_assets[0]["_provider_project_id"] == "provider-project-1"


@pytest.mark.asyncio
async def test_concurrent_provider_media_resolve_uploads_once_and_reuses_winner(
    container, project, register_bytes
):
    provider = BlockingAssetUploadProvider()
    account_id = add_fake_route(container, provider)
    asset = register_bytes(container, project.id, "CHARACTER_REFERENCE")
    start = asyncio.Barrier(2)

    async def resolve() -> tuple[str, bool]:
        await start.wait()
        return await container.media.resolve_provider_media(
            asset.id,
            provider,
            project_id=project.id,
            account_id=account_id,
            worker_id="fake-worker",
        )

    first = asyncio.create_task(resolve())
    second = asyncio.create_task(resolve())
    await asyncio.wait_for(provider.upload_entered.wait(), timeout=2)
    await asyncio.sleep(0.1)
    assert provider.upload_count == 1

    provider.release_upload.set()
    results = await asyncio.gather(first, second)

    assert {media_id for media_id, _ in results} == {"provider-media-fenced"}
    assert sorted(reused for _, reused in results) == [False, True]
    assert provider.upload_count == 1
    with container.database.session() as session:
        binding = session.scalar(
            select(MediaProviderBinding).where(MediaProviderBinding.asset_id == asset.id)
        )
        assert binding is not None
        assert binding.status == "READY"
        assert binding.provider_media_id == "provider-media-fenced"
        assert binding.upload_claim_token is None


@pytest.mark.asyncio
async def test_expired_provider_upload_claim_before_boundary_can_be_taken_over(
    container, project, register_bytes
):
    provider = FakeProvider()
    account_id = add_fake_route(container, provider)
    asset = register_bytes(container, project.id, "CHARACTER_REFERENCE")
    with container.database.session() as session:
        session.add(
            MediaProviderBinding(
                asset_id=asset.id,
                provider=provider.name,
                account_id=account_id,
                provider_media_id=None,
                status="UPLOAD_CLAIMED",
                upload_claim_token="abandoned-pre-boundary-claim",
                upload_claim_expires_at=utcnow() - timedelta(minutes=1),
                upload_started_at=None,
            )
        )

    media_id, reused = await container.media.resolve_provider_media(
        asset.id,
        provider,
        project_id=project.id,
        account_id=account_id,
        worker_id="fake-worker",
    )

    assert (media_id, reused) == ("provider-media-1", False)
    assert provider.upload_count == 1
    with container.database.session() as session:
        binding = session.scalar(
            select(MediaProviderBinding).where(MediaProviderBinding.asset_id == asset.id)
        )
        assert binding is not None
        assert binding.status == "READY"
        assert binding.upload_claim_token is None


@pytest.mark.asyncio
async def test_expired_provider_upload_after_boundary_requires_reconciliation_without_reupload(
    container, project, register_bytes
):
    provider = FakeProvider()
    account_id = add_fake_route(container, provider)
    asset = register_bytes(container, project.id, "CHARACTER_REFERENCE")
    with container.database.session() as session:
        session.add(
            MediaProviderBinding(
                asset_id=asset.id,
                provider=provider.name,
                account_id=account_id,
                provider_media_id=None,
                status="UPLOADING",
                upload_claim_token="expired-post-boundary-claim",
                upload_claim_expires_at=utcnow() - timedelta(minutes=1),
                upload_started_at=utcnow() - timedelta(minutes=2),
            )
        )

    with pytest.raises(ProviderMediaReconciliationRequired, match="paid-call boundary"):
        await container.media.resolve_provider_media(
            asset.id,
            provider,
            project_id=project.id,
            account_id=account_id,
            worker_id="fake-worker",
        )

    assert provider.upload_count == 0
    with container.database.session() as session:
        binding = session.scalar(
            select(MediaProviderBinding).where(MediaProviderBinding.asset_id == asset.id)
        )
        assert binding is not None
        assert binding.status == "NEEDS_RECONCILIATION"


@pytest.mark.asyncio
async def test_provider_upload_exception_is_fenced_and_never_automatically_retried(
    container, project, register_bytes
):
    provider = FailingAssetUploadProvider()
    account_id = add_fake_route(container, provider)
    asset = register_bytes(container, project.id, "CHARACTER_REFERENCE")

    with pytest.raises(RuntimeError, match="after upload dispatch"):
        await container.media.resolve_provider_media(
            asset.id,
            provider,
            project_id=project.id,
            account_id=account_id,
            worker_id="fake-worker",
        )
    with pytest.raises(ProviderMediaReconciliationRequired, match="requires reconciliation"):
        await container.media.resolve_provider_media(
            asset.id,
            provider,
            project_id=project.id,
            account_id=account_id,
            worker_id="fake-worker",
        )

    assert provider.upload_count == 1
    with container.database.session() as session:
        binding = session.scalar(
            select(MediaProviderBinding).where(MediaProviderBinding.asset_id == asset.id)
        )
        assert binding is not None
        assert binding.status == "NEEDS_RECONCILIATION"
        assert binding.upload_started_at is not None


def test_provider_router_has_future_slots(container):
    assert set(container.providers.list()) == {
        "google_flow",
        "grok",
        "kling",
        "omni",
        "openrouter",
        "runapi",
        "runway",
        "seedance",
        "veo_official",
        "wan",
    }
    assert container.providers.configured() == ["google_flow"]


def test_gateway_rejects_cross_project_reference_asset(container, project, register_bytes):
    with container.database.session() as session:
        other_project = Project(title="Other project")
        session.add(other_project)
        session.flush()
        other_project_id = other_project.id
    foreign_asset = register_bytes(container, other_project_id, "IMAGE")
    with pytest.raises(LookupError, match="does not belong"):
        container.gateway.create(
            GenerationRequest(
                project_id=project.id,
                type="video",
                provider="google_flow",
                model="flow-veo-3.1",
                prompt="One action",
                reference_asset_ids=[foreign_asset.id],
                idempotency_key="cross-project-reference",
            )
        )


@pytest.mark.asyncio
async def test_gateway_quarantines_legacy_job_with_unknown_provider(container, project):
    with container.database.session() as session:
        job = GenerationJob(
            project_id=project.id,
            generation_type="video",
            provider="poison-provider",
            model="poison-model",
            request_json={"prompt": "One action."},
            request_hash="0" * 64,
        )
        session.add(job)
        session.flush()
        job_id = job.id

    processed = await container.gateway.process(job_id)

    assert processed.status == JobStatus.FAILED.value
    assert processed.error_code == "PROVIDER_NOT_REGISTERED"
    assert "poison-provider" in processed.error_message
    assert container.gateway.events(job_id)[-1].event_type == "PROVIDER_NOT_REGISTERED"


@pytest.mark.asyncio
async def test_worker_quarantines_one_exception_and_continues_to_next_job(
    container,
    project,
    monkeypatch,
):
    first, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="google_flow",
            model="flow-veo-3.1",
            prompt="First action.",
            idempotency_key="worker-isolation-first",
            priority=10,
        )
    )
    second, _ = container.gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="google_flow",
            model="flow-veo-3.1",
            prompt="Second action.",
            idempotency_key="worker-isolation-second",
        )
    )
    processed_ids: list[str] = []

    async def isolated_process(job_id: str):
        processed_ids.append(job_id)
        if job_id == first.id:
            raise RuntimeError("single poisoned job")
        with container.database.session() as session:
            job = session.get(GenerationJob, job_id)
            job.status = JobStatus.FAILED.value
            session.flush()
            return job

    monkeypatch.setattr(container.gateway, "process", isolated_process)

    assert await process_next_job(container) is True
    assert await process_next_job(container) is True
    assert processed_ids == [first.id, second.id]
    with container.database.session() as session:
        failed = session.get(GenerationJob, first.id)
        assert failed.status == JobStatus.FAILED.value
        assert failed.error_code == "WORKER_PROCESSING_ERROR"
        assert "single poisoned job" in failed.error_message
