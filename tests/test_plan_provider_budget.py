from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cost_core import CreditEstimate
from entitlement_core import PlanEntitlementDenied, WorkspaceModelResolver
from fastapi.testclient import TestClient
from model_registry_core import ModelRole
from openrouter_provider import OpenRouterProvider
from production_domain.models import (
    CostRecord,
    GenerationJob,
    ModelRoleBinding,
    Project,
    User,
    Workspace,
    WorkspaceCreditEntry,
)
from provider_budget_core import DatabaseProviderBudgetRepository
from provider_sdk import MockProviderTransport, ProviderBudgetConflict, ProviderBudgetExceeded
from runapi_provider import RunAPIEdgeProvider
from sqlalchemy import func, select
from video_platform_api.main import create_app


def _free_project(container) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        user = User(email="free-models@example.com", display_name="Free Models")
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


def test_free_workspace_resolves_seedance_and_denies_paid_video_roles(container):
    project_id = _free_project(container)
    resolver = WorkspaceModelResolver(container.database, container.model_infrastructure)

    selected = resolver.resolve(project_id, ModelRole.VIDEO_SEEDANCE)

    assert selected.logical_name == "seedance-2.5-official"
    assert selected.provider == "seedance"
    assert resolver.default_video_role(project_id) is ModelRole.VIDEO_SEEDANCE
    with pytest.raises(PlanEntitlementDenied):
        resolver.resolve(project_id, ModelRole.VIDEO_FLOW)


def test_unconfigured_free_doubao_fails_closed_without_openrouter_fallback(container):
    project_id = _free_project(container)
    resolver = WorkspaceModelResolver(container.database, container.model_infrastructure)
    with container.database.session() as session:
        free_director = session.scalar(
            select(ModelRoleBinding).where(
                ModelRoleBinding.role == ModelRole.DIRECTOR.value,
                ModelRoleBinding.plan_tier == "FREE",
            )
        )
        assert free_director is not None
        free_director.enabled = False

    with pytest.raises(LookupError, match="no compatible model binding"):
        resolver.resolve(project_id, ModelRole.DIRECTOR)
    with pytest.raises(LookupError, match="no compatible model binding"):
        resolver.resolve(project_id, ModelRole.MULTIMODAL_EMBEDDING)

    candidates = container.model_infrastructure.candidates_for_role(
        ModelRole.DIRECTOR,
        plan_tier="FREE",
    )
    embedding_candidates = container.model_infrastructure.candidates_for_role(
        ModelRole.MULTIMODAL_EMBEDDING,
        plan_tier="FREE",
    )
    assert candidates == []
    assert embedding_candidates == []


def test_free_prompt_refine_without_free_bindings_never_reaches_paid_transports(container):
    project_id = _free_project(container)
    with container.database.session() as session:
        free_bindings = session.scalars(
            select(ModelRoleBinding).where(
                ModelRoleBinding.role.in_(
                    {
                        ModelRole.PROMPT_REFINER_LOW_COST.value,
                        ModelRole.PROMPT_REFINER_FALLBACK.value,
                    }
                ),
                ModelRoleBinding.plan_tier == "FREE",
            )
        ).all()
        assert len(free_bindings) == 2
        for binding in free_bindings:
            binding.enabled = False

    container.model_infrastructure.configure_runtime_model(
        "runapi-prompt-refiner-edge",
        "paid-runapi-refiner",
        enabled=True,
    )
    runapi = container.providers.get("runapi")
    openrouter = container.providers.get("openrouter")
    assert isinstance(runapi, RunAPIEdgeProvider)
    assert isinstance(openrouter, OpenRouterProvider)
    assert isinstance(runapi.client.transport, MockProviderTransport)
    assert isinstance(openrouter.client.transport, MockProviderTransport)
    runapi.configured = True
    openrouter.configured = True

    with TestClient(create_app(container)) as client:
        response = client.post(
            "/v1/prompts/refine",
            json={"project_id": project_id, "prompt": "Mina raises the red phone."},
        )

    assert response.status_code == 200
    assert response.json()["model_refinement"] == {
        "accepted": False,
        "source": "local_safe_fallback",
        "reason_codes": ["PRIMARY_UNAVAILABLE", "FALLBACK_UNAVAILABLE"],
    }
    assert runapi.client.transport.requests == []
    assert openrouter.client.transport.requests == []


def test_database_provider_budget_reserve_settle_and_exhaustion(container):
    repository = DatabaseProviderBudgetRepository(container.database)
    snapshot = repository.ensure("runapi", Decimal("10"))
    assert snapshot.remaining_budget_usd == Decimal("10.000000")

    reservation = repository.reserve(
        provider="runapi",
        task_id="prompt-refinement-1",
        task_role="PROMPT_REFINER_LOW_COST",
        estimated_cost_usd=Decimal("3.25"),
    )
    replay = repository.reserve(
        provider="runapi",
        task_id="prompt-refinement-1",
        task_role="PROMPT_REFINER_LOW_COST",
        estimated_cost_usd=Decimal("3.25"),
    )
    assert replay.reservation_id == reservation.reservation_id
    assert reservation.acquired is True
    assert replay.acquired is False
    assert replay.remaining_budget_usd == Decimal("6.750000")

    settled = repository.settle(
        reservation.reservation_id,
        actual_cost_usd=Decimal("2.50"),
    )
    assert settled.status == "SETTLED"
    assert settled.actual_cost_usd == Decimal("2.500000")
    assert repository.get("runapi").remaining_budget_usd == Decimal("7.500000")

    with pytest.raises(ProviderBudgetConflict):
        repository.reserve(
            provider="runapi",
            task_id="prompt-refinement-1",
            task_role="PROMPT_REFINER_LOW_COST",
            estimated_cost_usd=Decimal("3.50"),
        )
    with pytest.raises(ProviderBudgetExceeded):
        repository.reserve(
            provider="runapi",
            task_id="too-expensive",
            task_role="PROMPT_REFINER_LOW_COST",
            estimated_cost_usd=Decimal("8"),
        )


def test_database_provider_budget_uncertain_usage_keeps_estimate_reserved(container):
    repository = DatabaseProviderBudgetRepository(container.database)
    repository.ensure("edge-uncertain", Decimal("1"))
    reservation = repository.reserve(
        provider="edge-uncertain",
        task_id="unknown-provider-cost",
        task_role="PROMPT_REFINER_LOW_COST",
        estimated_cost_usd=Decimal("0.25"),
    )

    uncertain = repository.settle(
        reservation.reservation_id,
        actual_cost_usd=None,
        status="UNCERTAIN",
    )
    replay = repository.settle(
        reservation.reservation_id,
        actual_cost_usd=None,
        status="UNCERTAIN",
    )
    snapshot = repository.get("edge-uncertain")

    assert uncertain.status == "UNCERTAIN"
    assert uncertain.actual_cost_usd is None
    assert replay.reservation_id == reservation.reservation_id
    assert snapshot.actual_cost_usd == Decimal("0.000000")
    assert snapshot.reserved_cost_usd == Decimal("0.250000")
    assert snapshot.remaining_budget_usd == Decimal("0.750000")


def test_database_provider_budget_trusted_actual_settles_after_uncertain_paid_boundary(container):
    repository = DatabaseProviderBudgetRepository(container.database)
    repository.ensure("runapi-late-actual", Decimal("10"))
    reservation = repository.reserve(
        provider="runapi-late-actual",
        task_id="trusted-late-actual",
        task_role="PROMPT_TRANSLATION",
        estimated_cost_usd=Decimal("1.25"),
    )
    uncertain = repository.settle(
        reservation.reservation_id,
        actual_cost_usd=None,
        status="UNCERTAIN",
    )

    settled = repository.settle(
        reservation.reservation_id,
        actual_cost_usd=Decimal("0.75"),
        status="SETTLED",
    )

    assert uncertain.status == "UNCERTAIN"
    assert settled.status == "SETTLED"
    assert settled.actual_cost_usd == Decimal("0.750000")
    snapshot = repository.get("runapi-late-actual")
    assert snapshot.reserved_cost_usd == Decimal("0.000000")
    assert snapshot.actual_cost_usd == Decimal("0.750000")
    assert snapshot.remaining_budget_usd == Decimal("9.250000")


def test_database_provider_budget_concurrent_reservations_cannot_overspend(container):
    repository = DatabaseProviderBudgetRepository(container.database)
    repository.ensure("edge-concurrent", Decimal("1"))

    def reserve(index: int) -> str:
        try:
            result = repository.reserve(
                provider="edge-concurrent",
                task_id=f"task-{index}",
                task_role="ASSET_CAPTION",
                estimated_cost_usd=Decimal("0.60"),
            )
            return result.status
        except ProviderBudgetExceeded:
            return "EXHAUSTED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, range(2)))

    assert sorted(results) == ["EXHAUSTED", "RESERVED"]
    snapshot = repository.get("edge-concurrent")
    assert snapshot.reserved_cost_usd == Decimal("0.600000")
    assert snapshot.remaining_budget_usd == Decimal("0.400000")


def test_free_passenger_video_is_server_routed_to_seedance_role(container, monkeypatch):
    container.settings.auth_required = True
    captured: dict[str, object] = {}
    monkeypatch.setattr(container.providers.get("seedance"), "configured", True)

    def estimate(**_kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            provider_cost_usd=0.1,
            resolution_multiplier=1.0,
            reference_multiplier=1.0,
            service_multiplier=1.2,
            estimated_total_usd=0.12,
            credits=12,
        )

    def submit(command, **_server_quote):  # type: ignore[no-untyped-def]
        captured["command"] = command
        return (
            SimpleNamespace(
                id="free-seedance-job",
                status="NEW",
                provider=command.provider,
                model=command.model,
                output_asset_id=None,
                cost_estimate=command.estimated_cost,
            ),
            False,
        )

    monkeypatch.setattr(container.credit_pricing, "estimate", estimate)
    monkeypatch.setattr(container.visual_runtime, "submit_passenger", submit)
    with TestClient(create_app(container)) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "email": "free-route@example.com",
                "password": "correct horse battery staple",
            },
        ).json()
        headers = {"Authorization": f"Bearer {registered['access_token']}"}
        project = client.post(
            "/v1/projects",
            headers=headers,
            json={"title": "Free Route"},
        ).json()
        response = client.post(
            "/api/passenger/generate",
            headers=headers,
            json={
                # Auto: no provider/model named, so the platform routes.
                "project_id": project["id"],
                "media_type": "video",
                "prompt": "A single visible action",
                "idempotency_key": "free-role-route",
            },
        )

    assert response.status_code == 202, response.text
    command = captured["command"]
    assert command.provider == "seedance"  # type: ignore[attr-defined]
    assert command.model == "doubao-seedance-2-5-260628"  # type: ignore[attr-defined]
    assert command.model_role == "VIDEO_SEEDANCE"  # type: ignore[attr-defined]


def test_free_generic_generation_cannot_bypass_seedance_with_raw_paid_target(container):
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "email": "free-bypass@example.com",
                "password": "correct horse battery staple",
            },
        ).json()
        headers = {"Authorization": f"Bearer {registered['access_token']}"}
        project = client.post(
            "/v1/projects",
            headers=headers,
            json={"title": "No Paid Bypass"},
        ).json()
        response = client.post(
            "/v1/generations",
            headers=headers,
            json={
                "project_id": project["id"],
                "type": "video",
                "provider": "openrouter",
                "model": "kwaivgi/kling-v3.0-pro",
                "prompt": "One visible action",
                "idempotency_key": "raw-paid-bypass",
            },
        )

    assert response.status_code != 202
    with container.database.session() as session:
        assert session.scalar(select(func.count(GenerationJob.id))) == 0


def test_free_passenger_charge_job_and_cost_are_atomic_and_idempotent(container, monkeypatch):
    container.settings.auth_required = True
    seedance = container.providers.get("seedance")
    monkeypatch.setattr(seedance, "configured", True)
    container.providers.register_model("seedance", "doubao-seedance-2-5-260628", "video")
    monkeypatch.setattr(
        container.credit_pricing,
        "estimate",
        lambda **_kwargs: CreditEstimate(
            provider_cost_usd=0.1,
            resolution_multiplier=1.0,
            reference_multiplier=1.0,
            service_multiplier=1.2,
            estimated_total_usd=0.12,
            credits=12,
            usd_per_credit=0.01,
        ),
    )
    payload = {
        "media_type": "video",
        "prompt": "One visible action",
        "idempotency_key": "atomic-free-charge",
    }
    with TestClient(create_app(container)) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "email": "free-charge@example.com",
                "password": "correct horse battery staple",
            },
        ).json()
        headers = {"Authorization": f"Bearer {registered['access_token']}"}
        project = client.post(
            "/v1/projects",
            headers=headers,
            json={"title": "Atomic Credits"},
        ).json()
        request = {**payload, "project_id": project["id"]}
        first = client.post("/api/passenger/generate", headers=headers, json=request)
        replay = client.post("/api/passenger/generate", headers=headers, json=request)

    assert first.status_code == 202, first.text
    assert replay.status_code == 202, replay.text
    assert first.json()["id"] == replay.json()["id"]
    assert replay.json()["replayed"] is True
    assert first.json()["provider"] == "seedance"
    with container.database.session() as session:
        stored_project = session.get(Project, project["id"])
        assert stored_project is not None
        workspace = session.get(Workspace, stored_project.workspace_id)
        assert workspace is not None
        assert workspace.credit_balance == 38
        assert session.scalar(select(func.count(GenerationJob.id))) == 1
        assert session.scalar(select(func.count(WorkspaceCreditEntry.id))) == 1
        cost = session.scalar(select(CostRecord))
        assert cost is not None
        assert cost.credits == 12
        assert cost.estimated_cost == pytest.approx(0.12)

    with TestClient(create_app(container)) as client:
        wallet = client.get(
            f"/api/workspaces/{workspace.id}/credits",
            headers=headers,
        )
    assert wallet.status_code == 200, wallet.text
    assert wallet.json()["balance"] == 38
    assert wallet.json()["entries"][0]["credits"] == 12


def test_insufficient_free_credits_roll_back_job_cost_and_ledger(container, monkeypatch):
    container.settings.auth_required = True
    seedance = container.providers.get("seedance")
    monkeypatch.setattr(seedance, "configured", True)
    container.providers.register_model("seedance", "doubao-seedance-2-5-260628", "video")
    with TestClient(create_app(container)) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "email": "free-insufficient@example.com",
                "password": "correct horse battery staple",
            },
        ).json()
        headers = {"Authorization": f"Bearer {registered['access_token']}"}
        project = client.post(
            "/v1/projects",
            headers=headers,
            json={"title": "Insufficient Credits"},
        ).json()
        response = client.post(
            "/api/passenger/generate",
            headers=headers,
            json={
                "project_id": project["id"],
                "media_type": "video",
                "prompt": "One visible action",
                "duration": 8,
                "idempotency_key": "insufficient-free-charge",
            },
        )

    # 402, not 403: being out of credits is a top-up problem, and it shares
    # nothing but a rough shape with a plan that does not permit the request.
    assert response.status_code == 402, response.text
    assert "required=87, available=50" in response.json()["detail"]
    with container.database.session() as session:
        stored_project = session.get(Project, project["id"])
        assert stored_project is not None
        workspace = session.get(Workspace, stored_project.workspace_id)
        assert workspace is not None
        assert workspace.credit_balance == 50
        assert session.scalar(select(func.count(GenerationJob.id))) == 0
        assert session.scalar(select(func.count(WorkspaceCreditEntry.id))) == 0
        assert session.scalar(select(func.count(CostRecord.id))) == 0


def test_free_starter_default_video_fits_one_seedance_reservation(container, monkeypatch):
    container.settings.auth_required = True
    seedance = container.providers.get("seedance")
    monkeypatch.setattr(seedance, "configured", True)
    container.providers.register_model("seedance", "doubao-seedance-2-5-260628", "video")

    with TestClient(create_app(container)) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "email": "free-starter-video@example.com",
                "password": "correct horse battery staple",
            },
        ).json()
        headers = {"Authorization": f"Bearer {registered['access_token']}"}
        project = client.post(
            "/v1/projects",
            headers=headers,
            json={"title": "Starter Video"},
        ).json()
        response = client.post(
            "/api/passenger/generate",
            headers=headers,
            json={
                "project_id": project["id"],
                "media_type": "video",
                "prompt": "One visible action",
                "idempotency_key": "starter-default-four-seconds",
            },
        )

    assert response.status_code == 202, response.text
    assert response.json()["provider"] == "seedance"
    assert response.json()["estimated_credits"] == 44
    with container.database.session() as session:
        stored_project = session.get(Project, project["id"])
        assert stored_project is not None
        workspace = session.get(Workspace, stored_project.workspace_id)
        assert workspace is not None
        assert workspace.credit_balance == 6
        job = session.scalar(select(GenerationJob))
        assert job is not None
        assert job.request_json["duration"] == 4
        entry = session.scalar(select(WorkspaceCreditEntry))
        assert entry is not None
        assert entry.status == "RESERVED"
        assert entry.credits == 44


def test_model_roles_endpoint_hides_unconfigured_deployment_models(container):
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        registered = client.post(
            "/api/auth/register",
            json={
                "email": "role-availability@example.com",
                "password": "correct horse battery staple",
            },
        ).json()
        headers = {"Authorization": f"Bearer {registered['access_token']}"}
        project = client.post(
            "/v1/projects",
            headers=headers,
            json={"title": "Deployment Availability"},
        ).json()
        with container.database.session() as session:
            stored = session.get(Project, project["id"])
            assert stored is not None and stored.workspace_id is not None
            workspace = session.get(Workspace, stored.workspace_id)
            assert workspace is not None
            workspace.plan_tier = "PRO"

        response = client.get(
            f"/api/projects/{project['id']}/model-roles",
            headers=headers,
        )

    assert response.status_code == 200, response.text
    advertised = {item["role"] for item in response.json()["roles"]}
    assert "DIRECTOR" not in advertised
    assert "VIDEO_VEO" not in advertised
    assert "VIDEO_GROK" not in advertised


def test_provider_budget_audit_is_internal_only(container):
    DatabaseProviderBudgetRepository(container.database).ensure("runapi", Decimal("10"))
    with TestClient(create_app(container)) as client:
        denied = client.get("/internal/provider-budgets/runapi")
        allowed = client.get(
            "/internal/provider-budgets/runapi",
            headers={"Authorization": f"Bearer {container.settings.platform_api_key}"},
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()["remaining_budget_usd"] == "10.000000"
