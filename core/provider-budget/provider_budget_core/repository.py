from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from threading import RLock

from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import DecisionRecord, ProviderBudget, ProviderBudgetUsage
from provider_sdk import (
    ProviderBudgetConflict,
    ProviderBudgetExceeded,
    ProviderBudgetReservation,
    ProviderBudgetSnapshot,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

MONEY_QUANTUM = Decimal("0.000001")
PROVIDER_BUDGET_RECONCILIATION = "PROVIDER_BUDGET_RECONCILIATION"
SETTLE_ACTUAL_COST = "SETTLE_ACTUAL_COST"
RELEASE_NO_REMOTE_CHARGE = "RELEASE_NO_REMOTE_CHARGE"

# PostgreSQL row locks are the production concurrency fence. Tests use SQLite,
# where SELECT FOR UPDATE is ignored, so one process-local lock keeps the same
# contract for concurrent internal API calls without weakening the DB fence.
_PROVIDER_BUDGET_TRANSITION_LOCK = RLock()


@dataclass(frozen=True)
class ProviderBudgetReconciliation:
    reservation: ProviderBudgetReservation
    budget: ProviderBudgetSnapshot
    previous_status: str
    action: str
    audit_decision_id: str
    replayed: bool


def _money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANTUM)


def _remaining(budget: ProviderBudget) -> Decimal:
    return _money(
        max(
            Decimal("0"),
            budget.credit_budget_usd - budget.actual_cost_usd - budget.reserved_cost_usd,
        )
    )


def _snapshot(budget: ProviderBudget) -> ProviderBudgetSnapshot:
    return ProviderBudgetSnapshot(
        provider=budget.provider,
        credit_budget_usd=_money(budget.credit_budget_usd),
        actual_cost_usd=_money(budget.actual_cost_usd),
        reserved_cost_usd=_money(budget.reserved_cost_usd),
        remaining_budget_usd=_remaining(budget),
        routing_enabled=bool(budget.routing_enabled and _remaining(budget) > 0),
    )


def _reservation(
    record: ProviderBudgetUsage,
    *,
    acquired: bool = True,
) -> ProviderBudgetReservation:
    return ProviderBudgetReservation(
        reservation_id=record.id,
        provider=record.provider,
        task_id=record.task_id,
        task_role=record.task_role,
        estimated_cost_usd=_money(record.estimated_cost_usd),
        actual_cost_usd=(_money(record.actual_cost_usd) if record.actual_cost_usd is not None else None),
        status=record.status,
        remaining_budget_usd=_money(record.remaining_budget_usd),
        created_at=record.created_at,
        updated_at=record.updated_at,
        acquired=acquired,
    )


class DatabaseProviderBudgetRepository:
    """Transactional provider-budget implementation for production routing.

    Reservations are idempotent per ``(provider, task_id)``. PostgreSQL uses
    row locks for settle/release, while the reserve path uses a conditional
    update so concurrent workers cannot oversubscribe the same budget.
    """

    def __init__(self, database: Database):
        self.database = database

    def ensure(self, provider: str, credit_budget_usd: Decimal) -> ProviderBudgetSnapshot:
        """Create the configured ceiling once without overwriting recorded spend."""

        provider = provider.strip()
        limit = _money(credit_budget_usd)
        if not provider or limit < 0:
            raise ValueError("provider and a non-negative credit budget are required")
        try:
            with self.database.session() as session:
                existing = session.scalar(select(ProviderBudget).where(ProviderBudget.provider == provider))
                if existing:
                    return _snapshot(existing)
                item = ProviderBudget(
                    provider=provider,
                    credit_budget_usd=limit,
                    actual_cost_usd=Decimal("0"),
                    reserved_cost_usd=Decimal("0"),
                    routing_enabled=limit > 0,
                )
                session.add(item)
                session.flush()
                return _snapshot(item)
        except IntegrityError:
            return self.get(provider)

    def get(self, provider: str) -> ProviderBudgetSnapshot:
        with self.database.session() as session:
            budget = session.scalar(select(ProviderBudget).where(ProviderBudget.provider == provider))
            if not budget:
                raise LookupError(f"provider budget is not configured: {provider}")
            return _snapshot(budget)

    def reserve(
        self,
        *,
        provider: str,
        task_id: str,
        task_role: str,
        estimated_cost_usd: Decimal,
        initial_status: str = "RESERVED",
    ) -> ProviderBudgetReservation:
        provider = provider.strip()
        task_id = task_id.strip()
        task_role = task_role.strip()
        estimate = _money(estimated_cost_usd)
        if not provider or not task_id or not task_role:
            raise ValueError("provider, task_id and task_role are required")
        if estimate <= 0:
            raise ValueError("estimated_cost_usd must be positive")
        if initial_status not in {"RESERVED", "UNCERTAIN"}:
            raise ValueError("initial provider budget status must be RESERVED or UNCERTAIN")
        try:
            return self._reserve_once(provider, task_id, task_role, estimate, initial_status)
        except IntegrityError:
            # A concurrent identical reservation may win the unique key after
            # our initial read. Re-read it as an idempotent replay.
            with self.database.session() as session:
                existing = session.scalar(
                    select(ProviderBudgetUsage).where(
                        ProviderBudgetUsage.provider == provider,
                        ProviderBudgetUsage.task_id == task_id,
                    )
                )
                if not existing:
                    raise
                self._validate_replay(existing, task_role, estimate)
                return _reservation(existing, acquired=False)

    def _reserve_once(
        self,
        provider: str,
        task_id: str,
        task_role: str,
        estimate: Decimal,
        initial_status: str,
    ) -> ProviderBudgetReservation:
        with self.database.session() as session:
            existing = session.scalar(
                select(ProviderBudgetUsage).where(
                    ProviderBudgetUsage.provider == provider,
                    ProviderBudgetUsage.task_id == task_id,
                )
            )
            if existing:
                self._validate_replay(existing, task_role, estimate)
                return _reservation(existing, acquired=False)

            result = session.execute(
                update(ProviderBudget)
                .where(
                    ProviderBudget.provider == provider,
                    ProviderBudget.routing_enabled.is_(True),
                    ProviderBudget.credit_budget_usd
                    - ProviderBudget.actual_cost_usd
                    - ProviderBudget.reserved_cost_usd
                    >= estimate,
                )
                .values(
                    reserved_cost_usd=ProviderBudget.reserved_cost_usd + estimate,
                    routing_enabled=(
                        ProviderBudget.credit_budget_usd
                        - ProviderBudget.actual_cost_usd
                        - ProviderBudget.reserved_cost_usd
                        - estimate
                        > 0
                    ),
                )
            )
            if affected_rows(result) != 1:
                existing = session.scalar(
                    select(ProviderBudgetUsage).where(
                        ProviderBudgetUsage.provider == provider,
                        ProviderBudgetUsage.task_id == task_id,
                    )
                )
                if existing:
                    self._validate_replay(existing, task_role, estimate)
                    return _reservation(existing, acquired=False)
                budget = session.scalar(select(ProviderBudget).where(ProviderBudget.provider == provider))
                if not budget:
                    raise LookupError(f"provider budget is not configured: {provider}")
                raise ProviderBudgetExceeded(f"{provider} budget exhausted; remaining={_remaining(budget)}")
            budget = session.scalar(select(ProviderBudget).where(ProviderBudget.provider == provider))
            if not budget:  # pragma: no cover - guarded by conditional update
                raise LookupError(f"provider budget is not configured: {provider}")
            record = ProviderBudgetUsage(
                budget_id=budget.id,
                provider=provider,
                task_id=task_id,
                task_role=task_role,
                estimated_cost_usd=estimate,
                actual_cost_usd=None,
                remaining_budget_usd=_remaining(budget),
                status=initial_status,
            )
            session.add(record)
            session.flush()
            return _reservation(record)

    @staticmethod
    def _validate_replay(
        record: ProviderBudgetUsage,
        task_role: str,
        estimate: Decimal,
    ) -> None:
        if record.task_role != task_role or _money(record.estimated_cost_usd) != estimate:
            raise ProviderBudgetConflict("task id was already reserved with different budget facts")

    def settle(
        self,
        reservation_id: str,
        *,
        actual_cost_usd: Decimal | None,
        status: str = "SETTLED",
    ) -> ProviderBudgetReservation:
        if status not in {"SETTLED", "UNCERTAIN"}:
            raise ValueError("settlement status must be SETTLED or UNCERTAIN")
        if status == "SETTLED" and actual_cost_usd is None:
            raise ValueError("settled provider usage requires actual_cost_usd")
        if status == "UNCERTAIN" and actual_cost_usd is not None:
            raise ValueError("uncertain provider usage cannot claim an actual cost")
        actual = _money(actual_cost_usd) if actual_cost_usd is not None else None
        if actual is not None and actual < 0:
            raise ValueError("actual_cost_usd cannot be negative")
        with _PROVIDER_BUDGET_TRANSITION_LOCK:
            with self.database.session() as session:
                record = session.scalar(
                    select(ProviderBudgetUsage)
                    .where(ProviderBudgetUsage.id == reservation_id)
                    .with_for_update()
                )
                if not record:
                    raise LookupError("provider budget reservation not found")
                recorded_actual = (
                    _money(record.actual_cost_usd) if record.actual_cost_usd is not None else None
                )
                if record.status == "SETTLED":
                    if status != "SETTLED" or recorded_actual != actual:
                        raise ProviderBudgetConflict("reservation was already settled differently")
                    return _reservation(record)
                if record.status == "UNCERTAIN" and status == "UNCERTAIN":
                    return _reservation(record)
                if record.status not in {"RESERVED", "UNCERTAIN"}:
                    raise ProviderBudgetConflict(f"cannot settle a {record.status} reservation")
                budget = session.scalar(
                    select(ProviderBudget).where(ProviderBudget.id == record.budget_id).with_for_update()
                )
                if not budget:
                    raise LookupError("provider budget not found")
                if status == "SETTLED":
                    assert actual is not None
                    budget.reserved_cost_usd = _money(
                        max(Decimal("0"), budget.reserved_cost_usd - record.estimated_cost_usd)
                    )
                    budget.actual_cost_usd = _money(budget.actual_cost_usd + actual)
                    record.actual_cost_usd = actual
                else:
                    # Keep the server estimate reserved until trusted provider
                    # billing evidence resolves the uncertain submission.
                    record.actual_cost_usd = None
                record.status = status
                remaining = _remaining(budget)
                budget.routing_enabled = remaining > 0
                record.remaining_budget_usd = remaining
                session.flush()
                return _reservation(record)

    def release(self, reservation_id: str) -> ProviderBudgetReservation:
        with self.database.session() as session:
            record = session.scalar(
                select(ProviderBudgetUsage).where(ProviderBudgetUsage.id == reservation_id).with_for_update()
            )
            if not record:
                raise LookupError("provider budget reservation not found")
            if record.status == "RELEASED":
                return _reservation(record)
            if record.status != "RESERVED":
                raise ProviderBudgetConflict(f"cannot release a {record.status} reservation")
            budget = session.scalar(
                select(ProviderBudget).where(ProviderBudget.id == record.budget_id).with_for_update()
            )
            if not budget:
                raise LookupError("provider budget not found")
            budget.reserved_cost_usd = _money(
                max(Decimal("0"), budget.reserved_cost_usd - record.estimated_cost_usd)
            )
            remaining = _remaining(budget)
            budget.routing_enabled = remaining > 0
            record.status = "RELEASED"
            record.remaining_budget_usd = remaining
            session.flush()
            return _reservation(record)

    def reconcile_uncertain(
        self,
        reservation_id: str,
        *,
        action: str,
        actual_cost_usd: Decimal | None,
        idempotency_key: str,
        reason: str,
        evidence_reference: str,
    ) -> ProviderBudgetReconciliation:
        """Resolve an uncertain provider charge from trusted billing evidence.

        This is intentionally independent from workspace credits and generation
        CostRecord accounting. Only the provider-level USD reservation and its
        append-only DecisionRecord audit are changed in this transaction.
        """

        reservation_id = reservation_id.strip()
        key = idempotency_key.strip()
        normalized_reason = reason.strip()
        normalized_evidence = evidence_reference.strip()
        if not reservation_id:
            raise ValueError("provider budget reservation id is required")
        if not 8 <= len(key) <= 200:
            raise ValueError("Idempotency-Key must contain 8 to 200 characters")
        if not 3 <= len(normalized_reason) <= 240:
            raise ValueError("reason must contain 3 to 240 characters")
        if not 3 <= len(normalized_evidence) <= 500:
            raise ValueError("evidence_reference must contain 3 to 500 characters")
        if action not in {SETTLE_ACTUAL_COST, RELEASE_NO_REMOTE_CHARGE}:
            raise ValueError("unsupported provider budget reconciliation action")
        actual = _money(actual_cost_usd) if actual_cost_usd is not None else None
        if action == SETTLE_ACTUAL_COST and actual is None:
            raise ValueError("SETTLE_ACTUAL_COST requires actual_cost_usd")
        if action == RELEASE_NO_REMOTE_CHARGE and actual is not None:
            raise ValueError("RELEASE_NO_REMOTE_CHARGE forbids actual_cost_usd")
        if actual is not None and actual < 0:
            raise ValueError("actual_cost_usd cannot be negative")

        facts = {
            "idempotency_key": key,
            "action": action,
            "actual_cost_usd": str(actual) if actual is not None else None,
            "reason": normalized_reason,
            "evidence_reference": normalized_evidence,
        }
        with _PROVIDER_BUDGET_TRANSITION_LOCK:
            with self.database.session() as session:
                record = session.scalar(
                    select(ProviderBudgetUsage)
                    .where(ProviderBudgetUsage.id == reservation_id)
                    .with_for_update()
                )
                if not record:
                    raise LookupError("provider budget reservation not found")

                audits = list(
                    session.scalars(
                        select(DecisionRecord)
                        .where(DecisionRecord.decision_type == PROVIDER_BUDGET_RECONCILIATION)
                        .order_by(DecisionRecord.created_at, DecisionRecord.id)
                    )
                )
                reservation_audits = [
                    audit
                    for audit in audits
                    if (audit.input_features or {}).get("provider_budget_reservation_id") == reservation_id
                ]
                matching_key = next(
                    (
                        audit
                        for audit in reservation_audits
                        if (audit.input_features or {}).get("idempotency_key") == key
                    ),
                    None,
                )
                if matching_key is not None:
                    self._validate_reconciliation_replay(record, matching_key, facts)
                    budget = session.scalar(
                        select(ProviderBudget).where(ProviderBudget.id == record.budget_id).with_for_update()
                    )
                    if not budget:
                        raise LookupError("provider budget not found")
                    return ProviderBudgetReconciliation(
                        reservation=_reservation(record),
                        budget=_snapshot(budget),
                        previous_status="UNCERTAIN",
                        action=action,
                        audit_decision_id=matching_key.id,
                        replayed=True,
                    )
                if reservation_audits:
                    raise ProviderBudgetConflict(
                        "provider budget reservation already reached a terminal state "
                        "under a different reconciliation decision"
                    )
                if record.status != "UNCERTAIN":
                    raise ProviderBudgetConflict(
                        f"provider budget reconciliation requires UNCERTAIN, got {record.status}"
                    )

                budget = session.scalar(
                    select(ProviderBudget).where(ProviderBudget.id == record.budget_id).with_for_update()
                )
                if not budget:
                    raise LookupError("provider budget not found")
                previous_status = record.status
                if _money(budget.reserved_cost_usd) < _money(record.estimated_cost_usd):
                    raise ProviderBudgetConflict(
                        "provider budget reserved total is below the uncertain reservation estimate"
                    )
                budget.reserved_cost_usd = _money(budget.reserved_cost_usd - record.estimated_cost_usd)
                if action == SETTLE_ACTUAL_COST:
                    assert actual is not None
                    budget.actual_cost_usd = _money(budget.actual_cost_usd + actual)
                    record.actual_cost_usd = actual
                    record.status = "SETTLED"
                else:
                    record.actual_cost_usd = None
                    record.status = "RELEASED"
                remaining = _remaining(budget)
                budget.routing_enabled = remaining > 0
                record.remaining_budget_usd = remaining

                audit = DecisionRecord(
                    project_id=None,
                    shot_id=None,
                    decision_type=PROVIDER_BUDGET_RECONCILIATION,
                    input_features={
                        "provider_budget_reservation_id": record.id,
                        "provider": record.provider,
                        "task_id": record.task_id,
                        "task_role": record.task_role,
                        "estimated_cost_usd": str(_money(record.estimated_cost_usd)),
                        "previous_status": previous_status,
                        "action": action,
                        "actual_cost_usd": facts["actual_cost_usd"],
                        "reason": normalized_reason,
                        "evidence_reference": normalized_evidence,
                        "explicit_confirmation": True,
                        "server_actor": "PLATFORM_API_KEY",
                        "idempotency_key": key,
                        "resulting_status": record.status,
                        "remaining_budget_usd": str(remaining),
                    },
                    selected_action=action,
                    reason_codes=[normalized_reason],
                    model_version="manual-provider-budget-reconcile-v1",
                    policy_version="provider-budget-v1",
                )
                session.add(audit)
                session.flush()
                return ProviderBudgetReconciliation(
                    reservation=_reservation(record),
                    budget=_snapshot(budget),
                    previous_status=previous_status,
                    action=action,
                    audit_decision_id=audit.id,
                    replayed=False,
                )

    @staticmethod
    def _validate_reconciliation_replay(
        record: ProviderBudgetUsage,
        audit: DecisionRecord,
        facts: dict[str, str | None],
    ) -> None:
        stored = audit.input_features or {}
        if audit.selected_action != facts["action"] or any(
            stored.get(name) != value for name, value in facts.items()
        ):
            raise ProviderBudgetConflict(
                "provider budget reconciliation idempotency key was used for different facts"
            )
        expected_status = "SETTLED" if facts["action"] == SETTLE_ACTUAL_COST else "RELEASED"
        expected_actual = facts["actual_cost_usd"]
        recorded_actual = str(_money(record.actual_cost_usd)) if record.actual_cost_usd is not None else None
        if record.status != expected_status or recorded_actual != expected_actual:
            raise ProviderBudgetConflict(
                "provider budget reconciliation audit and reservation state disagree"
            )

    def records(self, provider: str) -> list[ProviderBudgetReservation]:
        with self.database.session() as session:
            records = list(
                session.scalars(
                    select(ProviderBudgetUsage)
                    .where(ProviderBudgetUsage.provider == provider)
                    .order_by(ProviderBudgetUsage.created_at, ProviderBudgetUsage.id)
                )
            )
            return [_reservation(record) for record in records]


def snapshot_dict(value: ProviderBudgetSnapshot) -> dict[str, str | bool]:
    """JSON-safe audit view without lossy float conversion."""

    return {
        "provider": value.provider,
        "credit_budget_usd": str(value.credit_budget_usd),
        "actual_cost_usd": str(value.actual_cost_usd),
        "reserved_cost_usd": str(value.reserved_cost_usd),
        "remaining_budget_usd": str(value.remaining_budget_usd),
        "routing_enabled": value.routing_enabled,
    }


def reservation_dict(value: ProviderBudgetReservation) -> dict[str, str | None]:
    return {
        "reservation_id": value.reservation_id,
        "provider": value.provider,
        "task_id": value.task_id,
        "task_role": value.task_role,
        "estimated_cost_usd": str(value.estimated_cost_usd),
        "actual_cost_usd": (str(value.actual_cost_usd) if value.actual_cost_usd is not None else None),
        "status": value.status,
        "remaining_budget_usd": str(value.remaining_budget_usd),
        "created_at": _iso(value.created_at),
        "updated_at": _iso(value.updated_at),
    }


def _iso(value: datetime) -> str:
    return value.isoformat()
