from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from entitlement_core import InsufficientWorkspaceCredits, WorkspaceCreditConflict
from fastapi.testclient import TestClient
from generation_gateway.gateway import UnsafeRetry
from media_service import RemoteMediaSecurityError
from platform_contracts import GenerationRequest
from production_domain.models import (
    BrowserWorker,
    CostRecord,
    DecisionRecord,
    GenerationEvent,
    GenerationJob,
    JobStatus,
    MediaProviderBinding,
    Project,
    ProviderAccount,
    RetryCategory,
    User,
    Workspace,
    WorkspaceCreditEntry,
    WorkspaceCreditEvent,
)
from provider_sdk import (
    GenerationProvider,
    ProviderError,
    ProviderHealth,
    ProviderJob,
    ProviderSubmission,
)
from sqlalchemy import event, func, select
from video_platform_api.main import create_app


class _CancelledPollProvider(GenerationProvider):
    name = "cancelled_poll"

    async def generate_image(
        self,
        request: dict,  # type: ignore[type-arg]
        *,
        account_id: str,
        worker_id: str,
    ) -> ProviderSubmission:
        return ProviderSubmission("cancelled-provider-job")

    async def generate_video(
        self,
        request: dict,  # type: ignore[type-arg]
        *,
        account_id: str,
        worker_id: str,
    ) -> ProviderSubmission:
        return ProviderSubmission("cancelled-provider-job")

    async def upload_asset(
        self,
        asset: dict,  # type: ignore[type-arg]
        *,
        account_id: str,
        worker_id: str,
    ) -> str:
        return "unused-provider-media"

    async def validate_asset(
        self,
        provider_media_id: str,
        *,
        account_id: str,
        worker_id: str,
    ) -> bool:
        return True

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
    ) -> ProviderJob:
        return ProviderJob(
            provider_job_id,
            "CANCELLED",
            error="provider reports that the remote job was cancelled",
        )

    async def cancel_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
    ) -> bool:
        return True

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        return 100

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, "ready")


class _DelayedSubmissionProvider(_CancelledPollProvider):
    name = "delayed_submission"

    def __init__(self) -> None:
        self.submission_started = asyncio.Event()
        self.release_submission = asyncio.Event()

    async def generate_image(
        self,
        request: dict,  # type: ignore[type-arg]
        *,
        account_id: str,
        worker_id: str,
    ) -> ProviderSubmission:
        return await self.generate_video(request, account_id=account_id, worker_id=worker_id)

    async def generate_video(
        self,
        request: dict,  # type: ignore[type-arg]
        *,
        account_id: str,
        worker_id: str,
    ) -> ProviderSubmission:
        self.submission_started.set()
        await self.release_submission.wait()
        return ProviderSubmission("late-provider-job")


class _FalseSubmittedErrorProvider(_CancelledPollProvider):
    name = "false_submitted_error"

    def __init__(self) -> None:
        self.submit_count = 0

    async def generate_image(
        self,
        request: dict,  # type: ignore[type-arg]
        *,
        account_id: str,
        worker_id: str,
    ) -> ProviderSubmission:
        return await self.generate_video(request, account_id=account_id, worker_id=worker_id)

    async def generate_video(
        self,
        request: dict,  # type: ignore[type-arg]
        *,
        account_id: str,
        worker_id: str,
    ) -> ProviderSubmission:
        self.submit_count += 1
        raise ProviderError(
            "adapter incorrectly classified an error after dispatch as not submitted",
            RetryCategory.TRANSIENT_NETWORK,
            code="FALSE_NOT_SUBMITTED_AFTER_BOUNDARY",
            submitted=False,
        )


class _AssetUploadBoundaryProvider(_CancelledPollProvider):
    name = "asset_upload_boundary"

    def __init__(self, *, fail_after_dispatch: bool = False) -> None:
        self.fail_after_dispatch = fail_after_dispatch
        self.upload_count = 0
        self.submit_count = 0

    async def upload_asset(
        self,
        asset: dict,  # type: ignore[type-arg]
        *,
        account_id: str,
        worker_id: str,
    ) -> str:
        del asset, account_id, worker_id
        self.upload_count += 1
        if self.fail_after_dispatch:
            raise RuntimeError("connection failed after provider upload dispatch")
        return "asset-upload-boundary-media"

    async def generate_video(
        self,
        request: dict,  # type: ignore[type-arg]
        *,
        account_id: str,
        worker_id: str,
    ) -> ProviderSubmission:
        del request, account_id, worker_id
        self.submit_count += 1
        return ProviderSubmission("asset-upload-boundary-job")


class _RunningPollProvider(_CancelledPollProvider):
    name = "running_poll"

    def __init__(self) -> None:
        self.poll_returned = threading.Event()

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
    ) -> ProviderJob:
        self.poll_returned.set()
        return ProviderJob(provider_job_id, "RUNNING", progress=0.5)


class _CompletedPollProvider(_CancelledPollProvider):
    name = "completed_poll"

    def __init__(self) -> None:
        self.poll_count = 0

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
    ) -> ProviderJob:
        del account_id, worker_id, generation_type
        self.poll_count += 1
        return ProviderJob(
            provider_job_id,
            "COMPLETED",
            output_url="https://unsafe-provider.invalid/output.mp4",
        )


def _free_projects(container, *titles: str) -> tuple[str, list[str]]:  # type: ignore[no-untyped-def]
    return _plan_projects(container, "FREE", *titles)


def _plan_projects(container, plan_tier: str, *titles: str) -> tuple[str, list[str]]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        user = User(
            email=f"credit-lifecycle-{plan_tier.lower()}@example.com",
            display_name="Credit Lifecycle",
        )
        session.add(user)
        session.flush()
        workspace = Workspace(
            owner_user_id=user.id,
            name=f"Credit Lifecycle {plan_tier}",
            status="ACTIVE",
            plan_tier=plan_tier,
        )
        session.add(workspace)
        session.flush()
        projects = [Project(workspace_id=workspace.id, title=title, status="ACTIVE") for title in titles]
        session.add_all(projects)
        session.flush()
        return workspace.id, [project.id for project in projects]


def _reserve(
    container,  # type: ignore[no-untyped-def]
    project_id: str,
    *,
    idempotency_key: str,
    credits: int = 10,
    prompt: str = "A single visible action",
    provider: str = "google_flow",
    model: str = "flow-veo-3.1",
) -> GenerationJob:
    job, replayed = container.gateway.create(
        GenerationRequest(
            project_id=project_id,
            type="video",
            provider=provider,
            model=model,
            prompt=prompt,
            idempotency_key=idempotency_key,
        ),
        estimated_credits=credits,
        pricing_version="credit-lifecycle-test-v1",
    )
    assert replayed is False
    return job


def _require_reconciliation(container, job_id: str, reason: str = "TEST_UNCERTAIN") -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None
        job.status = JobStatus.WORKER_NEEDS_USER_ACTION.value
        job.submission_state = "SENT_UNCONFIRMED"
        job.safe_to_retry = False
        transition = container.workspace_credits.require_reconciliation(
            session,
            job,
            reason=reason,
        )
        assert transition.status == "RECONCILIATION_REQUIRED"


def _register_cancelled_poll_provider(container) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    model = "cancelled-model"
    container.providers.register(_CancelledPollProvider())
    container.providers.register_model(_CancelledPollProvider.name, model, "video")
    with container.database.session() as session:
        account = ProviderAccount(
            provider=_CancelledPollProvider.name,
            account_identifier="cancelled-poll@example.com",
            credits=100,
            supported_models=[model],
            video_capacity=1,
            image_capacity=0,
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id="cancelled-poll-worker",
            provider=_CancelledPollProvider.name,
            account_id=account.id,
            connection_id="cancelled-poll-connection",
            capabilities=["video", "poll"],
            max_jobs=1,
        )
        session.add(worker)
        account.worker_id = worker.id
        session.flush()
        return account.id, worker.id


def _register_submission_provider(
    container,  # type: ignore[no-untyped-def]
    provider: GenerationProvider,
    *,
    model: str,
) -> tuple[str, str]:
    container.providers.register(provider)
    container.providers.register_model(provider.name, model, "video")
    with container.database.session() as session:
        account = ProviderAccount(
            provider=provider.name,
            account_identifier=f"{provider.name}@example.com",
            credits=100,
            supported_models=[model],
            video_capacity=1,
            image_capacity=0,
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id=f"{provider.name}-worker",
            provider=provider.name,
            account_id=account.id,
            connection_id=f"{provider.name}-connection",
            capabilities=["video", "upload"],
            max_jobs=1,
        )
        session.add(worker)
        account.worker_id = worker.id
        session.flush()
        return account.id, worker.id


def test_pre_submit_cancel_refunds_once_and_is_idempotent(container):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Pre-submit cancellation")
    job = _reserve(container, project_id, idempotency_key="cancel-before-send")

    first = asyncio.run(container.gateway.cancel(job.id))
    second = asyncio.run(container.gateway.cancel(job.id))

    assert first.status == JobStatus.CANCELLED.value
    assert second.status == JobStatus.CANCELLED.value
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        refund_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type == "REFUNDED",
            )
        )
        cost = session.scalar(select(CostRecord).where(CostRecord.generation_job_id == job.id))

        assert workspace is not None and workspace.credit_balance == 50
        assert entry is not None
        assert entry.status == "REFUNDED"
        assert entry.settled_credits == 0
        assert entry.refunded_credits == 10
        assert refund_events == 1
        assert cost is not None and cost.credits == 0


def test_late_retry_cannot_revive_cancelled_refunded_job(container):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Retry versus cancellation")
    job = _reserve(container, project_id, idempotency_key="retry-cancel-race")
    retry_update_reached = threading.Event()
    release_retry_update = threading.Event()
    retry_thread_id: list[int] = []

    def pause_retry_generation_update(
        _connection,  # type: ignore[no-untyped-def]
        _cursor,  # type: ignore[no-untyped-def]
        statement: str,
        _parameters,  # type: ignore[no-untyped-def]
        _context,  # type: ignore[no-untyped-def]
        _executemany: bool,
    ) -> None:
        if (
            retry_thread_id
            and threading.get_ident() == retry_thread_id[0]
            and statement.lstrip().upper().startswith("UPDATE GENERATION_JOBS")
            and not retry_update_reached.is_set()
        ):
            retry_update_reached.set()
            if not release_retry_update.wait(timeout=5):
                raise TimeoutError("test did not release the delayed retry update")

    def retry_after_snapshot():
        retry_thread_id.append(threading.get_ident())
        return container.gateway.retry(job.id)

    event.listen(
        container.database.engine,
        "before_cursor_execute",
        pause_retry_generation_update,
    )
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(retry_after_snapshot)
    try:
        assert retry_update_reached.wait(timeout=2), "retry never reached its delayed write"
        cancelled = asyncio.run(container.gateway.cancel(job.id))
        assert cancelled.status == JobStatus.CANCELLED.value
        release_retry_update.set()
        try:
            future.result(timeout=2)
        except UnsafeRetry:
            pass
    finally:
        release_retry_update.set()
        executor.shutdown(wait=True)
        event.remove(
            container.database.engine,
            "before_cursor_execute",
            pause_retry_generation_update,
        )

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        stored = session.get(GenerationJob, job.id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        refund_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type == "REFUNDED",
            )
        )

        assert workspace is not None and workspace.credit_balance == 50
        assert stored is not None
        assert stored.status == JobStatus.CANCELLED.value
        assert stored.status != JobStatus.RETRY_WAIT.value
        assert stored.safe_to_retry is False
        assert entry is not None and entry.status == "REFUNDED"
        assert entry.refunded_credits == 10
        assert refund_events == 1


def test_late_error_scheduler_cannot_revive_cancelled_refunded_job(
    container,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Error scheduling versus cancellation")
    job = _reserve(container, project_id, idempotency_key="schedule-error-cancel-race")
    error_update_reached = threading.Event()
    release_error_update = threading.Event()
    error_thread_id: list[int] = []

    def pause_error_generation_update(
        _connection,  # type: ignore[no-untyped-def]
        _cursor,  # type: ignore[no-untyped-def]
        statement: str,
        _parameters,  # type: ignore[no-untyped-def]
        _context,  # type: ignore[no-untyped-def]
        _executemany: bool,
    ) -> None:
        if (
            error_thread_id
            and threading.get_ident() == error_thread_id[0]
            and statement.lstrip().upper().startswith("UPDATE GENERATION_JOBS")
            and not error_update_reached.is_set()
        ):
            error_update_reached.set()
            if not release_error_update.wait(timeout=5):
                raise TimeoutError("test did not release the delayed error update")

    def schedule_error_after_snapshot():
        error_thread_id.append(threading.get_ident())
        return container.gateway._schedule_error(
            job.id,
            RetryCategory.TRANSIENT_NETWORK,
            "TEST_TRANSIENT_FAILURE",
            "transient failure arrived after cancellation",
            submitted=False,
        )

    event.listen(
        container.database.engine,
        "before_cursor_execute",
        pause_error_generation_update,
    )
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(schedule_error_after_snapshot)
    try:
        assert error_update_reached.wait(timeout=2), "error scheduler never reached its delayed write"
        cancelled = asyncio.run(container.gateway.cancel(job.id))
        assert cancelled.status == JobStatus.CANCELLED.value
        release_error_update.set()
        future.result(timeout=2)
    finally:
        release_error_update.set()
        executor.shutdown(wait=True)
        event.remove(
            container.database.engine,
            "before_cursor_execute",
            pause_error_generation_update,
        )

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        stored = session.get(GenerationJob, job.id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        refund_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type == "REFUNDED",
            )
        )

        assert workspace is not None and workspace.credit_balance == 50
        assert stored is not None
        assert stored.status == JobStatus.CANCELLED.value
        assert stored.status != JobStatus.RETRY_WAIT.value
        assert stored.safe_to_retry is False
        assert entry is not None and entry.status == "REFUNDED"
        assert entry.refunded_credits == 10
        assert refund_events == 1


def test_late_restart_recovery_cannot_revive_cancelled_refunded_job(
    container,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Restart recovery versus cancellation")
    job = _reserve(container, project_id, idempotency_key="restart-recovery-cancel-race")
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        assert stored is not None
        stored.status = JobStatus.QUEUED.value

    recovery_update_reached = threading.Event()
    release_recovery_update = threading.Event()
    recovery_thread_id: list[int] = []

    def pause_recovery_generation_update(
        _connection,  # type: ignore[no-untyped-def]
        _cursor,  # type: ignore[no-untyped-def]
        statement: str,
        _parameters,  # type: ignore[no-untyped-def]
        _context,  # type: ignore[no-untyped-def]
        _executemany: bool,
    ) -> None:
        if (
            recovery_thread_id
            and threading.get_ident() == recovery_thread_id[0]
            and statement.lstrip().upper().startswith("UPDATE GENERATION_JOBS")
            and not recovery_update_reached.is_set()
        ):
            recovery_update_reached.set()
            if not release_recovery_update.wait(timeout=5):
                raise TimeoutError("test did not release the delayed recovery update")

    def recover_after_snapshot() -> int:
        recovery_thread_id.append(threading.get_ident())
        return container.gateway.recover_after_restart()

    event.listen(
        container.database.engine,
        "before_cursor_execute",
        pause_recovery_generation_update,
    )
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(recover_after_snapshot)
    try:
        assert recovery_update_reached.wait(timeout=2), "recovery never reached its delayed write"
        cancelled = asyncio.run(container.gateway.cancel(job.id))
        assert cancelled.status == JobStatus.CANCELLED.value
        release_recovery_update.set()
        assert future.result(timeout=2) == 0
    finally:
        release_recovery_update.set()
        executor.shutdown(wait=True)
        event.remove(
            container.database.engine,
            "before_cursor_execute",
            pause_recovery_generation_update,
        )

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        stored = session.get(GenerationJob, job.id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        refund_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type == "REFUNDED",
            )
        )

        assert workspace is not None and workspace.credit_balance == 50
        assert stored is not None
        assert stored.status == JobStatus.CANCELLED.value
        assert stored.safe_to_retry is False
        assert entry is not None and entry.status == "REFUNDED"
        assert entry.refunded_credits == 10
        assert refund_events == 1


def test_paid_submission_boundary_winning_cancel_race_never_refunds(
    container,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Cancel versus paid boundary")
    provider = _CancelledPollProvider()
    _register_submission_provider(container, provider, model="cancel-boundary-model")
    job = _reserve(
        container,
        project_id,
        idempotency_key="cancel-boundary-race",
        provider=provider.name,
        model="cancel-boundary-model",
    )
    claim_token = container.gateway._claim_for_submission(job.id)

    assert claim_token is not None
    assert container.gateway._begin_provider_submission(
        job.id,
        claim_token,
        {"prompt": "the paid boundary has already won"},
        provider.name,
    )

    result = asyncio.run(container.gateway.cancel(job.id))

    assert result.submission_state == "SENT_UNCONFIRMED"
    assert result.status != JobStatus.CANCELLED.value
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        refund_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type.in_(["REFUNDED", "RECONCILED_REFUNDED"]),
            )
        )
        boundary_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type == "PROVIDER_SUBMISSION_STARTED",
            )
        )

        assert workspace is not None and workspace.credit_balance == 40
        assert entry is not None
        assert entry.status == "RECONCILIATION_REQUIRED"
        assert entry.refunded_credits == 0
        assert refund_events == 0
        assert boundary_events == 1


def test_adapter_false_not_submitted_after_boundary_still_requires_reconciliation(
    container,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "False adapter submission flag")
    provider = _FalseSubmittedErrorProvider()
    _register_submission_provider(container, provider, model="false-submitted-error-model")
    job = _reserve(
        container,
        project_id,
        idempotency_key="false-not-submitted-after-boundary",
        provider=provider.name,
        model="false-submitted-error-model",
    )

    uncertain = asyncio.run(container.gateway.process(job.id))
    repeated = asyncio.run(container.gateway.process(job.id))

    assert uncertain.status == JobStatus.WORKER_NEEDS_USER_ACTION.value
    assert uncertain.submission_state == "SENT_UNCONFIRMED"
    assert uncertain.safe_to_retry is False
    assert uncertain.next_retry_at is None
    assert uncertain.provider_job_id is None
    assert repeated.status == JobStatus.WORKER_NEEDS_USER_ACTION.value
    assert provider.submit_count == 1
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        refund_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type.in_(["REFUNDED", "RECONCILED_REFUNDED"]),
            )
        )
        boundary_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type == "PROVIDER_SUBMISSION_STARTED",
            )
        )
        reconciliation_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type == "RECONCILIATION_REQUIRED",
            )
        )
        cost = session.scalar(select(CostRecord).where(CostRecord.generation_job_id == job.id))

        assert workspace is not None and workspace.credit_balance == 40
        assert entry is not None
        assert entry.status == "RECONCILIATION_REQUIRED"
        assert entry.refunded_credits == 0
        assert entry.reconciliation_reason == "FALSE_NOT_SUBMITTED_AFTER_BOUNDARY"
        assert refund_events == 0
        assert boundary_events == 1
        assert reconciliation_events == 1
        assert cost is not None and cost.credits == 10


def test_paid_media_security_rejection_is_terminal_reconciled_and_releases_capacity(
    container,
    monkeypatch,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Paid unsafe provider output")
    provider = _CompletedPollProvider()
    account_id, worker_id = _register_submission_provider(
        container,
        provider,
        model="completed-poll-model",
    )
    job = _reserve(
        container,
        project_id,
        idempotency_key="paid-media-security-rejection",
        provider=provider.name,
        model="completed-poll-model",
    )

    submitted = asyncio.run(container.gateway.process(job.id))
    assert submitted.status == JobStatus.SUBMITTED.value
    assert submitted.provider_job_id == "cancelled-provider-job"
    with container.database.session() as session:
        session.get(GenerationJob, job.id).next_retry_at = None

    async def reject_provider_output(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RemoteMediaSecurityError("provider media host resolved to a non-public address")

    monkeypatch.setattr(container.gateway, "_stage_provider_outputs", reject_provider_output)
    failed = asyncio.run(container.gateway.process(job.id))
    replay = asyncio.run(container.gateway.process(job.id))

    assert failed.status == JobStatus.FAILED.value
    assert failed.error_code == "PROVIDER_MEDIA_SECURITY_ERROR"
    assert failed.safe_to_retry is False
    assert failed.next_retry_at is None
    assert replay.status == JobStatus.FAILED.value
    assert provider.poll_count == 1

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, worker_id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        refund_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type.in_(["REFUNDED", "RECONCILED_REFUNDED"]),
            )
        )

        assert workspace is not None and workspace.credit_balance == 40
        assert account is not None and account.video_inflight == 0 and account.pending_jobs == 0
        assert worker is not None and worker.current_jobs == 0
        assert entry is not None and entry.status == "RECONCILIATION_REQUIRED"
        assert entry.refunded_credits == 0
        assert entry.reconciliation_reason == "PROVIDER_MEDIA_SECURITY_ERROR"
        assert refund_events == 0


def test_asset_upload_boundary_allows_same_claim_to_continue_to_generation(
    container,
    register_bytes,
) -> None:  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Asset upload then generation")
    provider = _AssetUploadBoundaryProvider()
    _register_submission_provider(container, provider, model="asset-boundary-model")
    asset = register_bytes(container, project_id, "CHARACTER_REFERENCE")
    job, replayed = container.gateway.create(
        GenerationRequest(
            project_id=project_id,
            type="video",
            provider=provider.name,
            model="asset-boundary-model",
            prompt="One action with one reference",
            reference_asset_ids=[asset.id],
            idempotency_key="asset-upload-then-generation",
        ),
        estimated_credits=10,
        pricing_version="credit-lifecycle-test-v1",
    )
    assert replayed is False

    submitted = asyncio.run(container.gateway.process(job.id))

    assert submitted.status == JobStatus.SUBMITTED.value
    assert submitted.submission_state == "CONFIRMED"
    assert submitted.provider_job_id == "asset-upload-boundary-job"
    assert provider.upload_count == 1
    assert provider.submit_count == 1
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        boundary_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type == "PROVIDER_SUBMISSION_STARTED",
            )
        )
        binding = session.scalar(
            select(MediaProviderBinding).where(MediaProviderBinding.asset_id == asset.id)
        )
        assert workspace is not None and workspace.credit_balance == 40
        assert entry is not None and entry.status == "RESERVED"
        assert boundary_events == 1
        assert binding is not None and binding.status == "READY"


def test_asset_upload_exception_holds_wallet_and_requires_reconciliation_without_retry(
    container,
    register_bytes,
) -> None:  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Asset upload uncertain")
    provider = _AssetUploadBoundaryProvider(fail_after_dispatch=True)
    _register_submission_provider(container, provider, model="asset-boundary-failure-model")
    asset = register_bytes(container, project_id, "CHARACTER_REFERENCE")
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project_id,
            type="video",
            provider=provider.name,
            model="asset-boundary-failure-model",
            prompt="One action with one uncertain reference upload",
            reference_asset_ids=[asset.id],
            idempotency_key="asset-upload-uncertain",
        ),
        estimated_credits=10,
        pricing_version="credit-lifecycle-test-v1",
    )

    uncertain = asyncio.run(container.gateway.process(job.id))
    repeated = asyncio.run(container.gateway.process(job.id))

    assert uncertain.status == JobStatus.WORKER_NEEDS_USER_ACTION.value
    assert uncertain.submission_state == "SENT_UNCONFIRMED"
    assert uncertain.safe_to_retry is False
    assert uncertain.provider_job_id is None
    assert repeated.status == JobStatus.WORKER_NEEDS_USER_ACTION.value
    assert provider.upload_count == 1
    assert provider.submit_count == 0
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        binding = session.scalar(
            select(MediaProviderBinding).where(MediaProviderBinding.asset_id == asset.id)
        )
        refund_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type.in_(["REFUNDED", "RECONCILED_REFUNDED"]),
            )
        )
        assert workspace is not None and workspace.credit_balance == 40
        assert entry is not None and entry.status == "RECONCILIATION_REQUIRED"
        assert entry.refunded_credits == 0
        assert binding is not None and binding.status == "NEEDS_RECONCILIATION"
        assert refund_events == 0


def test_claim_loss_after_successful_asset_upload_is_quarantined_without_generation_retry(
    container,
    register_bytes,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Asset upload claim loss")
    provider = _AssetUploadBoundaryProvider()
    _register_submission_provider(container, provider, model="asset-boundary-claim-loss-model")
    asset = register_bytes(container, project_id, "CHARACTER_REFERENCE")
    job, _ = container.gateway.create(
        GenerationRequest(
            project_id=project_id,
            type="video",
            provider=provider.name,
            model="asset-boundary-claim-loss-model",
            prompt="One action whose claim expires after upload",
            reference_asset_ids=[asset.id],
            idempotency_key="asset-upload-claim-loss",
        ),
        estimated_credits=10,
        pricing_version="credit-lifecycle-test-v1",
    )
    monkeypatch.setattr(container.gateway, "_begin_provider_submission", lambda *args: False)

    quarantined = asyncio.run(container.gateway.process(job.id))

    assert quarantined.status == JobStatus.WORKER_NEEDS_USER_ACTION.value
    assert quarantined.error_code == "ASSET_UPLOAD_BOUNDARY_CLAIM_LOST"
    assert quarantined.submission_state == "SENT_UNCONFIRMED"
    assert quarantined.safe_to_retry is False
    assert provider.upload_count == 1
    assert provider.submit_count == 0
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        assert workspace is not None and workspace.credit_balance == 40
        assert entry is not None and entry.status == "RECONCILIATION_REQUIRED"
        assert entry.refunded_credits == 0


def test_missing_local_reference_fails_before_asset_boundary_and_refunds(
    container,
) -> None:  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Missing local reference")
    provider = _AssetUploadBoundaryProvider()
    _register_submission_provider(container, provider, model="asset-boundary-local-failure-model")
    with pytest.raises(LookupError, match="media asset not found"):
        container.gateway.create(
            GenerationRequest(
                project_id=project_id,
                type="video",
                provider=provider.name,
                model="asset-boundary-local-failure-model",
                prompt="One action with a missing reference",
                reference_asset_ids=["missing-local-asset"],
                idempotency_key="asset-upload-local-validation-failure",
            ),
            estimated_credits=10,
            pricing_version="credit-lifecycle-test-v1",
        )

    assert provider.upload_count == 0
    assert provider.submit_count == 0
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None and workspace.credit_balance == 50
        assert session.scalar(select(func.count(GenerationJob.id))) == 0
        assert session.scalar(select(func.count(WorkspaceCreditEntry.id))) == 0
        assert session.scalar(select(func.count(GenerationEvent.id))) == 0


def test_provider_poll_cancelled_terminalizes_job_but_keeps_credit_for_reconciliation(
    container,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Provider-side cancellation")
    account_id, worker_id = _register_cancelled_poll_provider(container)
    job = _reserve(
        container,
        project_id,
        idempotency_key="provider-poll-cancelled",
        provider=_CancelledPollProvider.name,
        model="cancelled-model",
    )
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        assert stored is not None
        stored.provider_job_id = "cancelled-provider-job"
        stored.submission_state = "CONFIRMED"
        stored.status = JobStatus.SUBMITTED.value
        stored.safe_to_retry = False
        stored.account_id = account_id
        stored.worker_id = worker_id
        container.workspace_credits.record_submission_confirmed(
            session,
            stored,
            attempt=1,
            provider_job_id=stored.provider_job_id,
        )

    result = asyncio.run(container.gateway.process(job.id))

    assert result.status == JobStatus.CANCELLED.value
    assert result.error_code == "PROVIDER_JOB_CANCELLED"
    assert result.safe_to_retry is False
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        refund_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type.in_(["REFUNDED", "RECONCILED_REFUNDED"]),
            )
        )
        cost = session.scalar(select(CostRecord).where(CostRecord.generation_job_id == job.id))

        assert workspace is not None and workspace.credit_balance == 40
        assert entry is not None
        assert entry.status == "RECONCILIATION_REQUIRED"
        assert entry.refunded_credits == 0
        assert entry.reconciliation_reason == "PROVIDER_REPORTED_CANCELLED_WITH_BILLING_UNKNOWN"
        assert refund_events == 0
        assert cost is not None and cost.credits == 10


def test_late_running_poll_cannot_revive_cancelled_generation(
    container,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Running poll versus cancellation")
    provider = _RunningPollProvider()
    account_id, worker_id = _register_submission_provider(
        container,
        provider,
        model="running-model",
    )
    job = _reserve(
        container,
        project_id,
        idempotency_key="running-poll-cancel-race",
        provider=provider.name,
        model="running-model",
    )
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        assert stored is not None
        stored.provider_job_id = "running-provider-job"
        stored.submission_state = "CONFIRMED"
        stored.status = JobStatus.SUBMITTED.value
        stored.safe_to_retry = False
        stored.account_id = account_id
        stored.worker_id = worker_id
        container.workspace_credits.record_submission_confirmed(
            session,
            stored,
            attempt=1,
            provider_job_id=stored.provider_job_id,
        )

    running_update_reached = threading.Event()
    release_running_update = threading.Event()
    poll_thread_id: list[int] = []
    post_poll_updates = 0

    def pause_final_running_update(
        _connection,  # type: ignore[no-untyped-def]
        _cursor,  # type: ignore[no-untyped-def]
        statement: str,
        _parameters,  # type: ignore[no-untyped-def]
        _context,  # type: ignore[no-untyped-def]
        _executemany: bool,
    ) -> None:
        nonlocal post_poll_updates
        if (
            poll_thread_id
            and threading.get_ident() == poll_thread_id[0]
            and provider.poll_returned.is_set()
            and statement.lstrip().upper().startswith("UPDATE GENERATION_JOBS")
        ):
            post_poll_updates += 1
            if post_poll_updates == 2:
                running_update_reached.set()
                if not release_running_update.wait(timeout=5):
                    raise TimeoutError("test did not release the delayed poll update")

    def process_poll():
        poll_thread_id.append(threading.get_ident())
        return asyncio.run(container.gateway.process(job.id))

    event.listen(container.database.engine, "before_cursor_execute", pause_final_running_update)
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(process_poll)
    try:
        assert running_update_reached.wait(timeout=2), "poll never reached its delayed RUNNING write"
        with container.database.session() as session:
            stored = session.get(GenerationJob, job.id)
            assert stored is not None
            stored.claim_expires_at = stored.updated_at.replace(year=2000)
        cancelled = asyncio.run(container.gateway.cancel(job.id))
        assert cancelled.status == JobStatus.CANCELLED.value
        release_running_update.set()
        result = future.result(timeout=2)
        assert result.status == JobStatus.CANCELLED.value
    finally:
        release_running_update.set()
        executor.shutdown(wait=True)
        event.remove(container.database.engine, "before_cursor_execute", pause_final_running_update)

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        stored = session.get(GenerationJob, job.id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        refund_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type.in_(["REFUNDED", "RECONCILED_REFUNDED"]),
            )
        )

        assert workspace is not None and workspace.credit_balance == 40
        assert stored is not None
        assert stored.status == JobStatus.CANCELLED.value
        assert stored.safe_to_retry is False
        assert entry is not None and entry.status == "RECONCILIATION_REQUIRED"
        assert refund_events == 0


def test_uncertain_submission_is_held_for_reconciliation_and_never_auto_refunded(
    container,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Uncertain paid boundary")
    job = _reserve(container, project_id, idempotency_key="uncertain-paid-boundary")
    _require_reconciliation(container, job.id)

    cancelled = asyncio.run(container.gateway.cancel(job.id))
    repaired = container.gateway.reconcile_credit_lifecycle()

    assert cancelled.status == JobStatus.WORKER_NEEDS_USER_ACTION.value
    assert repaired == 0
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        refund_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type.in_(["REFUNDED", "RECONCILED_REFUNDED"]),
            )
        )

        assert workspace is not None and workspace.credit_balance == 40
        assert entry is not None
        assert entry.status == "RECONCILIATION_REQUIRED"
        assert entry.refunded_credits == 0
        assert entry.reconciliation_reason == "TEST_UNCERTAIN"
        assert refund_events == 0


def test_internal_credit_reconcile_is_authenticated_strict_and_idempotent(
    container,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Internal reconciliation")
    job = _reserve(container, project_id, idempotency_key="manual-reconcile")
    _require_reconciliation(container, job.id, "PROVIDER_RESULT_UNKNOWN")
    path = f"/internal/generations/{job.id}/credit-reconcile"
    body = {
        "action": "CONFIRM_PROVIDER_ACCEPTED",
        "reason": "provider invoice confirms generation charge",
        "explicit_confirmation": True,
        "evidence_reference": "invoice:test-001",
    }
    internal_headers = {
        "Authorization": f"Bearer {container.settings.platform_api_key}",
        "Idempotency-Key": "credit-decision-001",
    }

    with TestClient(create_app(container)) as client:
        denied = client.post(path, json=body, headers={"Idempotency-Key": "unauthorized"})
        extra = client.post(
            path,
            json={**body, "credits": 1},
            headers=internal_headers,
        )
        settled = client.post(path, json=body, headers=internal_headers)
        replay = client.post(path, json=body, headers=internal_headers)
        conflict = client.post(
            path,
            json={
                **body,
                "action": "CONFIRM_PROVIDER_NOT_CREATED",
                "reason": "same key must not select a different outcome",
            },
            headers=internal_headers,
        )

    assert denied.status_code == 401
    assert extra.status_code == 422
    assert settled.status_code == 200, settled.text
    assert settled.json()["credit_status"] == "SETTLED"
    assert settled.json()["replayed"] is False
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert conflict.status_code == 409, conflict.text
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        manual_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type == "RECONCILED_SETTLED",
            )
        )
        decisions = session.scalar(
            select(func.count(DecisionRecord.id)).where(
                DecisionRecord.project_id == project_id,
                DecisionRecord.decision_type == "WORKSPACE_CREDIT_RECONCILIATION",
            )
        )

        assert workspace is not None and workspace.credit_balance == 40
        assert entry is not None and entry.status == "SETTLED"
        assert entry.settled_credits == 10
        assert manual_events == 1
        assert decisions == 1


@pytest.mark.asyncio
async def test_late_provider_response_cannot_revive_manually_refunded_terminal_job(
    container,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Late provider response")
    provider = _DelayedSubmissionProvider()
    _register_submission_provider(container, provider, model="delayed-model")
    job = _reserve(
        container,
        project_id,
        idempotency_key="late-response-after-refund",
        provider=provider.name,
        model="delayed-model",
    )

    owner_task = asyncio.create_task(container.gateway.process(job.id))
    await asyncio.wait_for(provider.submission_started.wait(), timeout=2)
    try:
        with container.database.session() as session:
            stored = session.get(GenerationJob, job.id)
            assert stored is not None
            assert stored.submission_state == "SENT_UNCONFIRMED"
            assert stored.claim_token is not None
            stored.status = JobStatus.WORKER_NEEDS_USER_ACTION.value
            stored.safe_to_retry = False
            stored.claim_token = None
            stored.claim_expires_at = None
            transition = container.workspace_credits.require_reconciliation(
                session,
                stored,
                reason="OPERATOR_CONFIRMED_NOT_BILLABLE",
            )
            assert transition.status == "RECONCILIATION_REQUIRED"

        refunded = container.gateway.reconcile_credits(
            job.id,
            action="REFUND_RESERVED",
            idempotency_key="late-response-refund-decision",
            reason="operator confirmed the delayed request was not billable",
            evidence_reference="support-case:late-response-001",
        )
        assert refunded.status == "REFUNDED"
    finally:
        provider.release_submission.set()

    result = await asyncio.wait_for(owner_task, timeout=2)

    assert result.status == JobStatus.FAILED.value
    assert result.status != JobStatus.SUBMITTED.value
    assert result.provider_job_id is None
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        stored = session.get(GenerationJob, job.id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        late_response_events = session.scalar(
            select(func.count(GenerationEvent.id)).where(
                GenerationEvent.generation_job_id == job.id,
                GenerationEvent.event_type == "LATE_PROVIDER_RESPONSE_AFTER_TERMINAL",
            )
        )

        assert workspace is not None and workspace.credit_balance == 50
        assert stored is not None
        assert stored.status == JobStatus.FAILED.value
        assert stored.provider_job_id is None
        assert entry is not None and entry.status == "REFUNDED"
        assert entry.refunded_credits == 10
        assert late_response_events == 1


def test_internal_refund_rejects_active_claim_and_preserves_balance(
    container,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Active claim refund guard")
    job = _reserve(container, project_id, idempotency_key="active-claim-refund-guard")
    claim_token = container.gateway._claim_for_submission(job.id)
    assert claim_token is not None
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        assert stored is not None
        assert stored.status == JobStatus.RESERVED.value
        assert stored.claim_token == claim_token
        transition = container.workspace_credits.require_reconciliation(
            session,
            stored,
            reason="TEST_ACTIVE_CLAIM_RECONCILIATION",
        )
        assert transition.status == "RECONCILIATION_REQUIRED"

    path = f"/internal/generations/{job.id}/credit-reconcile"
    headers = {
        "Authorization": f"Bearer {container.settings.platform_api_key}",
        "Idempotency-Key": "active-claim-refund-decision",
    }
    with TestClient(create_app(container)) as client:
        response = client.post(
            path,
            json={
                "action": "CONFIRM_PROVIDER_NOT_CREATED",
                "reason": "must not refund while generation claim is active",
                "explicit_confirmation": True,
                "evidence_reference": "support-case:active-claim-001",
            },
            headers=headers,
        )

    assert response.status_code == 409, response.text
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        stored = session.get(GenerationJob, job.id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        refund_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type.in_(["REFUNDED", "RECONCILED_REFUNDED"]),
            )
        )

        assert workspace is not None and workspace.credit_balance == 40
        assert stored is not None
        assert stored.status == JobStatus.RESERVED.value
        assert stored.claim_token == claim_token
        assert entry is not None and entry.status == "RECONCILIATION_REQUIRED"
        assert entry.refunded_credits == 0
        assert refund_events == 0


def test_internal_settlement_rejects_active_claim_and_preserves_reservation(
    container,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Active claim settlement guard")
    provider = _CancelledPollProvider()
    _register_submission_provider(container, provider, model="active-claim-model")
    job = _reserve(
        container,
        project_id,
        idempotency_key="active-claim-settlement-guard",
        provider=provider.name,
        model="active-claim-model",
    )
    claim_token = container.gateway._claim_for_submission(job.id)
    assert claim_token is not None
    assert container.gateway._begin_provider_submission(
        job.id,
        claim_token,
        {"prompt": "provider call is still active"},
        provider.name,
    )
    with container.database.session() as session:
        stored = session.get(GenerationJob, job.id)
        assert stored is not None
        transition = container.workspace_credits.require_reconciliation(
            session,
            stored,
            reason="TEST_ACTIVE_SETTLEMENT_RECONCILIATION",
        )
        assert transition.status == "RECONCILIATION_REQUIRED"

    path = f"/internal/generations/{job.id}/credit-reconcile"
    headers = {
        "Authorization": f"Bearer {container.settings.platform_api_key}",
        "Idempotency-Key": "active-claim-settlement-decision",
    }
    with TestClient(create_app(container)) as client:
        response = client.post(
            path,
            json={
                "action": "CONFIRM_PROVIDER_ACCEPTED",
                "reason": "must not settle while the provider claim is active",
                "explicit_confirmation": True,
                "evidence_reference": "support-case:active-settlement-001",
            },
            headers=headers,
        )

    assert response.status_code == 409, response.text
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        stored = session.get(GenerationJob, job.id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        settlement_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type == "RECONCILED_SETTLED",
            )
        )

        assert workspace is not None and workspace.credit_balance == 40
        assert stored is not None
        assert stored.status == JobStatus.RESERVED.value
        assert stored.claim_token == claim_token
        assert stored.submission_state == "SENT_UNCONFIRMED"
        assert entry is not None and entry.status == "RECONCILIATION_REQUIRED"
        assert settlement_events == 0


def test_same_idempotency_key_reserves_independently_per_project(container):  # type: ignore[no-untyped-def]
    workspace_id, project_ids = _free_projects(container, "Project A", "Project B")

    first = _reserve(
        container,
        project_ids[0],
        idempotency_key="shared-project-local-key",
        prompt="First project action",
    )
    second = _reserve(
        container,
        project_ids[1],
        idempotency_key="shared-project-local-key",
        prompt="Second project action",
    )

    assert first.id != second.id
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        entries = list(
            session.scalars(
                select(WorkspaceCreditEntry).where(
                    WorkspaceCreditEntry.workspace_id == workspace_id,
                    WorkspaceCreditEntry.idempotency_key == "shared-project-local-key",
                )
            )
        )

        assert workspace is not None and workspace.credit_balance == 30
        assert {entry.project_id for entry in entries} == set(project_ids)
        assert {entry.status for entry in entries} == {"RESERVED"}


def test_settlement_compare_and_swap_replays_stale_winner_without_double_effect(
    container,
):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _free_projects(container, "Settlement CAS")
    job = _reserve(container, project_id, idempotency_key="settlement-cas")

    stale = container.database.Session()
    winner = container.database.Session()
    try:
        stale_job = stale.get(GenerationJob, job.id)
        stale_entry = stale.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        assert stale_job is not None and stale_entry is not None
        assert stale_entry.status == "RESERVED"
        # End the read transaction while retaining the stale identity-map snapshot.
        stale.commit()

        winner_job = winner.get(GenerationJob, job.id)
        assert winner_job is not None
        settled = container.workspace_credits.settle_generation(winner, winner_job)
        winner.commit()

        replayed = container.workspace_credits.settle_generation(stale, stale_job)
        stale.commit()
    finally:
        winner.close()
        stale.close()

    assert settled.status == "SETTLED" and settled.replayed is False
    assert replayed.status == "SETTLED" and replayed.replayed is True
    with container.database.session() as session:
        stored_job = session.get(GenerationJob, job.id)
        assert stored_job is not None
        with pytest.raises(WorkspaceCreditConflict, match="cannot refund credit state SETTLED"):
            container.workspace_credits.refund_generation(
                session,
                stored_job,
                reason="must not reverse a settled generation",
            )

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        settlement_events = session.scalar(
            select(func.count(WorkspaceCreditEvent.id)).where(
                WorkspaceCreditEvent.generation_job_id == job.id,
                WorkspaceCreditEvent.event_type == "SETTLED",
            )
        )

        assert workspace is not None and workspace.credit_balance == 40
        assert entry is not None
        assert entry.status == "SETTLED"
        assert entry.version == 2
        assert entry.settled_credits == 10
        assert entry.refunded_credits == 0
        assert settlement_events == 1


# --- Every plan draws on the same wallet -------------------------------------
#
# A plan sets the grant, the discount and which models may be used. It never
# decided whether a generation costs anything — except that it did: any tier
# other than FREE was quoted, the quote was written onto the job, and then
# nothing was reserved, nothing settled, and an ambiguous provider result left
# no credit to hold for reconciliation.


@pytest.mark.parametrize("plan_tier", ["FREE", "PRO", "ENTERPRISE"])
def test_every_plan_reserves_and_settles_against_the_same_wallet(container, plan_tier):  # type: ignore[no-untyped-def]
    workspace_id, (project_id,) = _plan_projects(container, plan_tier, "Billed Plan")

    job = _reserve(container, project_id, idempotency_key=f"{plan_tier}-billed", credits=10)

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        assert workspace.credit_balance == 40
        assert entry is not None
        assert entry.status == "RESERVED"
        assert entry.credits == 10
        assert session.get(GenerationJob, job.id).workspace_credit_required is True

    with container.database.session() as session:
        settled = container.workspace_credits.settle_generation(
            session, session.get(GenerationJob, job.id), reason="GENERATION_COMPLETED"
        )
        assert settled.applied is True

    with container.database.session() as session:
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        assert entry.status == "SETTLED"
        assert entry.settled_credits == 10
        assert session.get(Workspace, workspace_id).credit_balance == 40


def test_a_paid_workspace_out_of_credits_is_refused_before_the_provider(container):  # type: ignore[no-untyped-def]
    """The refusal a paid tier could not previously reach."""

    workspace_id, (project_id,) = _plan_projects(container, "PRO", "Exhausted")

    with pytest.raises(InsufficientWorkspaceCredits, match="required=51, available=50"):
        _reserve(container, project_id, idempotency_key="pro-too-expensive", credits=51)

    with container.database.session() as session:
        # Refused whole: no balance moved, no entry, no job.
        assert session.get(Workspace, workspace_id).credit_balance == 50
        assert (
            session.scalar(
                select(func.count(WorkspaceCreditEntry.id)).where(
                    WorkspaceCreditEntry.workspace_id == workspace_id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(GenerationJob.id)).where(GenerationJob.project_id == project_id)
            )
            == 0
        )


def test_the_development_bypass_workspace_is_not_a_plan_and_is_not_charged(container):  # type: ignore[no-untyped-def]
    """`ALL` is the authentication-disabled local bypass, not a tier.

    It still receives server pricing and CostRecords. Charging it would make
    local development spend a real balance, which is why it is excluded by the
    same property that includes every real plan.
    """

    workspace_id, (project_id,) = _plan_projects(container, "ALL", "Local Bypass")

    job = _reserve(container, project_id, idempotency_key="bypass-not-charged", credits=10)

    with container.database.session() as session:
        assert session.get(Workspace, workspace_id).credit_balance == 50
        assert (
            session.scalar(
                select(func.count(WorkspaceCreditEntry.id)).where(
                    WorkspaceCreditEntry.workspace_id == workspace_id
                )
            )
            == 0
        )
        assert session.get(GenerationJob, job.id).workspace_credit_required is False


def test_running_out_of_credits_is_402_and_a_plan_denial_is_403(container):  # type: ignore[no-untyped-def]
    """Two different problems with two different fixes: top up, or upgrade.

    They shared a 403 while a paid tier could not run out of credits. Now that
    it can, a client that cannot tell them apart cannot route the user to the
    thing that would actually help.
    """

    from entitlement_core import PlanEntitlementDenied
    from video_platform_api.main import create_app

    workspace_id, (project_id,) = _plan_projects(container, "PRO", "Status Codes")
    with container.database.session() as session:
        session.get(Workspace, workspace_id).credit_balance = 0

    app = create_app(container)
    with TestClient(app) as client:
        # For real: a PRO workspace at zero, quoted by the server, refused.
        broke = client.post(
            "/v1/generations",
            json={
                "project_id": project_id,
                "type": "video",
                "prompt": "one visible action",
                "idempotency_key": "pro-out-of-credits",
            },
        )
        assert broke.status_code == 402, broke.text
        assert "insufficient workspace credits" in broke.json()["detail"]

        # And the entitlement denial it used to be indistinguishable from.
        original = container.gateway.create

        def not_entitled(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise PlanEntitlementDenied("this plan cannot use that model")

        container.gateway.create = not_entitled  # type: ignore[method-assign]
        try:
            denied = client.post(
                "/v1/generations",
                json={
                    "project_id": project_id,
                    "type": "video",
                    "prompt": "one visible action",
                    "idempotency_key": "pro-not-entitled",
                },
            )
        finally:
            container.gateway.create = original  # type: ignore[method-assign]
        assert denied.status_code == 403, denied.text

    with container.database.session() as session:
        assert session.get(Workspace, workspace_id).credit_balance == 0
        assert (
            session.scalar(
                select(func.count(GenerationJob.id)).where(GenerationJob.project_id == project_id)
            )
            == 0
        )
