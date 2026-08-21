from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from threading import RLock
from typing import Protocol

MONEY_QUANTUM = Decimal("0.000001")


class ProviderBudgetExceeded(RuntimeError):
    pass


class ProviderBudgetConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderBudgetSnapshot:
    provider: str
    credit_budget_usd: Decimal
    actual_cost_usd: Decimal
    reserved_cost_usd: Decimal
    remaining_budget_usd: Decimal
    routing_enabled: bool


@dataclass(frozen=True)
class ProviderBudgetReservation:
    reservation_id: str
    provider: str
    task_id: str
    task_role: str
    estimated_cost_usd: Decimal
    actual_cost_usd: Decimal | None
    status: str
    remaining_budget_usd: Decimal
    created_at: datetime
    updated_at: datetime
    acquired: bool = True


class ProviderBudgetRepository(Protocol):
    """Atomic persistence boundary for provider spend.

    Production implementations must enforce unique `(provider, task_id)` and
    serialize reserve/settle transitions. This lets the DB-backed repository
    land independently without changing provider adapters.
    """

    def get(self, provider: str) -> ProviderBudgetSnapshot: ...

    def reserve(
        self,
        *,
        provider: str,
        task_id: str,
        task_role: str,
        estimated_cost_usd: Decimal,
        initial_status: str = "RESERVED",
    ) -> ProviderBudgetReservation: ...

    def settle(
        self,
        reservation_id: str,
        *,
        actual_cost_usd: Decimal | None,
        status: str = "SETTLED",
    ) -> ProviderBudgetReservation: ...

    def release(self, reservation_id: str) -> ProviderBudgetReservation: ...

    def records(self, provider: str) -> list[ProviderBudgetReservation]: ...


class InMemoryProviderBudgetRepository:
    """Thread-safe contract implementation for tests and single-process mock mode."""

    def __init__(self, limits: dict[str, Decimal | int | float | str]):
        self._limits = {name: _money(value) for name, value in limits.items()}
        self._records: dict[str, ProviderBudgetReservation] = {}
        self._task_index: dict[tuple[str, str], str] = {}
        self._lock = RLock()

    def _snapshot_unlocked(self, provider: str) -> ProviderBudgetSnapshot:
        limit = self._limits.get(provider, Decimal("0"))
        provider_records = [record for record in self._records.values() if record.provider == provider]
        actual = sum(
            (record.actual_cost_usd or Decimal("0"))
            for record in provider_records
            if record.status == "SETTLED"
        )
        reserved = sum(
            record.estimated_cost_usd
            for record in provider_records
            if record.status in {"RESERVED", "UNCERTAIN"}
        )
        remaining = max(Decimal("0"), limit - actual - reserved)
        return ProviderBudgetSnapshot(
            provider=provider,
            credit_budget_usd=_money(limit),
            actual_cost_usd=_money(actual),
            reserved_cost_usd=_money(reserved),
            remaining_budget_usd=_money(remaining),
            routing_enabled=remaining > 0,
        )

    def get(self, provider: str) -> ProviderBudgetSnapshot:
        with self._lock:
            return self._snapshot_unlocked(provider)

    def reserve(
        self,
        *,
        provider: str,
        task_id: str,
        task_role: str,
        estimated_cost_usd: Decimal,
        initial_status: str = "RESERVED",
    ) -> ProviderBudgetReservation:
        estimate = _money(estimated_cost_usd)
        if initial_status not in {"RESERVED", "UNCERTAIN"}:
            raise ValueError("initial provider budget status must be RESERVED or UNCERTAIN")
        if not task_id.strip() or not task_role.strip():
            raise ValueError("task_id and task_role are required")
        if estimate <= 0:
            raise ValueError("estimated_cost_usd must be positive")
        with self._lock:
            existing_id = self._task_index.get((provider, task_id))
            if existing_id:
                existing = self._records[existing_id]
                if existing.task_role != task_role or existing.estimated_cost_usd != estimate:
                    raise ProviderBudgetConflict("task id was already reserved with different budget facts")
                return replace(existing, acquired=False)
            snapshot = self._snapshot_unlocked(provider)
            if not snapshot.routing_enabled or estimate > snapshot.remaining_budget_usd:
                raise ProviderBudgetExceeded(
                    f"{provider} budget exhausted; remaining={snapshot.remaining_budget_usd}"
                )
            now = datetime.now(UTC)
            record = ProviderBudgetReservation(
                reservation_id=str(uuid.uuid4()),
                provider=provider,
                task_id=task_id,
                task_role=task_role,
                estimated_cost_usd=estimate,
                actual_cost_usd=None,
                status=initial_status,
                remaining_budget_usd=_money(snapshot.remaining_budget_usd - estimate),
                created_at=now,
                updated_at=now,
            )
            self._records[record.reservation_id] = record
            self._task_index[(provider, task_id)] = record.reservation_id
            return record

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
        with self._lock:
            record = self._records[reservation_id]
            if record.status == "SETTLED":
                if record.actual_cost_usd != actual or record.status != status:
                    raise ProviderBudgetConflict("reservation was already settled differently")
                return record
            if record.status == "UNCERTAIN":
                if status == "UNCERTAIN":
                    return record
                # A trusted synchronous response may arrive after the durable
                # paid-call marker. The same lock serializes it against manual
                # reconciliation; exactly one transition wins.
                provisional = replace(
                    record,
                    actual_cost_usd=actual,
                    status="SETTLED",
                    updated_at=datetime.now(UTC),
                )
                self._records[reservation_id] = provisional
                remaining = self._snapshot_unlocked(record.provider).remaining_budget_usd
                settled = replace(provisional, remaining_budget_usd=remaining)
                self._records[reservation_id] = settled
                return settled
            if record.status != "RESERVED":
                raise ProviderBudgetConflict(f"cannot settle a {record.status} reservation")
            provisional = replace(
                record,
                actual_cost_usd=actual,
                status=status,
                updated_at=datetime.now(UTC),
            )
            self._records[reservation_id] = provisional
            remaining = self._snapshot_unlocked(record.provider).remaining_budget_usd
            settled = replace(provisional, remaining_budget_usd=remaining)
            self._records[reservation_id] = settled
            return settled

    def release(self, reservation_id: str) -> ProviderBudgetReservation:
        with self._lock:
            record = self._records[reservation_id]
            if record.status == "RELEASED":
                return record
            if record.status != "RESERVED":
                raise ProviderBudgetConflict(f"cannot release a {record.status} reservation")
            provisional = replace(record, status="RELEASED", updated_at=datetime.now(UTC))
            self._records[reservation_id] = provisional
            remaining = self._snapshot_unlocked(record.provider).remaining_budget_usd
            released = replace(provisional, remaining_budget_usd=remaining)
            self._records[reservation_id] = released
            return released

    def records(self, provider: str) -> list[ProviderBudgetReservation]:
        with self._lock:
            return sorted(
                (record for record in self._records.values() if record.provider == provider),
                key=lambda record: (record.created_at, record.reservation_id),
            )


def _money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANTUM)


__all__ = [
    "InMemoryProviderBudgetRepository",
    "ProviderBudgetConflict",
    "ProviderBudgetExceeded",
    "ProviderBudgetRepository",
    "ProviderBudgetReservation",
    "ProviderBudgetSnapshot",
]
