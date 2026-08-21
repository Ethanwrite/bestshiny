from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from production_domain.models import (
    CostRecord,
    DecisionRecord,
    ProviderBudgetUsage,
    User,
    Workspace,
)
from provider_budget_core import DatabaseProviderBudgetRepository
from provider_sdk import ProviderBudgetConflict
from sqlalchemy import func, select
from video_platform_api.main import create_app


def _uncertain_reservation(container, *, task_id: str = "runapi-uncertain-task"):  # type: ignore[no-untyped-def]
    repository = DatabaseProviderBudgetRepository(container.database)
    repository.ensure("runapi", Decimal("10"))
    reservation = repository.reserve(
        provider="runapi",
        task_id=task_id,
        task_role="PROMPT_REFINER_LOW_COST",
        estimated_cost_usd=Decimal("3.25"),
    )
    return repository.settle(
        reservation.reservation_id,
        actual_cost_usd=None,
        status="UNCERTAIN",
    )


def _headers(container, *, key: str = "provider-budget-decision-001") -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {
        "Authorization": f"Bearer {container.settings.platform_api_key}",
        "Idempotency-Key": key,
    }


def _settle_body() -> dict[str, object]:
    return {
        "action": "SETTLE_ACTUAL_COST",
        "actual_cost_usd": "2.500000",
        "reason": "Provider invoice confirms the final charge.",
        "evidence_reference": "invoice:runapi:test-001",
        "explicit_confirmation": True,
    }


def test_provider_budget_reconcile_is_internal_strict_and_requires_confirmation(container) -> None:  # type: ignore[no-untyped-def]
    reservation = _uncertain_reservation(container)
    path = f"/internal/provider-budget-reservations/{reservation.reservation_id}/reconcile"
    forbidden_facts = {
        **_settle_body(),
        "workspace_id": "attacker-workspace",
        "provider": "another-provider",
        "model": "forged-model",
        "task_id": "forged-task",
        "estimated_cost_usd": "0.000001",
        "reservation_id": "forged-reservation",
    }

    with TestClient(create_app(container)) as client:
        unauthenticated = client.post(
            path,
            json=_settle_body(),
            headers={"Idempotency-Key": "provider-budget-unauthorized"},
        )
        ordinary_bearer = client.post(
            path,
            json=_settle_body(),
            headers={
                "Authorization": "Bearer ordinary-user-token",
                "Idempotency-Key": "provider-budget-user-token",
            },
        )
        missing_key = client.post(
            path,
            json=_settle_body(),
            headers={"Authorization": f"Bearer {container.settings.platform_api_key}"},
        )
        unconfirmed = client.post(
            path,
            json={**_settle_body(), "explicit_confirmation": False},
            headers=_headers(container),
        )
        extra = client.post(path, json=forbidden_facts, headers=_headers(container))

    assert unauthenticated.status_code == 401
    assert ordinary_bearer.status_code == 401
    assert missing_key.status_code == 400
    assert unconfirmed.status_code == 422
    assert extra.status_code == 422
    with container.database.session() as session:
        stored = session.get(ProviderBudgetUsage, reservation.reservation_id)
        assert stored is not None and stored.status == "UNCERTAIN"
        assert (
            session.scalar(
                select(func.count(DecisionRecord.id)).where(
                    DecisionRecord.decision_type == "PROVIDER_BUDGET_RECONCILIATION"
                )
            )
            == 0
        )


def test_provider_budget_uncertain_settlement_is_audited_idempotent_and_isolated(
    container,
    project,
) -> None:  # type: ignore[no-untyped-def]
    reservation = _uncertain_reservation(container)
    with container.database.session() as session:
        user = User(email="provider-budget-audit@example.com", display_name="Budget Audit")
        session.add(user)
        session.flush()
        workspace = Workspace(
            owner_user_id=user.id,
            name="Unrelated Workspace Wallet",
            status="ACTIVE",
            plan_tier="FREE",
            credit_balance=37,
        )
        session.add(workspace)
        session.flush()
        stored_project = session.get(type(project), project.id)
        assert stored_project is not None
        stored_project.workspace_id = workspace.id
        cost = CostRecord(
            project_id=project.id,
            provider="runapi",
            model="unrelated-cost-record",
            credits=7,
            estimated_cost=8.0,
            actual_cost=9.0,
        )
        session.add(cost)
        session.flush()
        workspace_id = workspace.id
        cost_id = cost.id

    path = f"/internal/provider-budget-reservations/{reservation.reservation_id}/reconcile"
    headers = _headers(container)
    with TestClient(create_app(container)) as client:
        first = client.post(path, json=_settle_body(), headers=headers)
        replay = client.post(path, json=_settle_body(), headers=headers)
        same_key_different_cost = client.post(
            path,
            json={**_settle_body(), "actual_cost_usd": "2.600000"},
            headers=headers,
        )
        same_key_different_action = client.post(
            path,
            json={
                "action": "RELEASE_NO_REMOTE_CHARGE",
                "reason": "A conflicting claim must not release this charge.",
                "evidence_reference": "provider-log:conflict",
                "explicit_confirmation": True,
            },
            headers=headers,
        )
        different_key = client.post(
            path,
            json={
                "action": "RELEASE_NO_REMOTE_CHARGE",
                "reason": "A second decision must not replace the terminal decision.",
                "evidence_reference": "provider-log:second-decision",
                "explicit_confirmation": True,
            },
            headers=_headers(container, key="provider-budget-decision-002"),
        )

    assert first.status_code == 200, first.text
    assert first.json()["reservation"]["status"] == "SETTLED"
    assert first.json()["reservation"]["actual_cost_usd"] == "2.500000"
    assert first.json()["provider_budget"]["reserved_cost_usd"] == "0.000000"
    assert first.json()["provider_budget"]["actual_cost_usd"] == "2.500000"
    assert first.json()["provider_budget"]["remaining_budget_usd"] == "7.500000"
    assert first.json()["replayed"] is False
    assert replay.status_code == 200, replay.text
    assert replay.json()["audit_decision_id"] == first.json()["audit_decision_id"]
    assert replay.json()["replayed"] is True
    assert same_key_different_cost.status_code == 409
    assert same_key_different_action.status_code == 409
    assert different_key.status_code == 409

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        cost = session.get(CostRecord, cost_id)
        audits = list(
            session.scalars(
                select(DecisionRecord).where(DecisionRecord.decision_type == "PROVIDER_BUDGET_RECONCILIATION")
            )
        )
        assert workspace is not None and workspace.credit_balance == 37
        assert cost is not None and cost.credits == 7 and cost.actual_cost == 9.0
        assert len(audits) == 1
        audit = audits[0]
        assert audit.project_id is None
        assert audit.shot_id is None
        assert audit.selected_action == "SETTLE_ACTUAL_COST"
        assert audit.input_features == {
            "provider_budget_reservation_id": reservation.reservation_id,
            "provider": "runapi",
            "task_id": "runapi-uncertain-task",
            "task_role": "PROMPT_REFINER_LOW_COST",
            "estimated_cost_usd": "3.250000",
            "previous_status": "UNCERTAIN",
            "action": "SETTLE_ACTUAL_COST",
            "actual_cost_usd": "2.500000",
            "reason": "Provider invoice confirms the final charge.",
            "evidence_reference": "invoice:runapi:test-001",
            "explicit_confirmation": True,
            "server_actor": "PLATFORM_API_KEY",
            "idempotency_key": "provider-budget-decision-001",
            "resulting_status": "SETTLED",
            "remaining_budget_usd": "7.500000",
        }


def test_provider_budget_uncertain_release_unfreezes_estimate(container) -> None:  # type: ignore[no-untyped-def]
    reservation = _uncertain_reservation(container)
    path = f"/internal/provider-budget-reservations/{reservation.reservation_id}/reconcile"
    body = {
        "action": "RELEASE_NO_REMOTE_CHARGE",
        "reason": "Provider activity ledger proves no remote request was created.",
        "evidence_reference": "provider-log:no-request:test-001",
        "explicit_confirmation": True,
    }

    with TestClient(create_app(container)) as client:
        response = client.post(path, json=body, headers=_headers(container))
        replay = client.post(path, json=body, headers=_headers(container))

    assert response.status_code == 200, response.text
    assert response.json()["reservation"]["status"] == "RELEASED"
    assert response.json()["reservation"]["actual_cost_usd"] is None
    assert response.json()["provider_budget"]["reserved_cost_usd"] == "0.000000"
    assert response.json()["provider_budget"]["actual_cost_usd"] == "0.000000"
    assert response.json()["provider_budget"]["remaining_budget_usd"] == "10.000000"
    assert response.json()["provider_budget"]["routing_enabled"] is True
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["audit_decision_id"] == response.json()["audit_decision_id"]


def test_provider_budget_reconcile_same_facts_are_concurrent_exactly_once(container) -> None:  # type: ignore[no-untyped-def]
    reservation = _uncertain_reservation(container)

    def reconcile():  # type: ignore[no-untyped-def]
        repository = DatabaseProviderBudgetRepository(container.database)
        return repository.reconcile_uncertain(
            reservation.reservation_id,
            action="SETTLE_ACTUAL_COST",
            actual_cost_usd=Decimal("2.5"),
            idempotency_key="provider-budget-concurrent-001",
            reason="Concurrent invoice verification reached the same result.",
            evidence_reference="invoice:runapi:concurrent-001",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: reconcile(), range(2)))

    assert sorted(result.replayed for result in results) == [False, True]
    assert len({result.audit_decision_id for result in results}) == 1
    snapshot = DatabaseProviderBudgetRepository(container.database).get("runapi")
    assert snapshot.actual_cost_usd == Decimal("2.500000")
    assert snapshot.reserved_cost_usd == Decimal("0.000000")
    with container.database.session() as session:
        assert (
            session.scalar(
                select(func.count(DecisionRecord.id)).where(
                    DecisionRecord.decision_type == "PROVIDER_BUDGET_RECONCILIATION"
                )
            )
            == 1
        )


@pytest.mark.parametrize("winner", ["manual_release", "late_actual"])
def test_provider_budget_manual_reconcile_and_late_actual_have_one_linearized_winner(
    container,
    winner: str,
) -> None:  # type: ignore[no-untyped-def]
    reservation = _uncertain_reservation(container, task_id=f"linearized-{winner}")
    repository = DatabaseProviderBudgetRepository(container.database)

    def manual_release():  # type: ignore[no-untyped-def]
        return repository.reconcile_uncertain(
            reservation.reservation_id,
            action="RELEASE_NO_REMOTE_CHARGE",
            actual_cost_usd=None,
            idempotency_key=f"provider-budget-{winner}-001",
            reason="Provider activity ledger confirms no charge.",
            evidence_reference=f"provider-log:{winner}",
        )

    def settle_late_actual():  # type: ignore[no-untyped-def]
        return repository.settle(
            reservation.reservation_id,
            actual_cost_usd=Decimal("2.5"),
            status="SETTLED",
        )

    if winner == "manual_release":
        assert manual_release().reservation.status == "RELEASED"
        with pytest.raises(ProviderBudgetConflict, match="cannot settle a RELEASED"):
            settle_late_actual()
        expected_actual = Decimal("0.000000")
        expected_audits = 1
    else:
        assert settle_late_actual().status == "SETTLED"
        with pytest.raises(ProviderBudgetConflict, match="requires UNCERTAIN"):
            manual_release()
        expected_actual = Decimal("2.500000")
        expected_audits = 0

    snapshot = repository.get("runapi")
    assert snapshot.actual_cost_usd == expected_actual
    assert snapshot.reserved_cost_usd == Decimal("0.000000")
    with container.database.session() as session:
        assert (
            session.scalar(
                select(func.count(DecisionRecord.id)).where(
                    DecisionRecord.decision_type == "PROVIDER_BUDGET_RECONCILIATION"
                )
            )
            == expected_audits
        )


def test_provider_budget_reconcile_refuses_reserved_without_remote_boundary_evidence(container) -> None:  # type: ignore[no-untyped-def]
    repository = DatabaseProviderBudgetRepository(container.database)
    repository.ensure("runapi", Decimal("10"))
    reservation = repository.reserve(
        provider="runapi",
        task_id="still-reserved",
        task_role="PROMPT_REFINER_LOW_COST",
        estimated_cost_usd=Decimal("1.5"),
    )

    with pytest.raises(ProviderBudgetConflict, match="requires UNCERTAIN"):
        repository.reconcile_uncertain(
            reservation.reservation_id,
            action="RELEASE_NO_REMOTE_CHARGE",
            actual_cost_usd=None,
            idempotency_key="provider-budget-reserved-001",
            reason="No stale reservation policy exists yet.",
            evidence_reference="manual-review:reserved-001",
        )

    snapshot = repository.get("runapi")
    assert snapshot.actual_cost_usd == Decimal("0.000000")
    assert snapshot.reserved_cost_usd == Decimal("1.500000")
