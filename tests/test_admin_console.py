from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from model_registry_core import ShotRequirements
from production_domain.models import (
    AdminAuditLog,
    AdminCreditAdjustment,
    GenerationJob,
    ModelDefinition,
    PlatformRole,
    Project,
    ProviderCredential,
    User,
    Workspace,
)
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from video_platform_api.main import create_app


def _register(client: TestClient, email: str) -> dict:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "correct horse battery staple", "display_name": email},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _headers(auth: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth['access_token']}"}


def _promote(container, user_id: str, role: str = "SUPER_ADMIN") -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        user = session.get(User, user_id)
        assert user is not None
        user.platform_role = role


def test_user_is_forbidden_from_every_admin_read_boundary(container) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        user = _register(client, "ordinary@example.com")
        for path in (
            "/api/admin/session",
            "/api/admin/dashboard",
            "/api/admin/users",
            "/api/admin/credits",
            "/api/admin/models",
            "/api/admin/providers",
            "/api/admin/routing",
            "/api/admin/jobs",
            "/api/admin/projects",
            "/api/admin/system",
            "/api/admin/audit",
        ):
            response = client.get(path, headers=_headers(user))
            assert response.status_code == 403, (path, response.text)


def test_admin_can_read_and_provider_secret_never_leaves_json(container) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        issued = _register(client, "admin@example.com")
        _promote(container, issued["user"]["id"], PlatformRole.ADMIN.value)
        with container.database.session() as session:
            session.add(
                ProviderCredential(
                    provider="wan",
                    secret_ciphertext="TOP-SECRET-CIPHERTEXT",
                    redacted_fingerprint="sha256:1234",
                )
            )
        response = client.get("/api/admin/providers", headers=_headers(issued))
        assert response.status_code == 200, response.text
        assert "TOP-SECRET-CIPHERTEXT" not in response.text
        wan = next(item for item in response.json()["items"] if item["name"] == "wan")
        assert wan["credential_present"] is True
        assert wan["credential_status"][0]["masked_identifier"] == "sha256:1234"
        not_configured = [item for item in response.json()["items"] if not item["configured"]]
        assert not_configured
        assert all(item["health"] == "NOT_CONFIGURED" for item in not_configured)


def test_credit_adjustment_is_atomic_nonnegative_idempotent_and_audited(container) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        issued = _register(client, "credit-admin@example.com")
        _promote(container, issued["user"]["id"])
        user_id = issued["user"]["id"]
        workspace_id = issued["user"]["workspaces"][0]["id"]
        command = {
            "workspace_id": workspace_id,
            "delta": 25,
            "reason": "Customer support correction",
            "reference": "ticket-42",
        }
        headers = {**_headers(issued), "Idempotency-Key": "credit-adjustment-42"}
        first = client.post(f"/api/admin/users/{user_id}/credit-adjustments", headers=headers, json=command)
        replay = client.post(f"/api/admin/users/{user_id}/credit-adjustments", headers=headers, json=command)
        assert first.status_code == 201, first.text
        assert replay.status_code == 201, replay.text
        assert first.json()["after_balance"] == 75
        assert replay.json()["replayed"] is True
        denied = client.post(
            f"/api/admin/users/{user_id}/credit-adjustments",
            headers={**_headers(issued), "Idempotency-Key": "credit-adjustment-negative"},
            json={**command, "delta": -76},
        )
        assert denied.status_code == 422
        ledger = client.get(
            f"/api/admin/credits?user_id={user_id}&event_type=MANUAL_ADJUSTMENT",
            headers=_headers(issued),
        )
        assert ledger.status_code == 200, ledger.text
        assert ledger.json()["summary"]["available"] == 75
        assert ledger.json()["items"][0]["source"] == "ADMIN_ADJUSTMENT"
        assert ledger.json()["items"][0]["balance_delta"] == 25
        with container.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            assert workspace is not None and workspace.credit_balance == 75
            assert session.scalar(select(func.count(AdminCreditAdjustment.id))) == 1
            audit = session.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "CREDITS_ADJUSTED"))
            assert audit is not None
            assert audit.before_json == {"credit_balance": 50}
            assert audit.after_json["credit_balance"] == 75
        with pytest.raises(IntegrityError, match="append-only"):
            with container.database.session() as session:
                session.execute(
                    update(AdminCreditAdjustment)
                    .where(AdminCreditAdjustment.id == first.json()["id"])
                    .values(reason="Attempted rewrite")
                )


def test_model_live_gate_router_switch_and_audit(container, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        issued = _register(client, "model-admin@example.com")
        _promote(container, issued["user"]["id"])
        headers = _headers(issued)
        with container.database.session() as session:
            model = session.scalar(
                select(ModelDefinition).where(
                    ModelDefinition.enabled.is_(True),
                    ModelDefinition.modality == "video",
                )
            )
            assert model is not None
            model_id = model.id
            model_provider = model.provider
            model_name = model.provider_model_id
            model.lifecycle_status = "VERIFIED"
            model.router_enabled = True
            model.live_enabled = False
            project = Project(
                workspace_id=issued["user"]["workspaces"][0]["id"],
                title="Live verification evidence",
            )
            session.add(project)
            session.flush()
            evidence_job = GenerationJob(
                project_id=project.id,
                generation_type=model.modality,
                provider=model.provider,
                model=model.provider_model_id,
                status="COMPLETED",
                request_json={"prompt": "Production verification"},
                provider_request_json={},
                request_hash="0" * 64,
                completed_at=datetime.now(UTC),
            )
            session.add(evidence_job)
            session.flush()
            evidence_job_id = evidence_job.id

        testing = client.post(
            f"/api/admin/models/{model_id}/lifecycle-transition",
            headers=headers,
            json={"target_status": "TESTING", "reason": "Begin production protocol validation"},
        )
        assert testing.status_code == 200, testing.text
        direct_verified = client.post(
            f"/api/admin/models/{model_id}/lifecycle-transition",
            headers=headers,
            json={"target_status": "VERIFIED", "reason": "Attempt manual verification"},
        )
        assert direct_verified.status_code == 409
        with container.database.session() as session:
            model = session.get(ModelDefinition, model_id)
            assert model is not None
            model.lifecycle_status = "VERIFIED"

        blocked = client.post(
            f"/api/admin/models/{model_id}/lifecycle-transition",
            headers=headers,
            json={"target_status": "LIVE", "reason": "Promote after validation"},
        )
        assert blocked.status_code == 409
        assert "no successful billable production-protocol verification exists" in blocked.text

        metadata_probe = client.post(
            f"/api/admin/models/{model_id}/verifications",
            headers={**headers, "Idempotency-Key": "model-metadata-proof-1"},
            json={
                "protocol_version": "metadata-v1",
                "result": "SUCCESS",
                "evidence_reference": "probe:metadata-123",
                "billable": False,
                "latency_ms": 80,
            },
        )
        assert metadata_probe.status_code == 201, metadata_probe.text
        still_blocked = client.post(
            f"/api/admin/models/{model_id}/lifecycle-transition",
            headers=headers,
            json={"target_status": "LIVE", "reason": "Promote after metadata probe"},
        )
        assert still_blocked.status_code == 409
        assert "billable production-protocol" in still_blocked.text

        fabricated_live_proof = client.post(
            f"/api/admin/models/{model_id}/verifications",
            headers={**headers, "Idempotency-Key": "fabricated-live-proof"},
            json={
                "protocol_version": "generation-v1",
                "result": "SUCCESS",
                "evidence_reference": "canary:not-a-completed-job",
                "billable": True,
            },
        )
        assert fabricated_live_proof.status_code == 422

        verification = client.post(
            f"/api/admin/models/{model_id}/verifications",
            headers={**headers, "Idempotency-Key": "model-live-proof-1"},
            json={
                "protocol_version": "generation-v1",
                "result": "SUCCESS",
                "evidence_reference": f"generation-job:{evidence_job_id}",
                "billable": True,
                "latency_ms": 1200,
            },
        )
        assert verification.status_code == 201, verification.text
        monkeypatch.setattr(container.providers, "is_configured", lambda provider: provider == model_provider)
        live = client.post(
            f"/api/admin/models/{model_id}/lifecycle-transition",
            headers=headers,
            json={"target_status": "LIVE", "reason": "Promote after validation"},
        )
        assert live.status_code == 200, live.text
        decision = container.video_router.rank(ShotRequirements())
        assert any(
            item.provider == model_provider and item.model == model_name for item in decision.candidates
        )
        disabled = client.post(
            f"/api/admin/models/{model_id}/router",
            headers=headers,
            json={"enabled": False, "reason": "Temporary routing removal"},
        )
        assert disabled.status_code == 200, disabled.text
        next_decision = container.video_router.rank(ShotRequirements())
        assert all(
            not (item.provider == model_provider and item.model == model_name)
            for item in next_decision.candidates
        )

        metadata = client.post(
            f"/api/admin/models/{model_id}/metadata",
            headers=headers,
            json={
                "display_name": "QA Production Model",
                "user_visible": True,
                "pricing_metadata": {
                    "billing_unit": "GENERATION",
                    "credits": 12,
                    "currency": "CREDITS",
                    "amount": 12,
                },
                "reason": "Reviewed user pricing metadata",
            },
        )
        assert metadata.status_code == 200, metadata.text
        detail = client.get(f"/api/admin/models/{model_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        profile = detail.json()["capabilities"]
        capabilities = client.post(
            f"/api/admin/models/{model_id}/capabilities",
            headers=headers,
            json={
                "supported_operations": profile["supported_operations"],
                "supports_image_generation": profile["supports_image_generation"],
                "supports_video_generation": profile["supports_video_generation"],
                "supports_t2v": profile["supports_t2v"],
                "supports_i2v": profile["supports_i2v"],
                "supports_v2v": profile["supports_v2v"],
                "supports_reference_image": profile["supports_reference_image"],
                "supports_multi_reference": profile["supports_multi_reference"],
                "supports_start_frame": profile["supports_start_frame"],
                "supports_end_frame": profile["supports_end_frame"],
                "supports_audio": profile["supports_audio"],
                "max_reference_images": profile["max_reference_images"],
                "min_duration": profile["min_duration"],
                "max_duration": profile["max_duration"],
                "supported_aspect_ratios": profile["supported_aspect_ratios"],
                "supported_resolutions": profile["supported_resolutions"],
                "reason": "Reconfirm reviewed capability contract",
            },
        )
        assert capabilities.status_code == 200, capabilities.text
        assert capabilities.json()["verification_invalidated"] is True
        assert capabilities.json()["lifecycle_status"] == "CONFIGURED"
        assert capabilities.json()["router_enabled"] is False
        with container.database.session() as session:
            actions = set(session.scalars(select(AdminAuditLog.action)))
        assert {
            "MODEL_VERIFICATION_RECORDED",
            "MODEL_LIFECYCLE_CHANGED",
            "MODEL_ROUTER_CHANGED",
            "MODEL_METADATA_CHANGED",
            "MODEL_CAPABILITIES_CHANGED",
        } <= actions
