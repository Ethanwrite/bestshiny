from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from entitlement_core import (
    LiveCanaryDenied,
    LiveCanaryPermitService,
    ModelRoleRuntime,
    WorkspaceModelResolver,
)
from memory_core import (
    LocalTestEmbeddingProvider,
    MemoryEmbeddingUnavailable,
    MemoryQuery,
    ModelRoleEmbeddingProvider,
    MultimodalContent,
    MultimodalMemoryEngine,
)
from narrative_core import AuthoritativeTimelineStateEngine
from platform_contracts import GenerationRequest
from platform_shared import Settings
from production_domain.models import (
    AccountStatus,
    BillingEvidenceSource,
    CostRecord,
    DecisionRecord,
    EmbeddingEvidence,
    Episode,
    GenerationCandidate,
    GenerationJob,
    GenerationPolicy,
    LiveCanaryPermit,
    LiveCanaryUsage,
    ModelDefinition,
    ModelExecutionRecord,
    Project,
    ProviderAccount,
    ProviderBillingEvidence,
    RetryCategory,
    Scene,
    Shot,
    ShotStatus,
    TimelineState,
    TimelineTransition,
    TimelineTransitionType,
    utcnow,
)
from provider_sdk import (
    LIVE_PROVIDER_CONFIRMATION,
    EmbeddingCapability,
    ProviderCapability,
    ProviderCapabilityCatalog,
    ProviderError,
    ProviderJob,
    ProviderSubmission,
    ProviderTrustLevel,
)
from sqlalchemy import select
from video_platform_api.container import build_container


class _FixtureEmbeddingCapability(EmbeddingCapability):
    trust_level = ProviderTrustLevel.PRODUCTION
    configured = True

    def __init__(self) -> None:
        self.call_count = 0

    async def create_embeddings(
        self,
        *,
        model: str,
        inputs: str | list[str] | list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.call_count += 1
        del model, inputs
        dimension = int((parameters or {}).get("dimensions", 256))
        return {
            "data": [{"embedding": [1.0] * dimension}],
            "usage": {"prompt_tokens": 4, "total_tokens": 4, "cost": "0.02"},
        }


def test_business_code_cannot_call_voyage_directly(container) -> None:  # type: ignore[no-untyped-def]
    source = Path(build_container.__code__.co_filename).read_text(encoding="utf-8")

    assert "VoyageMultimodalEmbeddingProvider(" not in source
    assert "ModelRoleEmbeddingProvider(" in source
    assert isinstance(container.memory.embeddings, LocalTestEmbeddingProvider)


@pytest.mark.asyncio
async def test_model_role_runtime_embedding(container, project) -> None:  # type: ignore[no-untyped-def]
    providers = ProviderCapabilityCatalog()
    providers.register(
        "voyage",
        _FixtureEmbeddingCapability(),
        {ProviderCapability.EMBEDDINGS.value},
    )
    runtime = ModelRoleRuntime(
        container.database,
        WorkspaceModelResolver(container.database, container.model_infrastructure),
        providers,
        provider_mode="mock",
    )

    execution = await runtime.execute_embeddings(project.id, inputs="Lin Jin continuity state")

    assert execution.resolved_model.provider == "voyage"
    assert execution.resolved_model.provider_model_id == "voyage-multimodal-3.5"
    assert len(execution.response["data"][0]["embedding"]) == 256
    with container.database.session() as session:
        record = session.scalar(
            select(DecisionRecord).where(DecisionRecord.id == execution.decision_record_id)
        )
        assert record is not None
        assert record.decision_type == "MODEL_ROLE_EXECUTION"
        assert record.input_features["role"] == "MULTIMODAL_EMBEDDING"
        execution_record = session.get(ModelExecutionRecord, execution.execution_record_id)
        assert execution_record is not None
        assert execution_record.request_hash
        assert execution_record.actual_cost_usd is None
        assert execution_record.cost_source == "UNKNOWN"
        assert execution_record.metadata_json["provider_mode"] == "mock"
        assert execution_record.metadata_json["reported_actual_cost_ignored"] is True


def test_model_role_embedding_returns_project_scoped_provenance(container, project) -> None:  # type: ignore[no-untyped-def]
    providers = ProviderCapabilityCatalog()
    providers.register(
        "voyage",
        _FixtureEmbeddingCapability(),
        {ProviderCapability.EMBEDDINGS.value},
    )
    runtime = ModelRoleRuntime(
        container.database,
        WorkspaceModelResolver(container.database, container.model_infrastructure),
        providers,
        provider_mode="mock",
    )
    adapter = ModelRoleEmbeddingProvider(runtime, dimension=256)

    embedded = adapter.embed_with_provenance(
        MultimodalContent(text="Lin Jin", image_urls=["https://example.invalid/lin.png"]),
        input_type="document",
        project_id=project.id,
    )

    assert len(embedded.values) == 256
    assert embedded.provenance.provider == "voyage"
    assert embedded.provenance.model == "voyage-multimodal-3.5"
    assert embedded.provenance.input_type == "document"
    with container.database.session() as session:
        evidence = session.scalar(select(EmbeddingEvidence).where(EmbeddingEvidence.project_id == project.id))
        assert evidence is not None
        assert evidence.embedding_dimension == 256
        assert len(evidence.embedding_hash) == 64


def test_model_role_embedding_rejects_unverified_direct_video_url(container, project) -> None:  # type: ignore[no-untyped-def]
    capability = _FixtureEmbeddingCapability()
    providers = ProviderCapabilityCatalog()
    providers.register(
        "voyage",
        capability,
        {ProviderCapability.EMBEDDINGS.value},
    )
    runtime = ModelRoleRuntime(
        container.database,
        WorkspaceModelResolver(container.database, container.model_infrastructure),
        providers,
        provider_mode="mock",
    )
    adapter = ModelRoleEmbeddingProvider(runtime, dimension=256)

    with pytest.raises(MemoryEmbeddingUnavailable, match="extract bounded timestamped image frames"):
        adapter.embed_with_provenance(
            MultimodalContent(video_urls=["https://media.invalid/candidate.mp4"]),
            input_type="document",
            project_id=project.id,
        )

    assert capability.call_count == 0
    with container.database.session() as session:
        assert (
            session.scalar(select(ModelExecutionRecord).where(ModelExecutionRecord.project_id == project.id))
            is None
        )
        assert (
            session.scalar(select(EmbeddingEvidence).where(EmbeddingEvidence.project_id == project.id))
            is None
        )


def test_memory_vector_failure_degrades_to_structured_timeline(container, project) -> None:  # type: ignore[no-untyped-def]
    runtime = ModelRoleRuntime(
        container.database,
        WorkspaceModelResolver(container.database, container.model_infrastructure),
        ProviderCapabilityCatalog(),
        provider_mode="mock",
    )
    memory = MultimodalMemoryEngine(
        container.database,
        ModelRoleEmbeddingProvider(runtime, dimension=256),
        enabled=True,
    )

    assert memory.search(MemoryQuery(project_id=project.id, text="current costume")) == []

    with container.database.session() as session:
        record = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.project_id == project.id,
                DecisionRecord.decision_type == "MEMORY_VECTOR_DEGRADED",
            )
        )
        assert record is not None
        assert record.selected_action == "STRUCTURED_TIMELINE_ONLY"
        assert record.reason_codes == ["MEMORY_VECTOR_DEGRADED"]


def test_actual_cost_not_faked_when_unknown(container, project) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        job = GenerationJob(
            project_id=project.id,
            generation_type="video",
            provider="seedance",
            model="seedance-1.0-pro",
            request_json={"duration": 5},
            request_hash="a" * 64,
            cost_estimate=1.25,
        )
        session.add(job)
        session.flush()
        job_id = job.id

    record = container.cost.record_job(job_id, estimated_cost=1.25, actual_cost=None)
    evidence = container.cost.record_billing_evidence(
        job_id,
        evidence_key="provider-completion-without-billing",
        source=BillingEvidenceSource.UNKNOWN,
        estimated_cost_usd=Decimal("1.25"),
    )

    assert record.actual_cost is None
    assert evidence.actual_cost_usd is None
    assert evidence.source == BillingEvidenceSource.UNKNOWN.value
    with container.database.session() as session:
        stored_job = session.get(GenerationJob, job_id)
        stored_cost = session.get(CostRecord, record.id)
        stored_evidence = session.get(ProviderBillingEvidence, evidence.id)
        assert stored_job is not None and stored_job.actual_cost is None
        assert stored_cost is not None and stored_cost.actual_cost is None
        assert stored_evidence is not None and stored_evidence.verified_at is None


def test_cost_per_accepted_shot_includes_failed_candidates(container, project) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Cost evidence", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Cost scene")
        session.add(scene)
        session.flush()
        shot = Shot(scene_id=scene.id, sequence=1, prompt="Walk through the door")
        session.add(shot)
        session.flush()
        candidates = [
            GenerationCandidate(shot_id=shot.id, attempt_number=index, status=status)
            for index, status in ((1, "REJECTED"), (2, "REJECTED"), (3, "COMMITTED"))
        ]
        session.add_all(candidates)
        session.flush()
        shot.committed_candidate_id = candidates[-1].id
        for candidate, cost, accepted in zip(
            candidates,
            (0.5, 0.5, 0.6),
            (False, False, True),
            strict=True,
        ):
            session.add(
                CostRecord(
                    project_id=project.id,
                    shot_id=shot.id,
                    candidate_id=candidate.id,
                    provider="seedance",
                    model="seedance-1.0-pro",
                    actual_cost=cost,
                    accepted=accepted,
                    wasted=not accepted,
                )
            )
        shot_id = shot.id

    economics = container.cost.shot_cost(shot_id)

    assert economics["attempts"] == 3
    assert economics["accepted_cost"] == 0.6
    assert economics["wasted_cost"] == 1.0
    assert economics["accepted_shot_cost"] == 1.6
    assert economics["cost_per_accepted_shot"] == 1.6


def test_router_observations_ramp_without_overwriting_manual_prior(container, project) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        session.add_all(
            [
                CostRecord(
                    project_id=project.id,
                    provider="seedance",
                    model="doubao-seedance-2-5-260628",
                    estimated_cost=100,
                    accepted=False,
                    wasted=True,
                )
                for _ in range(3)
            ]
        )

    _score, evidence = container.capability_resolver._routing_score(
        "seedance",
        GenerationPolicy.TEXT_TO_VIDEO.value,
        "seedance",
        "DIALOGUE",
    )

    assert evidence["prior_weight"] == 0.8
    assert evidence["observation_weight"] == 0.2
    assert evidence["minimum_sample_count"] == 20
    assert evidence["observation_sample_count"] == 3
    assert evidence["effective_observation_weight"] == 0.03
    assert evidence["effective_prior_weight"] == 0.97
    assert evidence["observed_acceptance_rate"] == 0
    assert evidence["blended_task_quality"] == pytest.approx(
        evidence["expected_task_quality"] * 0.97,
        abs=0.0002,
    )


def test_live_canary_request_limit(container) -> None:  # type: ignore[no-untyped-def]
    service = LiveCanaryPermitService(container.database)
    permit = service.create(
        provider="openrouter",
        model="openai/gpt-5.6",
        max_requests=1,
        max_cost_usd="0.10",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="single offline gate test",
    )

    first = service.reserve(
        permit.id,
        provider="openrouter",
        model="openai/gpt-5.6",
        estimated_cost_usd="0.01",
        idempotency_key="request-1",
    )
    assert first.replayed is False
    with container.database.session() as session:
        stored = session.get(LiveCanaryPermit, permit.id)
        assert stored is not None
        assert stored.status == "EXHAUSTED"
    with pytest.raises(LiveCanaryDenied, match="request limit"):
        service.reserve(
            permit.id,
            provider="openrouter",
            model="openai/gpt-5.6",
            estimated_cost_usd="0.01",
            idempotency_key="request-2",
        )


def test_live_canary_cost_limit(container) -> None:  # type: ignore[no-untyped-def]
    service = LiveCanaryPermitService(container.database)
    permit = service.create(
        provider="voyage",
        model="voyage-multimodal-3.5",
        max_requests=3,
        max_cost_usd="0.05",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="bounded embedding canary",
    )

    service.reserve(
        permit.id,
        provider="voyage",
        model="voyage-multimodal-3.5",
        estimated_cost_usd="0.04",
        idempotency_key="embedding-1",
    )
    with pytest.raises(LiveCanaryDenied, match="cost limit"):
        service.reserve(
            permit.id,
            provider="voyage",
            model="voyage-multimodal-3.5",
            estimated_cost_usd="0.02",
            idempotency_key="embedding-2",
        )


def _live_media_container(tmp_path):  # type: ignore[no-untyped-def]
    live = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'live-media-canary.db'}",
            storage_root=tmp_path / "live-media",
            deployment_environment="test",
            auth_required=False,
            provider_mode="live",
            allow_live_provider_calls=True,
            live_provider_confirmation=LIVE_PROVIDER_CONFIRMATION,
            openrouter_api_key="offline-placeholder-never-sent",
            # OpenRouter fetches references itself, so live media must resolve
            # to an HTTPS URL rather than an unusable provider media identifier.
            public_base_url="https://media.invalid",
            # Local disk cannot presign; the signed local route stands in for
            # object storage so this offline test can reach the canary boundary.
            local_reference_signing_key="live-canary-reference-key",
        )
    )
    live.model_infrastructure.configure_runtime_model(
        "kling-3-standard-openrouter",
        "kwaivgi/kling-v3.0-std",
        enabled=True,
        live_enabled=True,
    )
    with live.database.session() as session:
        project = Project(title="Live media canary")
        session.add(project)
        session.flush()
        project_id = project.id
    return live, project_id


@pytest.mark.asyncio
async def test_live_generation_gateway_requires_matching_canary_before_transport(
    tmp_path,
    monkeypatch,
    register_bytes,
    stage_stub_output,
) -> None:  # type: ignore[no-untyped-def]
    live, project_id = _live_media_container(tmp_path)
    provider = live.providers.get("openrouter")
    asset = register_bytes(live, project_id, "START_FRAME", b"synthetic-reference")
    calls = {"upload": 0, "submit": 0}
    submitted_requests: list[dict[str, Any]] = []
    active_job_id = ""

    async def offline_upload(
        payload: dict[str, Any],
        *,
        account_id: str,
        worker_id: str,
    ) -> str:
        del payload, account_id, worker_id
        calls["upload"] += 1
        raise AssertionError("a FETCHABLE_URL provider must never be asked to ingest an upload")

    async def offline_submit(
        payload: dict[str, Any],
        *,
        account_id: str,
        worker_id: str,
    ) -> ProviderSubmission:
        del account_id, worker_id
        with live.database.session() as session:
            usage = session.scalar(
                select(LiveCanaryUsage).where(
                    LiveCanaryUsage.idempotency_key == f"generation:{active_job_id}"
                )
            )
            assert usage is not None and usage.status == "UNCERTAIN"
        calls["submit"] += 1
        submitted_requests.append(dict(payload))
        return ProviderSubmission("offline-provider-job")

    monkeypatch.setattr(provider, "upload_asset", offline_upload)
    monkeypatch.setattr(provider, "generate_video", offline_submit)

    allowed_request = GenerationRequest(
        project_id=project_id,
        type="video",
        provider="openrouter",
        model="kwaivgi/kling-v3.0-std",
        prompt="One bounded live canary shot",
        start_frame_asset_id=asset.id,
        idempotency_key="gateway-without-canary",
    )
    # A refused reservation is the spending fence, not a platform fault: the
    # job waits for a permit instead of dying as a refunded terminal failure
    # the user can only recover by rebuilding the whole action.
    allowed_job, _ = live.gateway.create(allowed_request)
    active_job_id = allowed_job.id
    denied = await live.gateway.process(allowed_job.id)
    assert denied.status == "RETRY_WAIT"
    assert denied.error_code == "LIVE_CANARY_DENIED"
    assert denied.retry_category == "RATE_LIMIT"
    assert denied.safe_to_retry is True
    assert calls == {"upload": 0, "submit": 0}

    wrong_permit = live.live_canary.create(
        provider="openrouter",
        model="another/server-model",
        max_requests=1,
        max_cost_usd="0.10",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="must never authorize the selected model",
    )
    wrong_job, _ = live.gateway.create(
        allowed_request.model_copy(update={"idempotency_key": "gateway-wrong-canary"})
    )
    active_job_id = wrong_job.id
    wrong = await live.gateway.process(wrong_job.id)
    assert wrong.status == "RETRY_WAIT"
    assert wrong.error_code == "LIVE_CANARY_DENIED"
    assert calls == {"upload": 0, "submit": 0}

    permit = live.live_canary.create(
        provider="openrouter",
        model="kwaivgi/kling-v3.0-std",
        max_requests=1,
        max_cost_usd="0.10",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="one server-selected media operation",
    )
    # Minting the matching permit is all the recovery the waiting job needs.
    with live.database.session() as session:
        stored_job = session.get(GenerationJob, allowed_job.id)
        assert stored_job is not None
        stored_job.next_retry_at = utcnow() - timedelta(seconds=1)
    active_job_id = allowed_job.id
    submitted = await live.gateway.process(allowed_job.id)
    assert submitted.status == "SUBMITTED"
    assert submitted.provider_job_id == "offline-provider-job"
    assert calls == {"upload": 0, "submit": 1}
    # The reference reached the provider as a fetchable URL, not a local asset
    # ID and not a provider media ID the provider could never resolve. It is a
    # short-lived signed URL, deliberately *not* the stored `public_url`: that
    # one points at this service's authenticated route, which an external
    # provider can neither authenticate to nor should be made to stream through.
    start_frame_url = str(submitted_requests[0]["start_frame_url"])
    assert start_frame_url.startswith("https://")
    assert start_frame_url != asset.public_url
    assert "/v1/storage/" not in start_frame_url
    assert "signature=" in start_frame_url
    assert "start_frame_provider_media_id" not in submitted_requests[0]

    replayed_job, replayed = live.gateway.create(allowed_request)
    assert replayed is True and replayed_job.id == allowed_job.id

    async def offline_poll(*_args: Any, **_kwargs: Any) -> ProviderJob:
        return ProviderJob(
            "offline-provider-job",
            "COMPLETED",
            progress=1,
            output_url="https://provider.invalid/offline-canary.mp4",
            raw={"usage": {"cost": "0.04", "credits_used": "4"}},
        )

    async def offline_download(url: str, **kwargs: Any):  # type: ignore[no-untyped-def]
        return stage_stub_output(live, kwargs["key_prefix"], b"offline-live-canary-video")

    monkeypatch.setattr(provider, "get_job", offline_poll)
    monkeypatch.setattr(live.media, "download_provider_output_to_staging", offline_download)
    with live.database.session() as session:
        stored_job = session.get(GenerationJob, allowed_job.id)
        assert stored_job is not None
        stored_job.next_retry_at = utcnow() - timedelta(seconds=1)
    completed = await live.gateway.process(allowed_job.id)
    assert completed.status == "COMPLETED"
    assert completed.actual_cost == 0.04

    with live.database.session() as session:
        usages = list(
            session.scalars(
                select(LiveCanaryUsage).where(
                    LiveCanaryUsage.idempotency_key == f"generation:{allowed_job.id}"
                )
            )
        )
        stored_permit = session.get(LiveCanaryPermit, permit.id)
        stored_wrong = session.get(LiveCanaryPermit, wrong_permit.id)
        billing = session.scalar(
            select(ProviderBillingEvidence).where(ProviderBillingEvidence.generation_job_id == allowed_job.id)
        )
        assert len(usages) == 1
        assert usages[0].status == "SETTLED"
        assert usages[0].estimated_cost_usd == Decimal("0.100000")
        assert usages[0].actual_cost_usd == Decimal("0.040000")
        assert stored_permit is not None and stored_permit.used_requests == 1
        assert stored_wrong is not None and stored_wrong.used_requests == 0
        assert billing is not None
        assert billing.source == BillingEvidenceSource.VERIFIED_PROVIDER.value
        assert billing.actual_cost_usd == Decimal("0.040000")
        assert billing.provider_credits == Decimal("4.000000")
        assert billing.metadata_json["provider_mode"] == "live"
        assert billing.metadata_json["reported_actual_cost_ignored"] is False


@pytest.mark.asyncio
async def test_live_generation_preflight_release_reopens_same_usage_once(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    live, project_id = _live_media_container(tmp_path)
    provider = live.providers.get("openrouter")
    submit_count = 0

    async def offline_submit(
        payload: dict[str, Any],
        *,
        account_id: str,
        worker_id: str,
    ) -> ProviderSubmission:
        del payload, account_id, worker_id
        nonlocal submit_count
        submit_count += 1
        return ProviderSubmission("offline-retry-job")

    monkeypatch.setattr(provider, "generate_video", offline_submit)
    permit = live.live_canary.create(
        provider="openrouter",
        model="kwaivgi/kling-v3.0-std",
        max_requests=1,
        max_cost_usd="0.25",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="one operation across a local retry",
    )
    with live.database.session() as session:
        account = session.scalar(
            select(ProviderAccount).where(
                ProviderAccount.provider == "openrouter",
                ProviderAccount.account_identifier == "direct-api://openrouter",
            )
        )
        assert account is not None
        account.status = AccountStatus.DISABLED.value
        account_id = account.id

    request = GenerationRequest(
        project_id=project_id,
        type="video",
        provider="openrouter",
        model="kwaivgi/kling-v3.0-std",
        prompt="Retry only after a conclusively local failure",
        idempotency_key="gateway-preflight-release",
    )
    job, _ = live.gateway.create(request)
    waiting = await live.gateway.process(job.id)
    assert waiting.status == "RETRY_WAIT"
    assert waiting.error_code == "NO_ACCOUNT"
    assert submit_count == 0
    with live.database.session() as session:
        usage = session.scalar(
            select(LiveCanaryUsage).where(LiveCanaryUsage.idempotency_key == f"generation:{job.id}")
        )
        stored_permit = session.get(LiveCanaryPermit, permit.id)
        account = session.get(ProviderAccount, account_id)
        stored_job = session.get(GenerationJob, job.id)
        assert usage is not None and usage.status == "RELEASED"
        assert stored_permit is not None and stored_permit.used_requests == 0
        assert stored_permit.reserved_cost_usd == Decimal("0.000000")
        assert account is not None and stored_job is not None
        account.status = AccountStatus.READY.value
        stored_job.next_retry_at = utcnow() - timedelta(seconds=1)
        usage_id = usage.id

    submitted = await live.gateway.process(job.id)
    assert submitted.status == "SUBMITTED"
    assert submit_count == 1
    with live.database.session() as session:
        usages = list(
            session.scalars(
                select(LiveCanaryUsage).where(LiveCanaryUsage.idempotency_key == f"generation:{job.id}")
            )
        )
        stored_permit = session.get(LiveCanaryPermit, permit.id)
        assert len(usages) == 1 and usages[0].id == usage_id
        assert usages[0].status == "UNCERTAIN"
        assert stored_permit is not None and stored_permit.used_requests == 1


@pytest.mark.asyncio
async def test_live_generation_failure_after_boundary_stays_uncertain(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    live, project_id = _live_media_container(tmp_path)
    provider = live.providers.get("openrouter")
    submit_count = 0

    async def uncertain_submit(
        payload: dict[str, Any],
        *,
        account_id: str,
        worker_id: str,
    ) -> ProviderSubmission:
        del payload, account_id, worker_id
        nonlocal submit_count
        submit_count += 1
        raise ProviderError(
            "offline fixture lost the response after dispatch",
            RetryCategory.TRANSIENT_NETWORK,
            code="OFFLINE_UNCERTAIN_SEND",
            submitted=True,
        )

    monkeypatch.setattr(provider, "generate_video", uncertain_submit)
    permit = live.live_canary.create(
        provider="openrouter",
        model="kwaivgi/kling-v3.0-std",
        max_requests=1,
        max_cost_usd="0.20",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="one ambiguous provider dispatch",
    )
    request = GenerationRequest(
        project_id=project_id,
        type="video",
        provider="openrouter",
        model="kwaivgi/kling-v3.0-std",
        prompt="Fail closed after the transport boundary",
        idempotency_key="gateway-uncertain-after-send",
    )
    job, _ = live.gateway.create(request)
    uncertain = await live.gateway.process(job.id)
    assert uncertain.status == "WORKER_NEEDS_USER_ACTION"
    assert uncertain.submission_state == "SENT_UNCONFIRMED"
    assert submit_count == 1

    replay = await live.gateway.process(job.id)
    assert replay.status == "WORKER_NEEDS_USER_ACTION"
    assert submit_count == 1
    with live.database.session() as session:
        usages = list(
            session.scalars(
                select(LiveCanaryUsage).where(LiveCanaryUsage.idempotency_key == f"generation:{job.id}")
            )
        )
        stored_permit = session.get(LiveCanaryPermit, permit.id)
        assert len(usages) == 1 and usages[0].status == "UNCERTAIN"
        assert stored_permit is not None and stored_permit.used_requests == 1


@pytest.mark.asyncio
async def test_live_model_role_requires_and_consumes_matching_canary(container, project) -> None:  # type: ignore[no-untyped-def]
    resolver = WorkspaceModelResolver(container.database, container.model_infrastructure)
    selected = resolver.resolve(project.id, "MULTIMODAL_EMBEDDING")
    with container.database.session() as session:
        definition = session.get(ModelDefinition, selected.definition_id)
        assert definition is not None
        definition.live_enabled = True
    capability = _FixtureEmbeddingCapability()
    providers = ProviderCapabilityCatalog()
    providers.register(
        selected.provider,
        capability,
        {ProviderCapability.EMBEDDINGS.value},
    )
    service = LiveCanaryPermitService(container.database)
    runtime = ModelRoleRuntime(
        container.database,
        resolver,
        providers,
        provider_mode="live",
        live_canary=service,
    )

    with pytest.raises(LiveCanaryDenied, match="no active live canary permit"):
        await runtime.execute_embeddings(project.id, inputs="one bounded embedding")
    assert capability.call_count == 0

    service.create(
        provider=selected.provider,
        model=selected.provider_model_id,
        max_requests=1,
        max_cost_usd="0.10",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="one role-runtime call",
    )
    execution = await runtime.execute_embeddings(
        project.id,
        inputs="one bounded embedding",
        parameters={"estimated_cost_usd": "0.01"},
    )
    assert execution.response["data"]
    assert capability.call_count == 1
    with container.database.session() as session:
        usage = session.scalar(select(LiveCanaryUsage))
        assert usage is not None
        assert usage.status == "SETTLED"
        assert usage.estimated_cost_usd == Decimal("0.010000")
        assert usage.actual_cost_usd == Decimal("0.020000")
        execution_record = session.get(ModelExecutionRecord, execution.execution_record_id)
        assert execution_record is not None
        assert execution_record.cost_source == "VERIFIED_PROVIDER"
        assert execution_record.actual_cost_usd == Decimal("0.020000")
        assert execution_record.metadata_json["provider_mode"] == "live"
        assert execution_record.metadata_json["reported_actual_cost_ignored"] is False

    with pytest.raises(LiveCanaryDenied, match="request limit"):
        await runtime.execute_embeddings(
            project.id,
            inputs="a second call must hard stop",
            parameters={"estimated_cost_usd": "0.01"},
        )
    assert capability.call_count == 1


def _compile_timeline_fixture(container, project, *, shots: int = 2) -> list[str]:  # type: ignore[no-untyped-def]
    beats = [
        "LinJin raises the phone.",
        "LinJin turns toward ZhaoKai.",
        "LinJin walks toward ZhaoKai.",
        "LinJin puts down the phone.",
    ][:shots]
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="Formal timeline transitions",
            episode_number=20,
            script_source="INT. ROOM - NIGHT\n" + "\n".join(beats),
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    return container.narrative.compile_episode(episode_id).shot_ids


def test_timeline_transition_continuous(container, project) -> None:  # type: ignore[no-untyped-def]
    first_id, second_id = _compile_timeline_fixture(container, project)
    authoritative = {
        "scene": {"location": "ROOM", "time": "night"},
        "characters": {"lin": {"position": "left", "mood": "alert"}},
        "costume": {"lin": "blue jacket"},
        "held_props": {"lin": {"right_hand": "phone"}},
    }
    with container.database.session() as session:
        first = session.get(Shot, first_id)
        assert first is not None and first.output_state_id is not None
        first.status = ShotStatus.COMMITTED.value
        first_output = session.get(TimelineState, first.output_state_id)
        assert first_output is not None
        first_output.state_json = authoritative

    propagation = AuthoritativeTimelineStateEngine(container.database).propagate_shot(first_id)

    assert propagation.propagated is True
    assert propagation.transition_type == TimelineTransitionType.CONTINUOUS.value
    assert propagation.reconciliation_required is False
    assert propagation.reason_code == "CONTINUOUS_TIMELINE"
    with container.database.session() as session:
        second = session.get(Shot, second_id)
        assert second is not None and second.input_state_id is not None
        second_input = session.get(TimelineState, second.input_state_id)
        transition = session.scalar(
            select(TimelineTransition).where(TimelineTransition.target_shot_id == second_id)
        )
        assert second_input is not None and second_input.state_json == authoritative
        assert transition is not None
        assert transition.transition_type == TimelineTransitionType.CONTINUOUS.value


def test_timeline_transition_time_jump(container, project) -> None:  # type: ignore[no-untyped-def]
    first_id, second_id = _compile_timeline_fixture(container, project)
    engine = AuthoritativeTimelineStateEngine(container.database)
    engine.set_transition(second_id, TimelineTransitionType.TIME_JUMP)
    with container.database.session() as session:
        first = session.get(Shot, first_id)
        second = session.get(Shot, second_id)
        assert first is not None and first.output_state_id is not None
        assert second is not None and second.input_state_id is not None and second.output_state_id is not None
        first.status = ShotStatus.COMMITTED.value
        first_output = session.get(TimelineState, first.output_state_id)
        second_input = session.get(TimelineState, second.input_state_id)
        second_output = session.get(TimelineState, second.output_state_id)
        assert first_output is not None and second_input is not None and second_output is not None
        first_output.state_json = {"scene": {"time": "night"}, "facts": ["before jump"]}
        original_input = dict(second_input.state_json)
        original_output = dict(second_output.state_json)

    propagation = engine.propagate_shot(first_id)

    assert propagation.propagated is False
    assert propagation.transition_type == TimelineTransitionType.TIME_JUMP.value
    assert propagation.reconciliation_required is True
    assert propagation.reason_code == "TIME_JUMP_RECONCILIATION_REQUIRED"
    with container.database.session() as session:
        second = session.get(Shot, second_id)
        assert second is not None and second.input_state_id is not None and second.output_state_id is not None
        assert session.get(TimelineState, second.input_state_id).state_json == original_input
        assert session.get(TimelineState, second.output_state_id).state_json == original_output
        assert second.downstream_state_stale is True
        assert second.stale_reason == "RECOMPUTE_REQUIRED"


def test_downstream_state_marked_stale_after_edit(container, project) -> None:  # type: ignore[no-untyped-def]
    first_id, second_id, third_id, fourth_id = _compile_timeline_fixture(
        container,
        project,
        shots=4,
    )
    with container.database.session() as session:
        first = session.get(Shot, first_id)
        third = session.get(Shot, third_id)
        assert first is not None and third is not None and third.output_state_id is not None
        first.status = ShotStatus.COMMITTED.value
        third.status = ShotStatus.COMMITTED.value
        third_output = session.get(TimelineState, third.output_state_id)
        assert third_output is not None
        committed_output_before = dict(third_output.state_json)

    engine = AuthoritativeTimelineStateEngine(container.database)
    stale = engine.mark_downstream_stale_after_edit(first_id)

    assert stale.marked_shot_ids == (second_id, third_id, fourth_id)
    assert stale.planning_shot_ids == (second_id, fourth_id)
    assert stale.immutable_shot_ids == (third_id,)
    with container.database.session() as session:
        for shot_id in (second_id, third_id, fourth_id):
            shot = session.get(Shot, shot_id)
            assert shot is not None
            assert shot.downstream_state_stale is True
            assert shot.stale_reason == "RECOMPUTE_REQUIRED"
            assert shot.stale_from_shot_id == first_id
        third = session.get(Shot, third_id)
        assert third is not None and third.output_state_id is not None
        assert session.get(TimelineState, third.output_state_id).state_json == committed_output_before

    recomputed = engine.recompute_downstream_planning(first_id)
    assert recomputed.recomputed_shot_ids == (second_id,)
    assert recomputed.blocked_shot_id == third_id
    assert recomputed.reason_code == "IMMUTABLE_OR_ACTIVE_SHOT_REQUIRES_REVIEW"
    with container.database.session() as session:
        third = session.get(Shot, third_id)
        assert third is not None and third.output_state_id is not None
        assert third.downstream_state_stale is True
        assert session.get(TimelineState, third.output_state_id).state_json == committed_output_before
