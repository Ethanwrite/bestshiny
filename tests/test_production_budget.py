"""The automatic production budget (2026-09-02).

A model's first live call is an operator's decision, taken by issuing a
``LiveCanaryPermit``. Once that call closes its loop the model reads
``VERIFIED_LIVE`` and ordinary traffic runs on a quote-bound, single-use spend
authorization — created in the same transaction as the credit reservation,
bound to workspace + job + provider + model — under a daily platform/provider
USD breaker. These tests hold the four rules that make that safe: the budget
is off until an operator sets a ceiling; an unverified model still needs a
permit and earns verification by closing its loop; a verified model consumes
no permit; and the breaker refuses before any credit or money moves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from cost_core import TokenCostEngine
from entitlement_core import (
    LiveCanaryDenied,
    LiveCanaryPermitService,
    LiveSpendDenied,
    ModelRoleRuntime,
    ProductionBudgetExceeded,
    ProductionBudgetPolicy,
    ProductionBudgetService,
    SpendAuthorizationConflict,
    WorkspaceModelResolver,
)
from entitlement_core.production_budget import (
    FENCE_CANARY,
    FENCE_PRODUCTION,
    PLATFORM_SCOPE,
    PLATFORM_SCOPE_KEY,
    PROVIDER_SCOPE,
    RELEASE_NO_REMOTE_CHARGE,
    SETTLE_ACTUAL_COST,
    SOURCE_ESTIMATED_QUOTE,
    SOURCE_TOKENS_LIST,
    SOURCE_VERIFIED_PROVIDER,
)
from fastapi.testclient import TestClient
from model_registry_core import VERIFIED_LIVE, ModelRole, production_serviceable
from platform_contracts import GenerationRequest
from platform_shared import Settings
from production_domain.models import (
    AccountStatus,
    GenerationEvent,
    GenerationJob,
    GenerationSpendAuthorization,
    LiveCanaryPermit,
    LiveCanaryUsage,
    ModelDefinition,
    ModelExecutionRecord,
    ModelPricingProfile,
    ProductionBudgetLedger,
    Project,
    ProviderAccount,
    RetryCategory,
    User,
    Workspace,
    WorkspaceCreditEntry,
    utcnow,
)
from provider_sdk import (
    LIVE_PROVIDER_CONFIRMATION,
    ChatCapability,
    ProviderCapability,
    ProviderCapabilityCatalog,
    ProviderError,
    ProviderJob,
    ProviderSubmission,
    ProviderTrustLevel,
)
from sqlalchemy import select
from video_platform_api.container import build_container
from video_platform_api.main import create_app

PROVIDER = "openrouter"
MODEL = "kwaivgi/kling-v3.0-std"
QUOTE_USD = 0.08
QUOTE_CREDITS = 8
USD_PER_CNY = Decimal("0.14743")


# ------------------------------------------------------------------ helpers


def _policy(platform: str, providers: dict[str, str] | None = None) -> ProductionBudgetPolicy:
    return ProductionBudgetPolicy(
        platform_limit_usd=Decimal(platform),
        provider_limits_usd={name: Decimal(value) for name, value in (providers or {}).items()},
    )


def _authorizations(container, **filters):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        statement = select(GenerationSpendAuthorization)
        for name, value in filters.items():
            statement = statement.where(getattr(GenerationSpendAuthorization, name) == value)
        return list(session.scalars(statement.order_by(GenerationSpendAuthorization.created_at)))


def _ledger(container, scope: str, scope_key: str) -> ProductionBudgetLedger:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        rows = list(
            session.scalars(
                select(ProductionBudgetLedger).where(
                    ProductionBudgetLedger.scope == scope,
                    ProductionBudgetLedger.scope_key == scope_key,
                )
            )
        )
        assert len(rows) == 1, [(row.scope, row.scope_key, row.window_start) for row in rows]
        session.expunge(rows[0])
        return rows[0]


def _canary_usages(container, job_id: str) -> list[LiveCanaryUsage]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        return list(
            session.scalars(
                select(LiveCanaryUsage).where(LiveCanaryUsage.idempotency_key == f"generation:{job_id}")
            )
        )


def _model_status(container) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        row = session.scalar(
            select(ModelDefinition).where(
                ModelDefinition.provider == PROVIDER,
                ModelDefinition.provider_model_id == MODEL,
            )
        )
        assert row is not None
        return row.live_canary_status


def _mark_verified(container) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        row = session.scalar(
            select(ModelDefinition).where(
                ModelDefinition.provider == PROVIDER,
                ModelDefinition.provider_model_id == MODEL,
            )
        )
        assert row is not None
        row.live_canary_status = VERIFIED_LIVE


def _live_container(tmp_path, *, platform_usd: str, provider_usd: str = ""):  # type: ignore[no-untyped-def]
    live = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'production-budget.db'}",
            storage_root=tmp_path / "live-media",
            deployment_environment="test",
            auth_required=False,
            platform_api_key="test-platform-key",
            provider_mode="live",
            allow_live_provider_calls=True,
            live_provider_confirmation=LIVE_PROVIDER_CONFIRMATION,
            openrouter_api_key="offline-placeholder-never-sent",
            public_base_url="https://media.invalid",
            local_reference_signing_key="live-canary-reference-key",
            production_budget_platform_usd_per_day=Decimal(platform_usd),
            production_budget_provider_usd_per_day=provider_usd,
        )
    )
    live.model_infrastructure.configure_runtime_model(
        "kling-3-standard-openrouter",
        MODEL,
        enabled=True,
        live_enabled=True,
    )
    with live.database.session() as session:
        user = User(email="budget@example.com", display_name="Budget")
        session.add(user)
        session.flush()
        workspace = Workspace(
            owner_user_id=user.id,
            name="Budget workspace",
            status="ACTIVE",
            plan_tier="PRO",
            credit_balance=1000,
        )
        session.add(workspace)
        session.flush()
        project = Project(workspace_id=workspace.id, title="Production budget", status="ACTIVE")
        session.add(project)
        session.flush()
        project_id = project.id
        workspace_id = workspace.id
    return live, project_id, workspace_id


def _request(project_id: str, key: str) -> GenerationRequest:
    return GenerationRequest(
        project_id=project_id,
        type="video",
        provider=PROVIDER,
        model=MODEL,
        prompt="One quote-bound live generation",
        idempotency_key=key,
    )


def _create(live, project_id: str, key: str):  # type: ignore[no-untyped-def]
    job, _ = live.gateway.create(
        _request(project_id, key),
        estimated_credits=QUOTE_CREDITS,
        pricing_version="test-pricing",
        quoted_cost_usd=QUOTE_USD,
    )
    return job


def _due(live, job_id: str) -> None:  # type: ignore[no-untyped-def]
    with live.database.session() as session:
        job = session.get(GenerationJob, job_id)
        assert job is not None
        job.next_retry_at = utcnow() - timedelta(seconds=1)


def _stub_happy_provider(live, monkeypatch, stage_stub_output, *, cost: str | None = "0.04"):  # type: ignore[no-untyped-def]
    provider = live.providers.get(PROVIDER)
    calls = {"submit": 0}

    async def offline_submit(
        payload: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del payload, account_id, worker_id
        calls["submit"] += 1
        return ProviderSubmission(f"offline-provider-job-{calls['submit']}")

    async def offline_poll(job_id: str, *_args: Any, **_kwargs: Any) -> ProviderJob:
        return ProviderJob(
            job_id,
            "COMPLETED",
            progress=1,
            output_url="https://provider.invalid/offline-budget.mp4",
            raw={"usage": {"cost": cost}} if cost is not None else {},
        )

    async def offline_download(url: str, **kwargs: Any):  # type: ignore[no-untyped-def]
        return stage_stub_output(live, kwargs["key_prefix"], b"offline-budget-video-" + url.encode())

    monkeypatch.setattr(provider, "generate_video", offline_submit)
    monkeypatch.setattr(provider, "get_job", offline_poll)
    monkeypatch.setattr(live.media, "download_provider_output_to_staging", offline_download)
    return calls


def _internal_headers(container) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {"Authorization": f"Bearer {container.settings.platform_api_key}"}


# ------------------------------------------------------------------- policy


def test_budget_is_off_by_default_and_provider_ceilings_never_exceed_the_platform() -> None:
    assert ProductionBudgetPolicy.from_settings(Settings(_env_file=None)).enabled is False
    policy = ProductionBudgetPolicy.from_settings(
        Settings(
            _env_file=None,
            production_budget_platform_usd_per_day=Decimal("50"),
            production_budget_provider_usd_per_day="seedance=20, openrouter=80",
        )
    )
    assert policy.enabled is True
    assert policy.provider_limit("seedance") == Decimal("20.000000")
    # A provider ceiling above the platform's is the platform's.
    assert policy.provider_limit("openrouter") == Decimal("50.000000")
    # A provider without its own ceiling shares the platform's.
    assert policy.provider_limit("wan") == Decimal("50.000000")
    with pytest.raises(ValueError, match="provider=usd"):
        ProductionBudgetPolicy.parse_provider_limits("seedance")
    with pytest.raises(ValueError, match="no provider"):
        ProductionBudgetPolicy.parse_provider_limits("=3")


def test_production_serviceable_is_the_model_switches_and_never_the_verdict() -> None:
    base = dict(enabled=True, live_enabled=True, lifecycle_status="CONFIGURED")
    # A model nobody has canaried yet is serviceable: the user's credits are
    # the gate, and the verdict is evidence, not permission.
    assert production_serviceable(**base) is True
    assert production_serviceable(**{**base, "lifecycle_status": "LIVE"}) is True
    assert production_serviceable(**{**base, "enabled": False}) is False
    assert production_serviceable(**{**base, "live_enabled": False}) is False
    assert production_serviceable(**{**base, "lifecycle_status": "BLOCKED"}) is False
    assert production_serviceable(**{**base, "lifecycle_status": "DISABLED"}) is False


# ------------------------------------------------------------------ service


def test_breaker_reserves_platform_and_provider_rows_atomically(container) -> None:  # type: ignore[no-untyped-def]
    service = ProductionBudgetService(container.database, _policy("1.00", {"seedance": "0.50"}))

    first = service.authorize_operation(
        operation_key="op-1", provider="seedance", model="m", max_cost_usd="0.30"
    )
    assert first.status == "RESERVED" and first.reserved_cost_usd == Decimal("0.300000")
    # The provider row trips first: 0.30 held + 0.30 asked > 0.50.
    with pytest.raises(ProductionBudgetExceeded, match="provider seedance"):
        service.authorize_operation(operation_key="op-2", provider="seedance", model="m", max_cost_usd="0.30")
    # Nothing was left held by the refusal.
    assert _ledger(container, PROVIDER_SCOPE, "seedance").reserved_usd == Decimal("0.300000")
    assert _ledger(container, PLATFORM_SCOPE, PLATFORM_SCOPE_KEY).reserved_usd == Decimal("0.300000")
    # Another provider fills the platform row: 0.30 + 0.60 = 0.90 of 1.00.
    service.authorize_operation(operation_key="op-3", provider="wan", model="w", max_cost_usd="0.60")
    with pytest.raises(ProductionBudgetExceeded, match="platform"):
        service.authorize_operation(operation_key="op-4", provider="wan", model="w", max_cost_usd="0.20")
    # A replay of an existing key is not a second reservation.
    replay = service.authorize_operation(
        operation_key="op-1", provider="seedance", model="m", max_cost_usd="0.30"
    )
    assert replay.replayed is True and replay.id == first.id
    with pytest.raises(SpendAuthorizationConflict):
        service.authorize_operation(operation_key="op-1", provider="seedance", model="m", max_cost_usd="0.31")

    # Settling below the hold hands the difference back on both rows.
    settled = service.settle(
        first.id, actual_cost_usd="0.10", evidence_reference="invoice:1", source=SOURCE_VERIFIED_PROVIDER
    )
    assert settled.status == "SETTLED" and settled.actual_cost_usd == Decimal("0.100000")
    platform = _ledger(container, PLATFORM_SCOPE, PLATFORM_SCOPE_KEY)
    assert (platform.reserved_usd, platform.actual_usd) == (Decimal("0.600000"), Decimal("0.100000"))
    seedance = _ledger(container, PROVIDER_SCOPE, "seedance")
    assert (seedance.reserved_usd, seedance.actual_usd) == (Decimal("0.000000"), Decimal("0.100000"))
    # 0.60 held + 0.10 spent + 0.30 asked = 1.00: exactly at the ceiling is allowed.
    service.authorize_operation(operation_key="op-5", provider="seedance", model="m", max_cost_usd="0.30")
    snapshot = service.snapshot()
    windows = {(item["scope"], item["scope_key"]): item for item in snapshot["windows"]}
    assert windows[("PLATFORM", "platform")]["remaining_usd"] == "0.000000"
    assert windows[("PLATFORM", "platform")]["tripped"] is True
    assert snapshot["policy"]["provider_limits_usd"] == {"seedance": "0.500000"}


def test_settle_at_quote_release_and_window_rollover(container) -> None:  # type: ignore[no-untyped-def]
    moment = datetime(2026, 9, 2, 23, 50, tzinfo=UTC)
    service = ProductionBudgetService(container.database, _policy("0.50"), clock=lambda: moment)

    held = service.authorize_operation(
        operation_key="day1-a", provider="seedance", model="m", max_cost_usd="0.50"
    )
    with pytest.raises(ProductionBudgetExceeded):
        service.authorize_operation(
            operation_key="day1-b", provider="seedance", model="m", max_cost_usd="0.01"
        )
    # A conclusively local failure hands the whole hold back.
    released = service.release_pre_boundary(held.id, evidence_reference="no-account")
    assert released.status == "RELEASED" and released.reserved_cost_usd == Decimal("0.000000")
    assert _ledger(container, PLATFORM_SCOPE, PLATFORM_SCOPE_KEY).reserved_usd == Decimal("0.000000")
    # ...and re-preparing the paid boundary re-reserves it against the current window.
    reopened = service.prepare_boundary(
        held.id, provider="seedance", model="m", fence=FENCE_PRODUCTION, evidence_reference="retry"
    )
    assert reopened.status == "UNCERTAIN" and reopened.reserved_cost_usd == Decimal("0.500000")
    # No provider figure: settle at the quote, and say so.
    at_quote = service.settle(
        held.id, actual_cost_usd=None, evidence_reference="poll", source=SOURCE_ESTIMATED_QUOTE
    )
    assert at_quote.actual_cost_usd == Decimal("0.500000")
    assert at_quote.settlement_source == SOURCE_ESTIMATED_QUOTE
    assert at_quote.overran_quote is False

    # The next UTC day is a fresh window; yesterday's spend stays on yesterday's row.
    moment = datetime(2026, 9, 3, 0, 5, tzinfo=UTC)
    fresh = service.authorize_operation(
        operation_key="day2-a", provider="seedance", model="m", max_cost_usd="0.50"
    )
    assert fresh.status == "RESERVED"
    with container.database.session() as session:
        rows = list(
            session.scalars(
                select(ProductionBudgetLedger).where(ProductionBudgetLedger.scope == PLATFORM_SCOPE)
            )
        )
    by_day = {row.window_start.strftime("%Y-%m-%d"): row for row in rows}
    assert by_day["2026-09-02"].actual_usd == Decimal("0.500000")
    assert by_day["2026-09-03"].reserved_usd == Decimal("0.500000")
    assert by_day["2026-09-03"].actual_usd == Decimal("0.000000")


def test_reconciliation_is_audited_and_idempotent(container) -> None:  # type: ignore[no-untyped-def]
    service = ProductionBudgetService(container.database, _policy("1.00"))
    held = service.authorize_operation(
        operation_key="rec-1", provider="seedance", model="m", max_cost_usd="0.20"
    )
    with pytest.raises(SpendAuthorizationConflict, match="requires UNCERTAIN"):
        service.reconcile_uncertain(
            held.id,
            action=SETTLE_ACTUAL_COST,
            actual_cost_usd="0.05",
            idempotency_key="rec-key-early",
            reason="too early",
            evidence_reference="console:none",
        )
    service.prepare_boundary(
        held.id, provider="seedance", model="m", fence=FENCE_PRODUCTION, evidence_reference="b"
    )
    settled, audit_id, replayed = service.reconcile_uncertain(
        held.id,
        action=SETTLE_ACTUAL_COST,
        actual_cost_usd="0.05",
        idempotency_key="rec-key-0001",
        reason="provider console shows one billed task",
        evidence_reference="ark-console:task-1",
    )
    assert replayed is False and settled.status == "SETTLED"
    assert settled.actual_cost_usd == Decimal("0.050000")
    again, again_audit, again_replayed = service.reconcile_uncertain(
        held.id,
        action=SETTLE_ACTUAL_COST,
        actual_cost_usd="0.05",
        idempotency_key="rec-key-0001",
        reason="provider console shows one billed task",
        evidence_reference="ark-console:task-1",
    )
    assert again_replayed is True and again_audit == audit_id and again.id == settled.id
    with pytest.raises(SpendAuthorizationConflict, match="different"):
        service.reconcile_uncertain(
            held.id,
            action=RELEASE_NO_REMOTE_CHARGE,
            actual_cost_usd=None,
            idempotency_key="rec-key-0001",
            reason="same key, other finding",
            evidence_reference="ark-console:task-1",
        )
    platform = _ledger(container, PLATFORM_SCOPE, PLATFORM_SCOPE_KEY)
    assert (platform.reserved_usd, platform.actual_usd) == (Decimal("0.000000"), Decimal("0.050000"))


# ------------------------------------------------------------------ gateway


@pytest.mark.asyncio
async def test_verified_model_runs_on_the_budget_and_never_touches_a_permit(
    tmp_path, monkeypatch, stage_stub_output
) -> None:  # type: ignore[no-untyped-def]
    live, project_id, workspace_id = _live_container(
        tmp_path, platform_usd="1.00", provider_usd="openrouter=0.50"
    )
    _mark_verified(live)
    calls = _stub_happy_provider(live, monkeypatch, stage_stub_output)

    job = _create(live, project_id, "budget-verified")
    held = _authorizations(live, generation_job_id=job.id)
    assert len(held) == 1
    assert held[0].status == "RESERVED" and held[0].fence == "PENDING"
    assert held[0].workspace_id == workspace_id and held[0].project_id == project_id
    assert (held[0].provider, held[0].model) == (PROVIDER, MODEL)
    assert held[0].max_cost_usd == Decimal("0.080000")
    assert held[0].quoted_credits == QUOTE_CREDITS
    # Credits and the authorization were taken together.
    with live.database.session() as session:
        credit = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        assert credit is not None and credit.status == "RESERVED" and credit.credits == QUOTE_CREDITS
        events = {
            row.event_type
            for row in session.scalars(
                select(GenerationEvent).where(GenerationEvent.generation_job_id == job.id)
            )
        }
    assert {"CREDIT_RESERVED", "SPEND_AUTHORIZED"} <= events
    assert _ledger(live, PLATFORM_SCOPE, PLATFORM_SCOPE_KEY).reserved_usd == Decimal("0.080000")
    assert _ledger(live, PROVIDER_SCOPE, PROVIDER).reserved_usd == Decimal("0.080000")

    # No permit exists anywhere, and none is needed.
    submitted = await live.gateway.process(job.id)
    assert submitted.status == "SUBMITTED", (submitted.error_code, submitted.error_message)
    assert calls["submit"] == 1
    assert _canary_usages(live, job.id) == []
    prepared = _authorizations(live, generation_job_id=job.id)[0]
    assert prepared.status == "UNCERTAIN" and prepared.fence == FENCE_PRODUCTION

    _due(live, job.id)
    completed = await live.gateway.process(job.id)
    assert completed.status == "COMPLETED"
    settled = _authorizations(live, generation_job_id=job.id)[0]
    assert settled.status == "SETTLED"
    assert settled.actual_cost_usd == Decimal("0.040000")
    assert settled.settlement_source == SOURCE_VERIFIED_PROVIDER
    platform = _ledger(live, PLATFORM_SCOPE, PLATFORM_SCOPE_KEY)
    assert (platform.reserved_usd, platform.actual_usd) == (Decimal("0.000000"), Decimal("0.040000"))
    provider_row = _ledger(live, PROVIDER_SCOPE, PROVIDER)
    assert (provider_row.reserved_usd, provider_row.actual_usd) == (Decimal("0.000000"), Decimal("0.040000"))
    with live.database.session() as session:
        credit = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
        )
        assert credit is not None and credit.status == "SETTLED"
        assert session.scalar(select(LiveCanaryPermit)) is None


@pytest.mark.asyncio
async def test_uncanaried_model_runs_on_credits_and_the_loop_records_the_verdict(
    tmp_path, monkeypatch, stage_stub_output
) -> None:  # type: ignore[no-untyped-def]
    """No permit anywhere, a model nobody has canaried: the user's generation runs.

    The verdict is written by the closed loop as evidence for lifecycle and
    routing; it was never what let the job through.
    """

    live, project_id, _workspace_id = _live_container(tmp_path, platform_usd="1.00")
    calls = _stub_happy_provider(live, monkeypatch, stage_stub_output)
    assert _model_status(live) == "NOT_RUN"

    job = _create(live, project_id, "budget-uncanaried")
    submitted = await live.gateway.process(job.id)
    assert submitted.status == "SUBMITTED", (submitted.error_code, submitted.error_message)
    assert calls["submit"] == 1
    assert _canary_usages(live, job.id) == []
    prepared = _authorizations(live, generation_job_id=job.id)[0]
    assert prepared.status == "UNCERTAIN" and prepared.fence == FENCE_PRODUCTION

    _due(live, job.id)
    completed = await live.gateway.process(job.id)
    assert completed.status == "COMPLETED"
    assert _authorizations(live, generation_job_id=job.id)[0].status == "SETTLED"
    assert _model_status(live) == VERIFIED_LIVE
    with live.database.session() as session:
        events = {
            row.event_type
            for row in session.scalars(
                select(GenerationEvent).where(GenerationEvent.generation_job_id == job.id)
            )
        }
        assert session.scalar(select(LiveCanaryPermit)) is None
    assert "LIVE_CANARY_VERDICT_RECORDED" in events


@pytest.mark.asyncio
async def test_budget_off_still_needs_the_permit(tmp_path, monkeypatch, stage_stub_output) -> None:  # type: ignore[no-untyped-def]
    """With no ceiling set the old rule stands: every live call waits for a permit."""

    live, project_id, _workspace_id = _live_container(tmp_path, platform_usd="0")
    calls = _stub_happy_provider(live, monkeypatch, stage_stub_output)
    job = _create(live, project_id, "budget-off-permit")
    denied = await live.gateway.process(job.id)
    assert denied.status == "RETRY_WAIT" and denied.error_code == "LIVE_CANARY_DENIED"
    assert denied.retry_category == RetryCategory.RATE_LIMIT.value
    assert calls["submit"] == 0
    assert _authorizations(live, generation_job_id=job.id) == []

    live.live_canary.create(
        provider=PROVIDER,
        model=MODEL,
        max_requests=1,
        max_cost_usd="0.10",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="the permit is the fence while the budget is off",
    )
    _due(live, job.id)
    submitted = await live.gateway.process(job.id)
    assert submitted.status == "SUBMITTED", (submitted.error_code, submitted.error_message)
    usages = _canary_usages(live, job.id)
    assert len(usages) == 1 and usages[0].status == "UNCERTAIN"


@pytest.mark.asyncio
async def test_generation_without_a_provider_figure_settles_at_the_quote(
    tmp_path, monkeypatch, stage_stub_output
) -> None:  # type: ignore[no-untyped-def]
    live, project_id, _workspace_id = _live_container(tmp_path, platform_usd="1.00")
    _mark_verified(live)
    _stub_happy_provider(live, monkeypatch, stage_stub_output, cost=None)
    job = _create(live, project_id, "budget-no-figure")
    assert (await live.gateway.process(job.id)).status == "SUBMITTED"
    _due(live, job.id)
    assert (await live.gateway.process(job.id)).status == "COMPLETED"
    settled = _authorizations(live, generation_job_id=job.id)[0]
    assert settled.status == "SETTLED"
    assert settled.actual_cost_usd == Decimal("0.080000")
    assert settled.settlement_source == SOURCE_ESTIMATED_QUOTE
    assert _ledger(live, PLATFORM_SCOPE, PLATFORM_SCOPE_KEY).actual_usd == Decimal("0.080000")


def test_tripped_breaker_refuses_the_job_before_credits_move(tmp_path) -> None:  # type: ignore[no-untyped-def]
    live, project_id, workspace_id = _live_container(tmp_path, platform_usd="0.05")
    _mark_verified(live)
    with pytest.raises(ProductionBudgetExceeded, match="platform"):
        _create(live, project_id, "budget-tripped")
    with live.database.session() as session:
        assert session.scalar(select(GenerationJob)) is None
        assert session.scalar(select(WorkspaceCreditEntry)) is None
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None and workspace.credit_balance == 1000
    assert _authorizations(live) == []


def test_live_job_without_a_server_quote_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    live, project_id, _workspace_id = _live_container(tmp_path, platform_usd="1.00")
    with pytest.raises(LiveSpendDenied, match="server-owned USD quote"):
        live.gateway.create(
            _request(project_id, "budget-no-quote"),
            estimated_credits=QUOTE_CREDITS,
            pricing_version="test-pricing",
        )
    with live.database.session() as session:
        assert session.scalar(select(GenerationJob)) is None


@pytest.mark.asyncio
async def test_local_failure_releases_the_authorization_and_a_retry_reserves_again(
    tmp_path, monkeypatch, stage_stub_output
) -> None:  # type: ignore[no-untyped-def]
    live, project_id, _workspace_id = _live_container(tmp_path, platform_usd="1.00")
    _mark_verified(live)
    calls = _stub_happy_provider(live, monkeypatch, stage_stub_output)
    with live.database.session() as session:
        account = session.scalar(
            select(ProviderAccount).where(
                ProviderAccount.provider == PROVIDER,
                ProviderAccount.account_identifier == f"direct-api://{PROVIDER}",
            )
        )
        assert account is not None
        account.status = AccountStatus.DISABLED.value
        account_id = account.id

    job = _create(live, project_id, "budget-local-failure")
    waiting = await live.gateway.process(job.id)
    assert waiting.status == "RETRY_WAIT" and waiting.error_code == "NO_ACCOUNT"
    assert calls["submit"] == 0
    released = _authorizations(live, generation_job_id=job.id)[0]
    assert released.status == "RELEASED" and released.reserved_cost_usd == Decimal("0.000000")
    assert _ledger(live, PLATFORM_SCOPE, PLATFORM_SCOPE_KEY).reserved_usd == Decimal("0.000000")

    with live.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        assert account is not None
        account.status = AccountStatus.READY.value
    _due(live, job.id)
    submitted = await live.gateway.process(job.id)
    assert submitted.status == "SUBMITTED", (submitted.error_code, submitted.error_message)
    reopened = _authorizations(live, generation_job_id=job.id)
    assert len(reopened) == 1 and reopened[0].status == "UNCERTAIN"
    assert reopened[0].reserved_cost_usd == Decimal("0.080000")
    assert _ledger(live, PLATFORM_SCOPE, PLATFORM_SCOPE_KEY).reserved_usd == Decimal("0.080000")


@pytest.mark.asyncio
async def test_ambiguous_failure_stays_uncertain_until_an_operator_reconciles_it(
    tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    live, project_id, _workspace_id = _live_container(tmp_path, platform_usd="1.00")
    _mark_verified(live)
    provider = live.providers.get(PROVIDER)

    async def uncertain_submit(
        payload: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del payload, account_id, worker_id
        raise ProviderError(
            "offline fixture lost the response after dispatch",
            RetryCategory.TRANSIENT_NETWORK,
            code="OFFLINE_UNCERTAIN_SEND",
            submitted=True,
        )

    monkeypatch.setattr(provider, "generate_video", uncertain_submit)
    job = _create(live, project_id, "budget-uncertain")
    uncertain = await live.gateway.process(job.id)
    assert uncertain.status == "WORKER_NEEDS_USER_ACTION"
    held = _authorizations(live, generation_job_id=job.id)[0]
    assert held.status == "UNCERTAIN" and held.reserved_cost_usd == Decimal("0.080000")
    assert _ledger(live, PLATFORM_SCOPE, PLATFORM_SCOPE_KEY).reserved_usd == Decimal("0.080000")

    path = f"/internal/spend-authorizations/{held.id}/reconcile"
    body = {
        "action": RELEASE_NO_REMOTE_CHARGE,
        "reason": "provider console shows no task for this dispatch",
        "evidence_reference": "openrouter-console:none",
        "explicit_confirmation": True,
    }
    headers = {**_internal_headers(live), "Idempotency-Key": "budget-reconcile-0001"}
    with TestClient(create_app(live)) as client:
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, headers=_internal_headers(live), json=body).status_code == 400
        first = client.post(path, headers=headers, json=body)
        replay = client.post(path, headers=headers, json=body)
        conflict = client.post(
            path, headers=headers, json={**body, "action": SETTLE_ACTUAL_COST, "actual_cost_usd": "0.01"}
        )
        listed = client.get(
            "/internal/spend-authorizations",
            headers=_internal_headers(live),
            params={"generation_job_id": job.id},
        )
        snapshot = client.get("/internal/production-budget", headers=_internal_headers(live))
    assert first.status_code == 200, first.text
    assert first.json()["replayed"] is False
    assert first.json()["authorization"]["status"] == "RELEASED"
    assert first.json()["authorization"]["settlement_source"] == "RECONCILED_MANUAL"
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert conflict.status_code == 409
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["authorizations"]] == [held.id]
    assert snapshot.status_code == 200
    assert snapshot.json()["policy"]["enabled"] is True
    platform = next(item for item in snapshot.json()["windows"] if item["scope"] == "PLATFORM")
    assert platform["reserved_usd"] == "0.000000"


def test_budget_off_creates_no_authorization(tmp_path) -> None:  # type: ignore[no-untyped-def]
    live, project_id, _workspace_id = _live_container(tmp_path, platform_usd="0")
    assert live.production_budget.enabled is False
    job = _create(live, project_id, "budget-off")
    assert _authorizations(live, generation_job_id=job.id) == []
    with live.database.session() as session:
        assert session.scalar(select(ProductionBudgetLedger)) is None
    with TestClient(create_app(live)) as client:
        snapshot = client.get("/internal/production-budget", headers=_internal_headers(live))
    assert snapshot.status_code == 200 and snapshot.json()["policy"]["enabled"] is False


# ------------------------------------------------------------- model roles


class _FixtureChatCapability(ChatCapability):
    trust_level = ProviderTrustLevel.PRODUCTION
    configured = True

    def __init__(self) -> None:
        self.call_count = 0

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del model, messages, parameters
        self.call_count += 1
        return {
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        }


def _seed_token_pricing(container, provider: str, model: str) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        for direction, price in (("input_tokens", "0.60"), ("output_tokens", "3.60")):
            session.add(
                ModelPricingProfile(
                    provider=provider,
                    provider_model_id=model,
                    input_mode=direction,
                    resolution="",
                    currency="CNY",
                    billing_unit="1M_tokens",
                    unit_price=Decimal(price),
                    estimate_unit="1M_tokens",
                    estimate_unit_price=Decimal(price),
                    usd_per_currency=USD_PER_CNY,
                    effective_from=utcnow() - timedelta(days=1),
                    source_url="https://example.invalid/test-token-pricing",
                    source_checked_at=utcnow() - timedelta(days=1),
                )
            )


def _live_director_runtime(container, project, policy: ProductionBudgetPolicy):  # type: ignore[no-untyped-def]
    resolver = WorkspaceModelResolver(container.database, container.model_infrastructure)
    capability = _FixtureChatCapability()
    providers = ProviderCapabilityCatalog()
    selected = resolver.resolve(project.id, ModelRole.DIRECTOR)
    with container.database.session() as session:
        definition = session.get(ModelDefinition, selected.definition_id)
        assert definition is not None
        definition.live_enabled = True
    providers.register(selected.provider, capability, {ProviderCapability.CHAT.value})
    _seed_token_pricing(container, selected.provider, selected.provider_model_id)
    permits = LiveCanaryPermitService(container.database)
    budget = ProductionBudgetService(container.database, policy)
    runtime = ModelRoleRuntime(
        container.database,
        resolver,
        providers,
        provider_mode="live",
        live_canary=permits,
        token_costs=TokenCostEngine(container.database),
        production_budget=budget,
    )
    return runtime, permits, capability, selected


@pytest.mark.asyncio
async def test_director_call_runs_on_the_budget_with_no_permit_and_records_the_verdict(
    container, project
) -> None:  # type: ignore[no-untyped-def]
    runtime, _permits, capability, selected = _live_director_runtime(container, project, _policy("1.00"))
    messages = [{"role": "user", "content": "一支30秒的城市天台悬疑广告"}]

    execution = await runtime.execute_chat(project.id, ModelRole.DIRECTOR, messages=messages)
    assert capability.call_count == 1
    with container.database.session() as session:
        assert session.scalar(select(LiveCanaryPermit)) is None
        assert session.scalar(select(LiveCanaryUsage)) is None
        definition = session.get(ModelDefinition, selected.definition_id)
        assert definition is not None and definition.live_canary_status == VERIFIED_LIVE
        assert "role call settled USD 0.000354" in definition.live_canary_detail
        record = session.get(ModelExecutionRecord, execution.execution_record_id)
        assert record is not None
        assert record.metadata_json["live_fence"] == FENCE_PRODUCTION
        assert record.metadata_json["spend_authorization_id"] is not None
        assert record.cost_source == "TOKENS_LIST"
    rows = _authorizations(container)
    assert len(rows) == 1
    assert rows[0].fence == FENCE_PRODUCTION and rows[0].status == "SETTLED"
    assert rows[0].actual_cost_usd == Decimal("0.000354")
    assert rows[0].settlement_source == SOURCE_TOKENS_LIST
    assert rows[0].model_role == ModelRole.DIRECTOR.value

    await runtime.execute_chat(project.id, ModelRole.DIRECTOR, messages=messages)
    assert capability.call_count == 2
    platform = _ledger(container, PLATFORM_SCOPE, PLATFORM_SCOPE_KEY)
    assert platform.reserved_usd == Decimal("0.000000")
    assert platform.actual_usd == Decimal("0.000708")


@pytest.mark.asyncio
async def test_director_call_with_the_budget_off_still_needs_a_permit(container, project) -> None:  # type: ignore[no-untyped-def]
    runtime, permits, capability, selected = _live_director_runtime(container, project, _policy("0"))
    messages = [{"role": "user", "content": "hello"}]
    with pytest.raises(LiveCanaryDenied, match="no active live canary permit"):
        await runtime.execute_chat(project.id, ModelRole.DIRECTOR, messages=messages)
    assert capability.call_count == 0
    assert _authorizations(container) == []
    permits.create(
        provider=selected.provider,
        model=selected.provider_model_id,
        max_requests=1,
        max_cost_usd="0.10",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="the permit is the fence while the budget is off",
    )
    execution = await runtime.execute_chat(project.id, ModelRole.DIRECTOR, messages=messages)
    assert capability.call_count == 1
    with container.database.session() as session:
        usage = session.scalars(select(LiveCanaryUsage)).one()
        assert usage.status == "SETTLED"
        record = session.get(ModelExecutionRecord, execution.execution_record_id)
        assert record is not None and record.metadata_json["live_fence"] == FENCE_CANARY


@pytest.mark.asyncio
async def test_tripped_breaker_refuses_a_director_call_the_way_a_missing_permit_does(
    container, project
) -> None:  # type: ignore[no-untyped-def]
    runtime, _permits, capability, selected = _live_director_runtime(container, project, _policy("0.00001"))
    with container.database.session() as session:
        definition = session.get(ModelDefinition, selected.definition_id)
        assert definition is not None
        definition.live_canary_status = VERIFIED_LIVE
    with pytest.raises(ProductionBudgetExceeded) as refused:
        await runtime.execute_chat(
            project.id, ModelRole.DIRECTOR, messages=[{"role": "user", "content": "hello"}]
        )
    # The director's turn and prompt refinement catch this base to degrade.
    assert isinstance(refused.value, LiveSpendDenied)
    assert capability.call_count == 0
    assert _authorizations(container) == []
