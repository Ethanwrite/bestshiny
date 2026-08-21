from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from generation_gateway import (
    FlowAffinityConflict,
    FlowAffinityUnavailable,
    FlowProjectAllocator,
    GenerationGateway,
    ProviderRouter,
)
from google_flow_provider import GoogleFlowProvider
from platform_contracts import GenerationRequest
from platform_shared import Settings
from production_domain.models import (
    AccountStatus,
    BrowserWorker,
    FlowMigrationPlan,
    FlowMigrationStatus,
    GenerationJob,
    JobStatus,
    Project,
    ProviderAccount,
    ProviderProjectBinding,
    ProviderProjectBindingStatus,
    WorkerStatus,
    utcnow,
)
from provider_sdk import (
    LIVE_PROVIDER_CONFIRMATION,
    GenerationProvider,
    ProviderError,
    ProviderHealth,
    ProviderJob,
    ProviderPollIdentity,
    ProviderSubmission,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError


class _Provisioner:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def create_project(
        self,
        *,
        local_project_id: str,
        provider_account_id: str,
        worker_id: str,
        idempotency_key: str,
    ) -> str:
        self.calls.append(
            {
                "local_project_id": local_project_id,
                "provider_account_id": provider_account_id,
                "worker_id": worker_id,
                "idempotency_key": idempotency_key,
            }
        )
        return f"remote-flow-{local_project_id}"


class _ClaimFailingAllocator(FlowProjectAllocator):
    def _claim_provisioning(
        self,
        *,
        local_project_id: str,
        provider_account_id: str,
    ) -> tuple[ProviderProjectBinding, bool]:
        del local_project_id, provider_account_id
        raise RuntimeError("binding database unavailable")


class _FlowProvider(GenerationProvider):
    name = "google_flow"

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        return await self.generate_video(request, account_id=account_id, worker_id=worker_id)

    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del account_id, worker_id
        self.requests.append(dict(request))
        return ProviderSubmission(f"flow-job-{len(self.requests)}")

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        del asset, account_id, worker_id
        return "unused-media"

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool:
        del provider_media_id, account_id, worker_id
        return True

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
        poll_identity: ProviderPollIdentity | None = None,
    ) -> ProviderJob:
        del account_id, worker_id, generation_type, poll_identity
        return ProviderJob(provider_job_id, "RUNNING")

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        del provider_job_id, account_id, worker_id
        return False

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        del account_id, worker_id
        return None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, "offline fixture")


class _PollRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []

    async def dispatch(
        self,
        worker_id: str,
        message_type: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append((worker_id, message_type, payload, kwargs))
        if message_type == "provider.media_url":
            return {"url": "https://example.test/result.mp4"}
        return {
            "status": 200,
            "data": {
                "media": [
                    {
                        "mediaMetadata": {
                            "mediaStatus": {"mediaGenerationStatus": ("MEDIA_GENERATION_STATUS_SUCCESSFUL")}
                        }
                    }
                ]
            },
        }

    def available_workers(self, provider: str) -> list[Any]:
        del provider
        return [type("Worker", (), {"id": "worker-1"})()]


def _reserved_job(container, project_id: str, *, suffix: str) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    token = f"claim-{suffix}"
    with container.database.session() as session:
        job = GenerationJob(
            project_id=project_id,
            generation_type="video",
            provider="google_flow",
            model="veo",
            status=JobStatus.RESERVED.value,
            request_json={"prompt": "one action"},
            request_hash=suffix * 64,
            claim_token=token,
            claim_expires_at=utcnow() + timedelta(minutes=5),
        )
        session.add(job)
        session.flush()
        return job.id, token


@pytest.mark.asyncio
async def test_flow_project_auto_affinity(container, project, account_worker):
    del account_worker
    provisioner = _Provisioner()
    allocator = FlowProjectAllocator(container.database, container.scheduler, provisioner)
    provider = _FlowProvider()
    router = ProviderRouter()
    router.register(provider)
    router.register_model("google_flow", "veo", "video")
    gateway = GenerationGateway(
        container.database,
        router,
        container.media,
        container.scheduler,
        flow_affinity=allocator,
    )

    first, _ = gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="google_flow",
            model="veo",
            prompt="one action",
            idempotency_key="flow-affinity-first",
        )
    )
    first = await gateway.process(first.id)
    second, _ = gateway.create(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="google_flow",
            model="veo",
            prompt="the next action",
            idempotency_key="flow-affinity-second",
        )
    )
    second = await gateway.process(second.id)

    assert first.status == JobStatus.SUBMITTED.value
    assert second.status == JobStatus.SUBMITTED.value
    assert len(provisioner.calls) == 1
    assert first.account_id == second.account_id
    assert first.provider_project_id == second.provider_project_id
    assert first.provider_project_id == f"remote-flow-{project.id}"
    assert [request["_provider_project_id"] for request in provider.requests] == [
        first.provider_project_id,
        first.provider_project_id,
    ]
    with container.database.session() as session:
        binding = session.scalar(
            select(ProviderProjectBinding).where(
                ProviderProjectBinding.local_project_id == project.id,
                ProviderProjectBinding.provider == "google_flow",
                ProviderProjectBinding.status == ProviderProjectBindingStatus.READY.value,
            )
        )
        assert binding is not None
        assert binding.provider_account_id == first.account_id
        assert binding.provider_project_id == first.provider_project_id


@pytest.mark.parametrize(
    "historical_status",
    [
        ProviderProjectBindingStatus.READY.value,
        ProviderProjectBindingStatus.DISABLED.value,
        ProviderProjectBindingStatus.FAILED.value,
    ],
)
def test_flow_remote_project_unique_owner_across_all_statuses(
    container,
    project,
    account_worker,
    historical_status,
):
    account_id, _ = account_worker
    with container.database.session() as session:
        other = Project(title="Another local project")
        session.add(other)
        session.flush()
        other_project_id = other.id
        session.add(
            ProviderProjectBinding(
                local_project_id=project.id,
                provider="google_flow",
                provider_account_id=account_id,
                provider_project_id="shared-remote-flow-project",
                status=historical_status,
            )
        )

    with pytest.raises(FlowAffinityConflict, match="permanently owned"):
        container.flow_affinity.bind_existing(
            local_project_id=other_project_id,
            provider_account_id=account_id,
            provider_project_id="shared-remote-flow-project",
        )

    with pytest.raises(IntegrityError):
        with container.database.session() as session:
            session.add(
                ProviderProjectBinding(
                    local_project_id=other_project_id,
                    provider="google_flow",
                    provider_account_id=account_id,
                    provider_project_id="shared-remote-flow-project",
                    status=ProviderProjectBindingStatus.READY.value,
                )
            )

    with container.database.session() as session:
        owners = session.scalar(
            select(func.count(ProviderProjectBinding.id)).where(
                ProviderProjectBinding.provider == "google_flow",
                ProviderProjectBinding.provider_project_id == "shared-remote-flow-project",
            )
        )
        assert owners == 1


def test_flow_local_project_can_migrate_to_a_new_remote_identity(
    container,
    project,
    account_worker,
):
    account_id, _ = account_worker
    with container.database.session() as session:
        historical = ProviderProjectBinding(
            local_project_id=project.id,
            provider="google_flow",
            provider_account_id=account_id,
            provider_project_id="historical-remote-flow-project",
            status=ProviderProjectBindingStatus.DISABLED.value,
        )
        session.add(historical)
        session.flush()
        historical_id = historical.id

    replacement = container.flow_affinity.bind_existing(
        local_project_id=project.id,
        provider_account_id=account_id,
        provider_project_id="replacement-remote-flow-project",
    )

    assert replacement.id != historical_id
    assert replacement.status == ProviderProjectBindingStatus.READY.value
    with container.database.session() as session:
        rows = session.scalars(
            select(ProviderProjectBinding)
            .where(
                ProviderProjectBinding.local_project_id == project.id,
                ProviderProjectBinding.provider == "google_flow",
            )
            .order_by(ProviderProjectBinding.provider_project_id)
        ).all()
        assert [(row.provider_project_id, row.status) for row in rows] == [
            ("historical-remote-flow-project", ProviderProjectBindingStatus.DISABLED.value),
            ("replacement-remote-flow-project", ProviderProjectBindingStatus.READY.value),
        ]


@pytest.mark.asyncio
async def test_flow_poll_requires_account_project_job_identity(
    container,
    project,
    account_worker,
):
    account_id, _ = account_worker
    with container.database.session() as session:
        job = GenerationJob(
            project_id=project.id,
            generation_type="video",
            provider="google_flow",
            model="veo",
            status=JobStatus.SUBMITTED.value,
            request_json={"prompt": "one action"},
            provider_request_json={"_provider_project_id": "flow-project-strong-id"},
            request_hash="p" * 64,
            provider_job_id="flow-job-strong-id",
            provider_project_id="flow-project-strong-id",
            submission_state="CONFIRMED",
            account_id=account_id,
        )
        session.add(job)
        session.flush()
        job_id = job.id
    runtime = _PollRuntime()
    settings = Settings(
        _env_file=None,
        provider_mode="live",
        allow_live_provider_calls=True,
        live_provider_confirmation=LIVE_PROVIDER_CONFIRMATION,
    )
    provider = GoogleFlowProvider(runtime, settings, container.database)
    identity = ProviderPollIdentity(
        local_generation_job_id=job_id,
        provider_account_id=account_id,
        provider_project_id="flow-project-strong-id",
        provider_job_id="flow-job-strong-id",
    )

    with pytest.raises(ProviderError, match="identity is required"):
        await provider.get_job(
            "flow-job-strong-id",
            account_id=account_id,
            worker_id="worker-1",
            generation_type="video",
        )
    invalid_calls = [
        (replace(identity, local_generation_job_id="another-local-job"), account_id),
        (replace(identity, provider_account_id="another-account"), "another-account"),
        (replace(identity, provider_project_id="another-project"), account_id),
        (replace(identity, provider_job_id="another-remote-job"), account_id),
    ]
    for invalid_identity, routed_account_id in invalid_calls:
        with pytest.raises(ProviderError):
            await provider.get_job(
                invalid_identity.provider_job_id,
                account_id=routed_account_id,
                worker_id="worker-1",
                generation_type="video",
                poll_identity=invalid_identity,
            )
    assert runtime.calls == []

    result = await provider.get_job(
        "flow-job-strong-id",
        account_id=account_id,
        worker_id="worker-1",
        generation_type="video",
        poll_identity=identity,
    )
    assert result.status == "COMPLETED"
    poll_body = next(call[2]["body"] for call in runtime.calls if "batchCheck" in call[2]["url"])
    assert poll_body == {
        "media": [
            {
                "name": "flow-job-strong-id",
                "projectId": "flow-project-strong-id",
            }
        ]
    }


@pytest.mark.asyncio
async def test_flow_affinity_requires_review_instead_of_blind_failover(
    container,
    project,
    account_worker,
):
    sticky_account_id, _ = account_worker
    with container.database.session() as session:
        sticky = session.get(ProviderAccount, sticky_account_id)
        assert sticky is not None
        sticky.status = AccountStatus.DISABLED.value
        fallback = ProviderAccount(
            provider="google_flow",
            account_identifier="healthy-fallback@example.com",
            credits=100,
            video_capacity=1,
            supported_models=["veo"],
        )
        session.add(fallback)
        session.flush()
        fallback_worker = BrowserWorker(
            id="healthy-fallback-worker",
            provider="google_flow",
            account_id=fallback.id,
            connection_id="healthy-fallback-connection",
            status=WorkerStatus.READY.value,
            capabilities=["video"],
        )
        session.add(fallback_worker)
        fallback.worker_id = fallback_worker.id
        session.add(
            ProviderProjectBinding(
                local_project_id=project.id,
                provider="google_flow",
                provider_account_id=sticky_account_id,
                provider_project_id="sticky-remote-project",
                status=ProviderProjectBindingStatus.READY.value,
            )
        )
        session.flush()
        fallback_account_id = fallback.id
    job_id, claim_token = _reserved_job(container, project.id, suffix="m")

    with pytest.raises(FlowAffinityUnavailable) as error:
        await container.flow_affinity.acquire_for_generation(
            local_project_id=project.id,
            capability="video",
            model="veo",
            priority=0,
            generation_job_id=job_id,
            claim_token=claim_token,
        )
    assert error.value.code == "FLOW_MIGRATION_REQUIRED"
    with container.database.session() as session:
        binding = session.scalar(
            select(ProviderProjectBinding).where(
                ProviderProjectBinding.local_project_id == project.id,
                ProviderProjectBinding.provider == "google_flow",
            )
        )
        assert binding is not None
        assert binding.status == ProviderProjectBindingStatus.MIGRATION_REQUIRED.value
        plan = session.scalar(
            select(FlowMigrationPlan).where(FlowMigrationPlan.source_binding_id == binding.id)
        )
        assert plan is not None
        assert plan.migration_status == FlowMigrationStatus.USER_REVIEW_REQUIRED.value
        assert plan.target_account_id is None
        job = session.get(GenerationJob, job_id)
        fallback = session.get(ProviderAccount, fallback_account_id)
        assert job is not None and job.account_id is None
        assert fallback is not None
        assert fallback.video_inflight == 0
        assert fallback.pending_jobs == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding_status", "expected_code"),
    [
        (ProviderProjectBindingStatus.DISABLED.value, "FLOW_AFFINITY_DISABLED"),
        (ProviderProjectBindingStatus.FAILED.value, "FLOW_AFFINITY_FAILED"),
    ],
)
async def test_inactive_flow_affinity_never_reprovisions_implicitly(
    container,
    project,
    account_worker,
    binding_status,
    expected_code,
):
    account_id, _ = account_worker
    with container.database.session() as session:
        session.add(
            ProviderProjectBinding(
                local_project_id=project.id,
                provider="google_flow",
                provider_account_id=account_id,
                provider_project_id="historical-remote-project",
                status=binding_status,
            )
        )
    job_id, claim_token = _reserved_job(container, project.id, suffix="i")
    provisioner = _Provisioner()
    allocator = FlowProjectAllocator(container.database, container.scheduler, provisioner)

    with pytest.raises(FlowAffinityUnavailable) as error:
        await allocator.acquire_for_generation(
            local_project_id=project.id,
            capability="video",
            model="veo",
            priority=0,
            generation_job_id=job_id,
            claim_token=claim_token,
        )

    assert error.value.code == expected_code
    assert provisioner.calls == []
    with container.database.session() as session:
        bindings = session.scalars(
            select(ProviderProjectBinding).where(
                ProviderProjectBinding.local_project_id == project.id,
                ProviderProjectBinding.provider == "google_flow",
            )
        ).all()
        assert len(bindings) == 1


@pytest.mark.asyncio
async def test_flow_claim_failure_releases_reserved_capacity(
    container,
    project,
    account_worker,
):
    account_id, worker_id = account_worker
    job_id, claim_token = _reserved_job(container, project.id, suffix="r")
    provisioner = _Provisioner()
    allocator = _ClaimFailingAllocator(
        container.database,
        container.scheduler,
        provisioner,
    )

    with pytest.raises(RuntimeError, match="binding database unavailable"):
        await allocator.acquire_for_generation(
            local_project_id=project.id,
            capability="video",
            model="veo",
            priority=0,
            generation_job_id=job_id,
            claim_token=claim_token,
        )

    assert provisioner.calls == []
    with container.database.session() as session:
        job = session.get(GenerationJob, job_id)
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, worker_id)
        assert job is not None
        assert job.account_id is None
        assert job.worker_id is None
        assert job.reservation_released_at is not None
        assert account is not None
        assert account.video_inflight == 0
        assert account.pending_jobs == 0
        assert worker is not None
        assert worker.current_jobs == 0
