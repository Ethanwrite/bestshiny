"""The automatic production budget: quote-bound spend authorizations under a breaker.

Until this existed every live provider call — a user's generation on a model
that had already proven itself, a director turn, an embedding — needed an
operator to mint a ``LiveCanaryPermit`` by hand, and an unquoted media call
held that permit's whole remaining budget. That is the right fence for a
model's *first* live call, which is a spending decision an operator should
take. It is the wrong fence for the thousandth.

This module is the second fence. A model earns it by closing one
permit-fenced canary loop (``live_canary_status = VERIFIED_LIVE``); from then
on each generation on it gets, in the same transaction as its workspace
credit reservation, one single-use ``GenerationSpendAuthorization`` bound to
the workspace, the job, the provider and the model, whose USD ceiling is the
server quote and nothing else. Above every authorization sit two
``ProductionBudgetLedger`` rows per UTC day — the platform and the provider —
reserved by conditional update before any money can move, so that a wrong
price, a free credit grant or a burst of concurrency trips a breaker instead
of an invoice.

Model-role calls (director, prompt refiner, embeddings) get the same
authorization per call, sized by the token estimate; the product decision on
who pays for them is recorded in ``docs/OPEN_ISSUES.md`` §1.18: they are
covered by the plan's quota, not by credits and not by the generation price.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    DecisionRecord,
    GenerationSpendAuthorization,
    ProductionBudgetLedger,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .canary import LiveSpendDenied

PLATFORM_SCOPE = "PLATFORM"
PROVIDER_SCOPE = "PROVIDER"
PLATFORM_SCOPE_KEY = "platform"
WINDOW_SECONDS = 86_400

GENERATION_KIND = "GENERATION"
MODEL_ROLE_KIND = "MODEL_ROLE"

FENCE_PENDING = "PENDING"
FENCE_PRODUCTION = "PRODUCTION"
FENCE_CANARY = "CANARY"

SETTLE_ACTUAL_COST = "SETTLE_ACTUAL_COST"
RELEASE_NO_REMOTE_CHARGE = "RELEASE_NO_REMOTE_CHARGE"
SPEND_AUTHORIZATION_RECONCILED = "SPEND_AUTHORIZATION_RECONCILED"

SOURCE_VERIFIED_PROVIDER = "VERIFIED_PROVIDER"
SOURCE_TOKENS_LIST = "TOKENS_LIST"
SOURCE_ESTIMATED_QUOTE = "ESTIMATED_QUOTE"
SOURCE_RECONCILED_MANUAL = "RECONCILED_MANUAL"

_MONEY = Decimal("0.000001")
_ZERO = Decimal("0").quantize(_MONEY)
# PostgreSQL row locks are the production concurrency fence. SQLite ignores
# SELECT FOR UPDATE, so one process-local lock keeps the same contract for the
# offline suite and for concurrent internal API calls in one process.
_BUDGET_LOCK = RLock()
_RECONCILIATION_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "ai-director-platform/spend-authorization-reconciliation/v1",
)


class ProductionBudgetExceeded(LiveSpendDenied):
    """The platform or provider breaker has no room for this reservation."""


class SpendAuthorizationDenied(LiveSpendDenied):
    """No usable authorization covers this operation."""


class SpendAuthorizationConflict(RuntimeError):
    """An authorization was replayed or transitioned with different facts."""


def _money(value: Decimal | str | float | int) -> Decimal:
    try:
        parsed = Decimal(str(value)).quantize(_MONEY)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("spend amount must be a decimal amount") from exc
    if parsed < 0:
        raise ValueError("spend amount cannot be negative")
    return parsed


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def window_start_for(now: datetime, window_seconds: int = WINDOW_SECONDS) -> datetime:
    """The start of the budget window containing ``now`` — UTC midnight for a day."""

    epoch = int(_aware(now).timestamp())
    return datetime.fromtimestamp(epoch - epoch % window_seconds, UTC)


@dataclass(frozen=True)
class ProductionBudgetPolicy:
    """The operator's ceilings, read once from settings."""

    platform_limit_usd: Decimal
    provider_limits_usd: Mapping[str, Decimal]
    window_seconds: int = WINDOW_SECONDS

    def __post_init__(self) -> None:
        # Money is quantized once, here, so every ledger row and every report
        # carries the same six decimals whatever the operator typed.
        object.__setattr__(self, "platform_limit_usd", _money(self.platform_limit_usd))
        object.__setattr__(
            self,
            "provider_limits_usd",
            {name.strip(): _money(value) for name, value in dict(self.provider_limits_usd).items()},
        )
        if int(self.window_seconds) <= 0:
            raise ValueError("production budget window must be positive")

    @property
    def enabled(self) -> bool:
        return self.platform_limit_usd > 0

    def provider_limit(self, provider: str) -> Decimal:
        """A provider's own ceiling, never above the platform's."""

        own = self.provider_limits_usd.get(provider.strip())
        if own is None:
            return self.platform_limit_usd
        return min(own, self.platform_limit_usd)

    @staticmethod
    def parse_provider_limits(value: str) -> dict[str, Decimal]:
        limits: dict[str, Decimal] = {}
        for group in value.split(","):
            provider, separator, amount = group.partition("=")
            if not separator:
                if group.strip():
                    raise ValueError(
                        "PRODUCTION_BUDGET_PROVIDER_USD_PER_DAY entries must be provider=usd pairs"
                    )
                continue
            name = provider.strip()
            if not name:
                raise ValueError("PRODUCTION_BUDGET_PROVIDER_USD_PER_DAY has an entry with no provider")
            parsed = _money(amount.strip())
            limits[name] = parsed
        return limits

    @classmethod
    def from_settings(cls, settings: Any) -> ProductionBudgetPolicy:
        platform = _money(getattr(settings, "production_budget_platform_usd_per_day", Decimal("0")))
        providers = cls.parse_provider_limits(
            str(getattr(settings, "production_budget_provider_usd_per_day", "") or "")
        )
        return cls(platform_limit_usd=platform, provider_limits_usd=providers)

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "window_seconds": self.window_seconds,
            "platform_limit_usd": format(self.platform_limit_usd, "f"),
            "provider_limits_usd": {
                name: format(self.provider_limit(name), "f") for name in sorted(self.provider_limits_usd)
            },
        }


@dataclass(frozen=True)
class SpendAuthorizationView:
    id: str
    operation_key: str
    kind: str
    generation_job_id: str | None
    workspace_id: str | None
    project_id: str | None
    model_role: str | None
    provider: str
    model: str
    max_cost_usd: Decimal
    reserved_cost_usd: Decimal
    actual_cost_usd: Decimal | None
    quoted_credits: int
    pricing_version: str
    status: str
    fence: str
    settlement_source: str | None
    evidence_reference: str | None
    replayed: bool

    @property
    def overran_quote(self) -> bool:
        """A settled figure above the ceiling: the price table, not the fence, was wrong."""

        return self.actual_cost_usd is not None and self.actual_cost_usd > self.max_cost_usd


@dataclass(frozen=True)
class ProductionBudgetWindow:
    scope: str
    scope_key: str
    window_start: datetime
    window_end: datetime
    limit_usd: Decimal
    reserved_usd: Decimal
    actual_usd: Decimal
    remaining_usd: Decimal
    tripped: bool


def authorization_dict(value: SpendAuthorizationView) -> dict[str, Any]:
    """JSON-safe view without lossy float conversion."""

    return {
        "id": value.id,
        "operation_key": value.operation_key,
        "kind": value.kind,
        "generation_job_id": value.generation_job_id,
        "workspace_id": value.workspace_id,
        "project_id": value.project_id,
        "model_role": value.model_role,
        "provider": value.provider,
        "model": value.model,
        "max_cost_usd": format(value.max_cost_usd, "f"),
        "reserved_cost_usd": format(value.reserved_cost_usd, "f"),
        "actual_cost_usd": (
            format(value.actual_cost_usd, "f") if value.actual_cost_usd is not None else None
        ),
        "quoted_credits": value.quoted_credits,
        "pricing_version": value.pricing_version,
        "status": value.status,
        "fence": value.fence,
        "settlement_source": value.settlement_source,
        "evidence_reference": value.evidence_reference,
        "overran_quote": value.overran_quote,
    }


def window_dict(value: ProductionBudgetWindow) -> dict[str, Any]:
    return {
        "scope": value.scope,
        "scope_key": value.scope_key,
        "window_start": value.window_start.isoformat(),
        "window_end": value.window_end.isoformat(),
        "limit_usd": format(value.limit_usd, "f"),
        "reserved_usd": format(value.reserved_usd, "f"),
        "actual_usd": format(value.actual_usd, "f"),
        "remaining_usd": format(value.remaining_usd, "f"),
        "tripped": value.tripped,
    }


def _remaining(ledger: ProductionBudgetLedger) -> Decimal:
    return _money(max(Decimal("0"), ledger.limit_usd - ledger.reserved_usd - ledger.actual_usd))


class ProductionBudgetService:
    """Reserve, fence, settle and reconcile live spend under the operator's ceilings."""

    version = "production-budget-v1"

    def __init__(
        self,
        database: Database,
        policy: ProductionBudgetPolicy,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.database = database
        self.policy = policy
        self.clock = clock or (lambda: datetime.now(UTC))

    @property
    def enabled(self) -> bool:
        return self.policy.enabled

    # ------------------------------------------------------------- ledgers

    def _ledger_for_update(
        self,
        session: Session,
        *,
        scope: str,
        scope_key: str,
        limit_usd: Decimal,
        now: datetime,
    ) -> ProductionBudgetLedger:
        start = window_start_for(now, self.policy.window_seconds)
        statement = (
            select(ProductionBudgetLedger)
            .where(
                ProductionBudgetLedger.scope == scope,
                ProductionBudgetLedger.scope_key == scope_key,
                ProductionBudgetLedger.window_start == start,
            )
            .with_for_update()
        )
        ledger = session.scalar(statement)
        if ledger is None:
            try:
                with session.begin_nested():
                    ledger = ProductionBudgetLedger(
                        scope=scope,
                        scope_key=scope_key,
                        window_start=start,
                        window_seconds=self.policy.window_seconds,
                        limit_usd=_money(limit_usd),
                        reserved_usd=Decimal("0"),
                        actual_usd=Decimal("0"),
                        version=1,
                    )
                    session.add(ledger)
                    session.flush([ledger])
            except IntegrityError:
                # A concurrent request created this window first; lock its row.
                ledger = session.scalar(statement)
                if ledger is None:  # pragma: no cover - the unique key just fired.
                    raise
        # The ceiling is the operator's current number, not the one in force
        # when the window happened to be opened. Spend is never touched here.
        if _money(ledger.limit_usd) != _money(limit_usd):
            ledger.limit_usd = _money(limit_usd)
            ledger.version += 1
            session.flush([ledger])
        return ledger

    @staticmethod
    def _reserve_on_ledger(session: Session, ledger: ProductionBudgetLedger, amount: Decimal) -> None:
        result = session.execute(
            update(ProductionBudgetLedger)
            .where(
                ProductionBudgetLedger.id == ledger.id,
                ProductionBudgetLedger.reserved_usd + ProductionBudgetLedger.actual_usd + amount
                <= ProductionBudgetLedger.limit_usd,
            )
            .values(
                reserved_usd=ProductionBudgetLedger.reserved_usd + amount,
                version=ProductionBudgetLedger.version + 1,
            )
        )
        session.expire(ledger)
        if affected_rows(result) != 1:
            label = "platform" if ledger.scope == PLATFORM_SCOPE else f"provider {ledger.scope_key}"
            raise ProductionBudgetExceeded(
                f"{label} production budget is exhausted for this window: "
                f"requested USD {amount}, remaining USD {_remaining(ledger)}"
            )

    def _reserve_window(
        self,
        session: Session,
        *,
        provider: str,
        amount: Decimal,
        now: datetime,
    ) -> tuple[ProductionBudgetLedger, ProductionBudgetLedger]:
        # Always platform first, then provider: one lock order, no deadlocks.
        platform = self._ledger_for_update(
            session,
            scope=PLATFORM_SCOPE,
            scope_key=PLATFORM_SCOPE_KEY,
            limit_usd=self.policy.platform_limit_usd,
            now=now,
        )
        provider_ledger = self._ledger_for_update(
            session,
            scope=PROVIDER_SCOPE,
            scope_key=provider,
            limit_usd=self.policy.provider_limit(provider),
            now=now,
        )
        self._reserve_on_ledger(session, platform, amount)
        self._reserve_on_ledger(session, provider_ledger, amount)
        return platform, provider_ledger

    @staticmethod
    def _adjust_ledgers(
        session: Session,
        authorization: GenerationSpendAuthorization,
        *,
        release_reserved: Decimal,
        add_actual: Decimal,
    ) -> None:
        for ledger_id in (authorization.platform_ledger_id, authorization.provider_ledger_id):
            ledger = session.scalar(
                select(ProductionBudgetLedger).where(ProductionBudgetLedger.id == ledger_id).with_for_update()
            )
            if ledger is None:
                raise RuntimeError("production budget ledger disappeared under an authorization")
            ledger.reserved_usd = _money(max(Decimal("0"), ledger.reserved_usd - release_reserved))
            ledger.actual_usd = _money(ledger.actual_usd + add_actual)
            ledger.version += 1

    # ------------------------------------------------------ authorizations

    def authorize_generation_in_session(
        self,
        session: Session,
        *,
        operation_key: str,
        generation_job_id: str,
        workspace_id: str | None,
        project_id: str | None,
        provider: str,
        model: str,
        max_cost_usd: Decimal | str | float,
        quoted_credits: int,
        pricing_version: str,
    ) -> SpendAuthorizationView:
        """Bind one authorization to a job inside the job-creating transaction.

        Runs in the caller's session on purpose: the credit reservation, the
        job row and this authorization commit or roll back together, so a
        tripped breaker leaves no job behind and no credits held.
        """

        if not self.enabled:
            raise RuntimeError("the production budget is not enabled")
        return self._authorize(
            session,
            operation_key=operation_key,
            kind=GENERATION_KIND,
            generation_job_id=generation_job_id,
            workspace_id=workspace_id,
            project_id=project_id,
            model_role=None,
            provider=provider,
            model=model,
            max_cost_usd=max_cost_usd,
            quoted_credits=quoted_credits,
            pricing_version=pricing_version,
        )

    def authorize_operation(
        self,
        *,
        operation_key: str,
        provider: str,
        model: str,
        max_cost_usd: Decimal | str | float,
        kind: str = MODEL_ROLE_KIND,
        model_role: str | None = None,
        project_id: str | None = None,
        workspace_id: str | None = None,
        pricing_version: str = "",
    ) -> SpendAuthorizationView:
        """One authorization for a jobless operation — a director turn, an embedding."""

        if not self.enabled:
            raise RuntimeError("the production budget is not enabled")
        with _BUDGET_LOCK, self.database.session() as session:
            return self._authorize(
                session,
                operation_key=operation_key,
                kind=kind,
                generation_job_id=None,
                workspace_id=workspace_id,
                project_id=project_id,
                model_role=model_role,
                provider=provider,
                model=model,
                max_cost_usd=max_cost_usd,
                quoted_credits=0,
                pricing_version=pricing_version,
            )

    def _authorize(
        self,
        session: Session,
        *,
        operation_key: str,
        kind: str,
        generation_job_id: str | None,
        workspace_id: str | None,
        project_id: str | None,
        model_role: str | None,
        provider: str,
        model: str,
        max_cost_usd: Decimal | str | float,
        quoted_credits: int,
        pricing_version: str,
    ) -> SpendAuthorizationView:
        key = operation_key.strip()
        provider = provider.strip()
        model = model.strip()
        if not key or not provider or not model:
            raise ValueError("operation_key, provider and model are required")
        if kind not in {GENERATION_KIND, MODEL_ROLE_KIND}:
            raise ValueError("unsupported spend authorization kind")
        amount = _money(max_cost_usd)
        if amount <= 0:
            raise SpendAuthorizationDenied(
                "live spend requires a positive server-owned USD quote; the request carries none"
            )
        with _BUDGET_LOCK:
            existing = session.scalar(
                select(GenerationSpendAuthorization)
                .where(GenerationSpendAuthorization.operation_key == key)
                .with_for_update()
            )
            if existing is not None:
                self._assert_replay(existing, provider, model, amount)
                return self._view(existing, replayed=True)
            now = self.clock()
            platform, provider_ledger = self._reserve_window(
                session, provider=provider, amount=amount, now=now
            )
            row = GenerationSpendAuthorization(
                operation_key=key,
                kind=kind,
                workspace_id=workspace_id,
                project_id=project_id,
                generation_job_id=generation_job_id,
                model_role=model_role,
                provider=provider,
                model=model,
                max_cost_usd=amount,
                reserved_cost_usd=amount,
                actual_cost_usd=None,
                quoted_credits=max(0, int(quoted_credits)),
                pricing_version=pricing_version[:80],
                status="RESERVED",
                fence=FENCE_PENDING,
                platform_ledger_id=platform.id,
                provider_ledger_id=provider_ledger.id,
                version=1,
            )
            session.add(row)
            session.flush([row])
            return self._view(row, replayed=False)

    def find_operation(
        self,
        operation_key: str,
        *,
        session: Session | None = None,
    ) -> SpendAuthorizationView | None:
        key = operation_key.strip()
        if not key:
            return None

        def resolve(active: Session) -> SpendAuthorizationView | None:
            row = active.scalar(
                select(GenerationSpendAuthorization).where(GenerationSpendAuthorization.operation_key == key)
            )
            return self._view(row, replayed=True) if row is not None else None

        if session is not None:
            return resolve(session)
        with self.database.session() as owned:
            return resolve(owned)

    def require_operation(
        self,
        *,
        operation_key: str,
        provider: str,
        model: str,
        allowed_statuses: frozenset[str] = frozenset({"UNCERTAIN"}),
        session: Session | None = None,
    ) -> SpendAuthorizationView:
        """Assert the server-selected operation owns one authorization in an allowed state."""

        key = operation_key.strip()
        if not key:
            raise ValueError("operation_key is required")

        def resolve(active: Session) -> SpendAuthorizationView:
            row = active.scalar(
                select(GenerationSpendAuthorization)
                .where(GenerationSpendAuthorization.operation_key == key)
                .with_for_update()
            )
            if row is None:
                raise SpendAuthorizationDenied("live operation has no spend authorization")
            if row.provider != provider or row.model != model:
                raise SpendAuthorizationDenied(
                    "spend authorization does not match the server-selected provider/model"
                )
            if row.status not in allowed_statuses:
                raise SpendAuthorizationDenied(f"spend authorization is not boundary-ready: {row.status}")
            return self._view(row, replayed=True)

        if session is not None:
            return resolve(session)
        with _BUDGET_LOCK, self.database.session() as owned:
            return resolve(owned)

    def prepare_boundary(
        self,
        authorization_id: str,
        *,
        provider: str,
        model: str,
        fence: str,
        evidence_reference: str,
    ) -> SpendAuthorizationView:
        """Mark the authorization UNCERTAIN before any transport, recording which fence ran.

        RESERVED moves to UNCERTAIN. RELEASED — a conclusively local failure
        handed the reservation back — is re-reserved against the *current*
        window (the breaker may refuse now) and then moves to UNCERTAIN.
        UNCERTAIN and SETTLED are returned as replays for the caller to refuse:
        a paid boundary may already have been crossed, and only an operator
        can say whether it was.
        """

        if fence not in {FENCE_PRODUCTION, FENCE_CANARY}:
            raise ValueError("fence must be PRODUCTION or CANARY")
        evidence = evidence_reference.strip()
        if not evidence:
            raise ValueError("boundary preparation requires evidence_reference")
        with _BUDGET_LOCK, self.database.session() as session:
            row = self._row_for_update(session, authorization_id)
            if row.provider != provider or row.model != model:
                raise SpendAuthorizationDenied(
                    "spend authorization does not match the server-selected provider/model"
                )
            if row.status in {"UNCERTAIN", "SETTLED"}:
                return self._view(row, replayed=True)
            if row.status == "RELEASED":
                platform, provider_ledger = self._reserve_window(
                    session, provider=row.provider, amount=_money(row.max_cost_usd), now=self.clock()
                )
                row.platform_ledger_id = platform.id
                row.provider_ledger_id = provider_ledger.id
                row.reserved_cost_usd = _money(row.max_cost_usd)
            elif row.status != "RESERVED":
                raise SpendAuthorizationConflict(f"cannot prepare a paid boundary from {row.status}")
            row.status = "UNCERTAIN"
            row.fence = fence
            row.evidence_reference = evidence[:500]
            row.version += 1
            session.flush([row])
            return self._view(row, replayed=False)

    def release_pre_boundary(
        self,
        authorization_id: str,
        *,
        evidence_reference: str,
    ) -> SpendAuthorizationView:
        """Hand the reservation back, only with proof no provider boundary ran."""

        evidence = evidence_reference.strip()
        if not evidence:
            raise ValueError("pre-boundary release requires evidence_reference")
        with _BUDGET_LOCK, self.database.session() as session:
            row = self._row_for_update(session, authorization_id)
            if row.status == "RELEASED":
                return self._view(row, replayed=True)
            if row.status not in {"RESERVED", "UNCERTAIN"}:
                raise SpendAuthorizationConflict(f"cannot release a spend authorization from {row.status}")
            self._adjust_ledgers(
                session,
                row,
                release_reserved=_money(row.reserved_cost_usd),
                add_actual=Decimal("0"),
            )
            row.reserved_cost_usd = _ZERO
            row.status = "RELEASED"
            row.evidence_reference = evidence[:500]
            row.version += 1
            session.flush([row])
            return self._view(row, replayed=False)

    def settle(
        self,
        authorization_id: str,
        *,
        actual_cost_usd: Decimal | str | float | None,
        evidence_reference: str,
        source: str,
    ) -> SpendAuthorizationView:
        """Replace the hold with what the call cost.

        ``actual_cost_usd=None`` settles at the ceiling itself, recorded as
        ``ESTIMATED_QUOTE``: most video providers report no cost figure in the
        poll result, and a breaker that waited for one would hold every
        finished generation's reservation until an operator typed it in. The
        quote is at least the list price with the service margin on top, so
        settling at it never understates the platform's exposure.
        """

        if source not in {
            SOURCE_VERIFIED_PROVIDER,
            SOURCE_TOKENS_LIST,
            SOURCE_ESTIMATED_QUOTE,
            SOURCE_RECONCILED_MANUAL,
        }:
            raise ValueError("unsupported settlement source")
        evidence = evidence_reference.strip()
        if not evidence:
            raise ValueError("settlement requires evidence_reference")
        if actual_cost_usd is None and source != SOURCE_ESTIMATED_QUOTE:
            raise ValueError(f"{source} settlement requires actual_cost_usd")
        with _BUDGET_LOCK, self.database.session() as session:
            row = self._row_for_update(session, authorization_id)
            amount = _money(actual_cost_usd) if actual_cost_usd is not None else _money(row.max_cost_usd)
            if row.status == "SETTLED":
                if _money(row.actual_cost_usd or 0) != amount or row.settlement_source != source:
                    raise SpendAuthorizationConflict("spend authorization was settled with different facts")
                return self._view(row, replayed=True)
            if row.status not in {"RESERVED", "UNCERTAIN"}:
                raise SpendAuthorizationConflict(f"cannot settle a spend authorization from {row.status}")
            self._adjust_ledgers(
                session,
                row,
                release_reserved=_money(row.reserved_cost_usd),
                add_actual=amount,
            )
            row.reserved_cost_usd = _ZERO
            row.actual_cost_usd = amount
            row.settlement_source = source
            row.evidence_reference = evidence[:500]
            row.status = "SETTLED"
            row.version += 1
            session.flush([row])
            return self._view(row, replayed=False)

    def reconcile_uncertain(
        self,
        authorization_id: str,
        *,
        action: str,
        actual_cost_usd: Decimal | str | float | None,
        idempotency_key: str,
        reason: str,
        evidence_reference: str,
        actor_type: str = "PLATFORM_API_KEY",
    ) -> tuple[SpendAuthorizationView, str, bool]:
        """Close an UNCERTAIN authorization with an operator's finding, audited.

        Deliberately independent of the workspace credit reconciliation: the
        user's refund and the platform's provider spend are two facts. A
        platform fault after the boundary refunds the user and still cost the
        platform money; this is where that second fact is recorded.
        """

        if action not in {SETTLE_ACTUAL_COST, RELEASE_NO_REMOTE_CHARGE}:
            raise ValueError("unsupported spend authorization reconciliation action")
        if actor_type != "PLATFORM_API_KEY":
            raise ValueError("spend authorization reconciliation requires the PLATFORM_API_KEY actor")
        key = idempotency_key.strip()
        if not 8 <= len(key) <= 200:
            raise ValueError("Idempotency-Key must contain 8 to 200 characters")
        detail = reason.strip()
        if not 3 <= len(detail) <= 240:
            raise ValueError("reason must contain 3 to 240 characters")
        evidence = evidence_reference.strip()
        if not 3 <= len(evidence) <= 500:
            raise ValueError("evidence_reference must contain 3 to 500 characters")
        actual = _money(actual_cost_usd) if actual_cost_usd is not None else None
        if action == SETTLE_ACTUAL_COST and actual is None:
            raise ValueError("SETTLE_ACTUAL_COST requires actual_cost_usd")
        if action == RELEASE_NO_REMOTE_CHARGE and actual is not None:
            raise ValueError("RELEASE_NO_REMOTE_CHARGE forbids actual_cost_usd")

        idempotency_key_hash = hashlib.sha256(key.encode()).hexdigest()
        audit_id = str(uuid5(_RECONCILIATION_NAMESPACE, idempotency_key_hash))
        request_facts = {
            "authorization_id": authorization_id,
            "action": action,
            "actual_cost_usd": format(actual, "f") if actual is not None else None,
            "evidence_reference": evidence,
        }
        request_hash = hashlib.sha256(
            json.dumps(request_facts, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        with _BUDGET_LOCK, self.database.session() as session:
            audit = session.get(DecisionRecord, audit_id)
            if audit is not None:
                features = audit.input_features if isinstance(audit.input_features, dict) else {}
                if (
                    audit.decision_type != SPEND_AUTHORIZATION_RECONCILED
                    or features.get("request_hash") != request_hash
                ):
                    raise SpendAuthorizationConflict(
                        "Idempotency-Key was already used for a different spend authorization finding"
                    )
                row = session.get(GenerationSpendAuthorization, authorization_id)
                if row is None:
                    raise RuntimeError("spend authorization reconciliation audit points to a missing row")
                return self._view(row, replayed=True), audit_id, True

            row = self._row_for_update(session, authorization_id)
            if row.status != "UNCERTAIN":
                raise SpendAuthorizationConflict(
                    f"spend authorization reconciliation requires UNCERTAIN, got {row.status}"
                )
            previous_status = row.status
            if action == SETTLE_ACTUAL_COST:
                assert actual is not None
                self._adjust_ledgers(
                    session, row, release_reserved=_money(row.reserved_cost_usd), add_actual=actual
                )
                row.actual_cost_usd = actual
                row.status = "SETTLED"
            else:
                self._adjust_ledgers(
                    session, row, release_reserved=_money(row.reserved_cost_usd), add_actual=Decimal("0")
                )
                row.actual_cost_usd = None
                row.status = "RELEASED"
            row.reserved_cost_usd = _ZERO
            row.settlement_source = SOURCE_RECONCILED_MANUAL
            row.evidence_reference = evidence
            row.version += 1
            session.add(
                DecisionRecord(
                    id=audit_id,
                    project_id=None,
                    shot_id=None,
                    decision_type=SPEND_AUTHORIZATION_RECONCILED,
                    input_features={
                        **request_facts,
                        "operation_key": row.operation_key,
                        "generation_job_id": row.generation_job_id,
                        "workspace_id": row.workspace_id,
                        "provider": row.provider,
                        "model": row.model,
                        "max_cost_usd": format(_money(row.max_cost_usd), "f"),
                        "previous_status": previous_status,
                        "resulting_status": row.status,
                        "reason": detail,
                        "request_hash": request_hash,
                        "idempotency_key_hash": idempotency_key_hash,
                        "server_actor": actor_type,
                        "explicit_confirmation": True,
                    },
                    selected_action=action,
                    reason_codes=["EXPLICIT_INTERNAL_SPEND_RECONCILIATION"],
                    model_version=self.version,
                    policy_version=self.version,
                )
            )
            session.flush()
            return self._view(row, replayed=False), audit_id, False

    # ------------------------------------------------------------- reading

    def windows(self, *, now: datetime | None = None) -> list[ProductionBudgetWindow]:
        """The current window's platform row and every provider row it has."""

        moment = now or self.clock()
        start = window_start_for(moment, self.policy.window_seconds)
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(ProductionBudgetLedger)
                    .where(ProductionBudgetLedger.window_start == start)
                    .order_by(ProductionBudgetLedger.scope, ProductionBudgetLedger.scope_key)
                )
            )
        seen = {(row.scope, row.scope_key) for row in rows}
        views = [self._window_view(row) for row in rows]
        # Configured ceilings with no spend yet still deserve a line: an
        # operator reading the snapshot should see every ceiling in force.
        if (PLATFORM_SCOPE, PLATFORM_SCOPE_KEY) not in seen:
            views.insert(
                0,
                self._empty_window(PLATFORM_SCOPE, PLATFORM_SCOPE_KEY, self.policy.platform_limit_usd, start),
            )
        for provider in sorted(self.policy.provider_limits_usd):
            if (PROVIDER_SCOPE, provider) not in seen:
                views.append(
                    self._empty_window(PROVIDER_SCOPE, provider, self.policy.provider_limit(provider), start)
                )
        return views

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        return {
            "version": self.version,
            "policy": self.policy.describe(),
            "windows": [window_dict(item) for item in self.windows(now=now)],
        }

    def list_authorizations(
        self,
        *,
        generation_job_id: str | None = None,
        operation_key: str | None = None,
        workspace_id: str | None = None,
        provider: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[SpendAuthorizationView]:
        statement = select(GenerationSpendAuthorization)
        if generation_job_id:
            statement = statement.where(GenerationSpendAuthorization.generation_job_id == generation_job_id)
        if operation_key:
            statement = statement.where(GenerationSpendAuthorization.operation_key == operation_key)
        if workspace_id:
            statement = statement.where(GenerationSpendAuthorization.workspace_id == workspace_id)
        if provider:
            statement = statement.where(GenerationSpendAuthorization.provider == provider)
        if status:
            statement = statement.where(GenerationSpendAuthorization.status == status)
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    statement.order_by(
                        GenerationSpendAuthorization.created_at.desc(),
                        GenerationSpendAuthorization.id.desc(),
                    ).limit(max(1, min(int(limit), 200)))
                )
            )
            return [self._view(row, replayed=True) for row in rows]

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _row_for_update(session: Session, authorization_id: str) -> GenerationSpendAuthorization:
        row = session.scalar(
            select(GenerationSpendAuthorization)
            .where(GenerationSpendAuthorization.id == authorization_id)
            .with_for_update()
        )
        if row is None:
            raise LookupError("spend authorization not found")
        return row

    @staticmethod
    def _assert_replay(
        row: GenerationSpendAuthorization,
        provider: str,
        model: str,
        amount: Decimal,
    ) -> None:
        if row.provider != provider or row.model != model or _money(row.max_cost_usd) != amount:
            raise SpendAuthorizationConflict(
                "spend authorization operation key was reused with different facts"
            )

    @staticmethod
    def _view(row: GenerationSpendAuthorization, *, replayed: bool) -> SpendAuthorizationView:
        return SpendAuthorizationView(
            id=row.id,
            operation_key=row.operation_key,
            kind=row.kind,
            generation_job_id=row.generation_job_id,
            workspace_id=row.workspace_id,
            project_id=row.project_id,
            model_role=row.model_role,
            provider=row.provider,
            model=row.model,
            max_cost_usd=_money(row.max_cost_usd),
            reserved_cost_usd=_money(row.reserved_cost_usd),
            actual_cost_usd=(_money(row.actual_cost_usd) if row.actual_cost_usd is not None else None),
            quoted_credits=row.quoted_credits,
            pricing_version=row.pricing_version,
            status=row.status,
            fence=row.fence,
            settlement_source=row.settlement_source,
            evidence_reference=row.evidence_reference,
            replayed=replayed,
        )

    def _window_view(self, ledger: ProductionBudgetLedger) -> ProductionBudgetWindow:
        start = _aware(ledger.window_start)
        remaining = _remaining(ledger)
        return ProductionBudgetWindow(
            scope=ledger.scope,
            scope_key=ledger.scope_key,
            window_start=start,
            window_end=start + timedelta(seconds=ledger.window_seconds),
            limit_usd=_money(ledger.limit_usd),
            reserved_usd=_money(ledger.reserved_usd),
            actual_usd=_money(ledger.actual_usd),
            remaining_usd=remaining,
            tripped=remaining <= 0,
        )

    def _empty_window(
        self, scope: str, scope_key: str, limit_usd: Decimal, start: datetime
    ) -> ProductionBudgetWindow:
        limit = _money(limit_usd)
        return ProductionBudgetWindow(
            scope=scope,
            scope_key=scope_key,
            window_start=start,
            window_end=start + timedelta(seconds=self.policy.window_seconds),
            limit_usd=limit,
            reserved_usd=_ZERO,
            actual_usd=_ZERO,
            remaining_usd=limit,
            tripped=limit <= 0,
        )


__all__ = [
    "FENCE_CANARY",
    "FENCE_PENDING",
    "FENCE_PRODUCTION",
    "GENERATION_KIND",
    "MODEL_ROLE_KIND",
    "PLATFORM_SCOPE",
    "PLATFORM_SCOPE_KEY",
    "PROVIDER_SCOPE",
    "RELEASE_NO_REMOTE_CHARGE",
    "SETTLE_ACTUAL_COST",
    "SOURCE_ESTIMATED_QUOTE",
    "SOURCE_RECONCILED_MANUAL",
    "SOURCE_TOKENS_LIST",
    "SOURCE_VERIFIED_PROVIDER",
    "SPEND_AUTHORIZATION_RECONCILED",
    "ProductionBudgetExceeded",
    "ProductionBudgetPolicy",
    "ProductionBudgetService",
    "ProductionBudgetWindow",
    "SpendAuthorizationConflict",
    "SpendAuthorizationDenied",
    "SpendAuthorizationView",
    "authorization_dict",
    "window_dict",
    "window_start_for",
]
