"""UI-alignment endpoints: the catalogue the browser renders is the server's.

What is pinned:

1. `GET /v1/image-tiers` reports the three public tiers with the same checks
   admission performs — an unpriced tier reports PRICING_UNVERIFIED, a
   credential-less provider reports PROVIDER_NOT_CONFIGURED, and a FREE
   workspace sees Pro tiers listed but not allowed;
2. `GET /v1/models` is the full user-facing catalogue, independent of which
   providers hold credentials, with unpriced models flagged and FREE plan
   locks visible instead of hidden;
3. `GET /v1/generations` lists a project's jobs newest first with the real
   cost and timestamp columns, behind the same project access check as the
   per-job endpoint;
4. paid "Auto" video admission resolves to a route with a provider-verified
   price — the default can never again point at a model the quote refuses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from model_registry_core import ModelRole
from platform_contracts import GenerationRequest
from production_domain.models import (
    GenerationJob,
    ModelPricingProfile,
    Project,
    User,
    Workspace,
)
from sqlalchemy import select
from video_platform_api.main import create_app


def _register(client: TestClient, email: str) -> tuple[dict, str]:
    registered = client.post(
        "/api/auth/register",
        json={"email": email, "password": "correct horse battery staple"},
    ).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    project = client.post("/v1/projects", headers=headers, json={"title": "Alignment"}).json()
    return headers, project["id"]


def _seed_verified_price(container, provider: str, model: str, *, unit: str, price: str = "0.03"):  # type: ignore[no-untyped-def]
    """One provider-verified list price, the shape migration 0062 seeds."""

    with container.database.session() as session:
        session.add(
            ModelPricingProfile(
                provider=provider,
                provider_model_id=model,
                input_mode="default",
                resolution="",
                currency="USD",
                billing_unit=unit,
                unit_price=Decimal(price),
                estimate_unit=unit,
                estimate_unit_price=Decimal(price),
                usd_per_currency=Decimal("1"),
                fx_source="test",
                effective_from=datetime.now(UTC) - timedelta(days=1),
                source_url="https://example.com/pricing",
                source_checked_at=datetime.now(UTC),
            )
        )


def _upgrade_workspace(container, project_id: str, plan_tier: str = "PRO") -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        workspace = session.scalar(
            select(Workspace)
            .join(Project, Project.workspace_id == Workspace.id)
            .where(Project.id == project_id)
        )
        assert workspace is not None
        workspace.plan_tier = plan_tier


def _pro_project(container, email: str) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        user = User(email=email, display_name="Paid Auto")
        session.add(user)
        session.flush()
        workspace = Workspace(owner_user_id=user.id, name="Pro", status="ACTIVE", plan_tier="PRO")
        session.add(workspace)
        session.flush()
        project = Project(workspace_id=workspace.id, title="Paid Auto", status="ACTIVE")
        session.add(project)
        session.flush()
        return project.id


# ------------------------------------------------------------------
# 1. /v1/image-tiers
# ------------------------------------------------------------------


def test_image_tiers_report_availability_with_admissions_own_checks(container, monkeypatch):
    container.settings.auth_required = True
    # Shiny's provider holds a credential and a verified list price.
    monkeypatch.setattr(container.providers.get("seedance"), "configured", True)
    _seed_verified_price(container, "seedance", "doubao-seedream-5-0-260128", unit="image")

    with TestClient(create_app(container)) as client:
        anonymous = client.get("/v1/image-tiers")
        headers, project_id = _register(client, "tiers-free@example.com")
        response = client.get("/v1/image-tiers", headers=headers)

    assert anonymous.status_code == 401
    assert response.status_code == 200, response.text
    tiers = {item["tier"]: item for item in response.json()}
    assert list(tiers) == ["shiny", "shinier", "shiniest"]

    shiny = tiers["shiny"]
    assert shiny["name"] == "Shiny" and shiny["stars"] == "✨"
    assert shiny["plan_requirement"] == "FREE"
    assert shiny["allowed_for_workspace"] is True
    assert shiny["available"] is True
    assert shiny["unavailable_reason"] is None

    # A FREE workspace sees the Pro tiers, locked rather than hidden.
    assert tiers["shinier"]["plan_requirement"] == "PRO"
    assert tiers["shinier"]["allowed_for_workspace"] is False
    assert tiers["shiniest"]["allowed_for_workspace"] is False

    # Shinier's model (google_flow/NARWHAL) is registered and enabled but has
    # no verified price — the exact production state migration 0062 left.
    assert tiers["shinier"]["available"] is False
    assert tiers["shinier"]["unavailable_reason"] == "PRICING_UNVERIFIED"

    # Shiniest's provider has no credential in this deployment; that is the
    # first failing check, before the model or pricing are even consulted.
    assert tiers["shiniest"]["available"] is False
    assert tiers["shiniest"]["unavailable_reason"] == "PROVIDER_NOT_CONFIGURED"


def test_image_tiers_unlock_for_a_paid_workspace(container, monkeypatch):
    container.settings.auth_required = True
    monkeypatch.setattr(container.providers.get("seedance"), "configured", True)
    with TestClient(create_app(container)) as client:
        headers, project_id = _register(client, "tiers-pro@example.com")
        _upgrade_workspace(container, project_id)
        response = client.get("/v1/image-tiers", headers=headers)
    tiers = {item["tier"]: item for item in response.json()}
    assert all(item["allowed_for_workspace"] for item in tiers.values())


# ------------------------------------------------------------------
# 2. /v1/models
# ------------------------------------------------------------------


def test_video_model_catalogue_is_independent_of_credentials(container, monkeypatch):
    container.settings.auth_required = True
    monkeypatch.setattr(container.providers.get("seedance"), "configured", True)
    _seed_verified_price(container, "seedance", "doubao-seedance-2-5-260628", unit="second")

    with TestClient(create_app(container)) as client:
        headers, project_id = _register(client, "models-free@example.com")
        response = client.get("/v1/models?modality=video", headers=headers)
        invalid = client.get("/v1/models?modality=text", headers=headers)
        missing = client.get("/v1/models", headers=headers)

    assert response.status_code == 200, response.text
    assert invalid.status_code == 422
    assert missing.status_code == 422
    models = {(item["provider"], item["model_id"]): item for item in response.json()}

    # The whole enabled, user-visible video catalogue is present, credentialed
    # or not: /v1/providers hides openrouter here, this endpoint must not.
    assert ("openrouter", "kwaivgi/kling-v3.0-pro") in models
    assert ("google_flow", "flow-veo-3.1") in models
    assert ("seedance", "doubao-seedance-2-5-260628") in models
    # Disabled models stay out entirely.
    assert ("wan", "wan3.0-video") not in models
    assert all(item["modality"] == "video" for item in models.values())

    kling = models[("openrouter", "kwaivgi/kling-v3.0-pro")]
    assert kling["available"] is False
    assert kling["unavailable_reason"] == "PROVIDER_NOT_CONFIGURED"
    assert kling["plan_locked"] is True  # FREE workspace, not the Seedance route

    # flow-veo-3.1 is the known-unpriced model: configured, enabled, unquotable.
    flow = models[("google_flow", "flow-veo-3.1")]
    assert flow["available"] is False
    assert flow["unavailable_reason"] == "PRICING_UNVERIFIED"

    seedance = models[("seedance", "doubao-seedance-2-5-260628")]
    assert seedance["available"] is True
    assert seedance["unavailable_reason"] is None
    assert seedance["plan_locked"] is False
    for field in (
        "logical_name",
        "min_duration",
        "max_duration",
        "supported_resolutions",
        "supports_reference_image",
        "max_reference_images",
    ):
        assert field in seedance


def test_model_catalogue_drops_plan_locks_for_paid_workspaces(container, monkeypatch):
    container.settings.auth_required = True
    monkeypatch.setattr(container.providers.get("seedance"), "configured", True)
    with TestClient(create_app(container)) as client:
        headers, project_id = _register(client, "models-pro@example.com")
        _upgrade_workspace(container, project_id)
        response = client.get("/v1/models?modality=video", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json(), "the catalogue must not be empty"
    assert not any(item["plan_locked"] for item in response.json())


# ------------------------------------------------------------------
# 3. /v1/generations list + _job_view cost/timestamps
# ------------------------------------------------------------------


def _seed_job(container, project_id: str, index: int) -> None:  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    with container.database.session() as session:
        session.add(
            GenerationJob(
                id=f"job-{index}",
                project_id=project_id,
                generation_type="video",
                provider="seedance",
                model="doubao-seedance-2-5-260628",
                status="COMPLETED",
                request_json={},
                request_hash=f"{index:064x}",
                cost_estimate=0.12 + index,
                actual_cost=0.10 + index,
                quoted_credits=12 + index,
                submitted_at=now + timedelta(seconds=index),
                started_at=now + timedelta(seconds=index, milliseconds=200),
                completed_at=now + timedelta(seconds=index + 30),
                created_at=now + timedelta(seconds=index),
            )
        )


def test_generation_list_is_newest_first_with_costs_and_timestamps(container):
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        anonymous = client.get("/v1/generations?project_id=unknowable")
        headers, project_id = _register(client, "list-gen@example.com")
        for index in range(3):
            _seed_job(container, project_id, index)

        listed = client.get(f"/v1/generations?project_id={project_id}", headers=headers)
        clamped = client.get(f"/v1/generations?project_id={project_id}&limit=0", headers=headers)
        limited = client.get(f"/v1/generations?project_id={project_id}&limit=2", headers=headers)
        detail = client.get("/v1/generations/job-2", headers=headers)

        # A stranger with their own workspace cannot list someone else's project.
        stranger_headers, _ = _register(client, "list-stranger@example.com")
        stranger = client.get(
            f"/v1/generations?project_id={project_id}", headers=stranger_headers
        )

    assert anonymous.status_code == 401
    assert stranger.status_code == 403
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [job["id"] for job in body["jobs"]] == ["job-2", "job-1", "job-0"]
    assert body["has_more"] is False

    top = body["jobs"][0]
    assert top["project_id"] == project_id
    assert top["estimated_cost"] == pytest.approx(2.12)
    # The reconciled provider figure is a billing internal and must not reach
    # a user surface; only the quote the job was reserved on is emitted.
    assert "actual_cost" not in top
    assert top["estimated_credits"] == 14
    assert top["submitted_at"] and top["started_at"] and top["completed_at"]
    assert top["created_at"]
    # The list stays light: histories live on the per-job endpoint.
    assert "events" not in top

    assert clamped.status_code == 200
    assert len(clamped.json()["jobs"]) == 1
    assert limited.status_code == 200
    assert len(limited.json()["jobs"]) == 2
    assert limited.json()["has_more"] is True

    assert detail.status_code == 200, detail.text
    job = detail.json()
    assert job["estimated_credits"] == 14
    assert job["estimated_cost"] == pytest.approx(2.12)
    assert job["completed_at"]
    assert "events" in job


# ------------------------------------------------------------------
# 4. Paid "Auto" resolves a priced, admissible route
# ------------------------------------------------------------------


def test_paid_auto_video_default_is_seedance(container):
    project_id = _pro_project(container, "paid-default@example.com")
    assert (
        container.workspace_models.default_video_role(project_id) is ModelRole.VIDEO_SEEDANCE
    )


def test_paid_auto_video_admission_resolves_a_priced_route(container, monkeypatch):
    monkeypatch.setattr(container.providers.get("seedance"), "configured", True)
    _seed_verified_price(container, "seedance", "doubao-seedance-2-5-260628", unit="second")
    project_id = _pro_project(container, "paid-auto@example.com")

    admitted = container.generation_admission.admit_passenger(
        GenerationRequest(
            project_id=project_id,
            type="video",
            # An empty pair is what the browser's "Auto" sends: no named model,
            # the platform routes.
            provider="",
            model="",
            prompt="One visible action",
            idempotency_key="paid-auto-priced",
        )
    )

    assert admitted.model_role == "VIDEO_SEEDANCE"
    assert admitted.request.provider == "seedance"
    assert admitted.request.model == "doubao-seedance-2-5-260628"
    # The route Auto picks must be priceable from a provider-verified rate:
    # this is the exact gate flow-veo-3.1 failed as the old paid default.
    assert admitted.estimate.pricing_status == "VERIFIED"
    assert container.generation_admission.pricing_verified(
        admitted.request.provider, admitted.request.model, "video"
    )


# ------------------------------------------------------------------
# 5. Plan locks follow the project's workspace, not an arbitrary membership
# ------------------------------------------------------------------


def _pro_membership_project(container, email: str) -> str:  # type: ignore[no-untyped-def]
    """A second, PRO workspace the same user merely belongs to, with a project."""

    from production_domain.models import WorkspaceMembership

    with container.database.session() as session:
        user = session.scalar(select(User).where(User.email == email))
        assert user is not None
        workspace = Workspace(owner_user_id=user.id, name="Shared Pro", status="ACTIVE", plan_tier="PRO")
        session.add(workspace)
        session.flush()
        session.add(
            WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="VIEWER", status="ACTIVE")
        )
        project = Project(workspace_id=workspace.id, title="Shared Pro project", status="ACTIVE")
        session.add(project)
        session.flush()
        return project.id


def test_catalogue_plan_locks_follow_the_projects_workspace(container, monkeypatch):
    """A user in a FREE personal workspace and a PRO shared one must see the
    locks of the project they are working in — the ones admission applies —
    and, with no project named, the most restrictive of their workspaces.
    Membership order is not a tier."""

    container.settings.auth_required = True
    monkeypatch.setattr(container.providers.get("seedance"), "configured", True)

    with TestClient(create_app(container)) as client:
        headers, free_project = _register(client, "two-workspaces@example.com")
        pro_project = _pro_membership_project(container, "two-workspaces@example.com")

        free_scoped = client.get(
            f"/v1/models?modality=video&project_id={free_project}", headers=headers
        )
        pro_scoped = client.get(
            f"/v1/models?modality=video&project_id={pro_project}", headers=headers
        )
        unscoped = client.get("/v1/models?modality=video", headers=headers)
        tiers_free = client.get(f"/v1/image-tiers?project_id={free_project}", headers=headers)
        tiers_pro = client.get(f"/v1/image-tiers?project_id={pro_project}", headers=headers)

        stranger_headers, _ = _register(client, "two-workspaces-stranger@example.com")
        forbidden = client.get(
            f"/v1/models?modality=video&project_id={pro_project}", headers=stranger_headers
        )

    def _locked(response, model_id: str) -> bool:
        assert response.status_code == 200, response.text
        return next(item for item in response.json() if item["model_id"] == model_id)["plan_locked"]

    paid_route = "kwaivgi/kling-v3.0-pro"
    assert _locked(free_scoped, paid_route) is True
    assert _locked(pro_scoped, paid_route) is False
    # No project named: the FREE membership wins, which can only under-promise.
    assert _locked(unscoped, paid_route) is True

    assert tiers_free.status_code == 200 and tiers_pro.status_code == 200
    shinier_free = next(t for t in tiers_free.json() if t["tier"] == "shinier")
    shinier_pro = next(t for t in tiers_pro.json() if t["tier"] == "shinier")
    assert shinier_free["allowed_for_workspace"] is False
    assert shinier_pro["allowed_for_workspace"] is True

    # Scoping to a project you cannot read is refused like every project route.
    assert forbidden.status_code == 403
