from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from platform_database import Database
from production_domain.models import DecisionRecord, LiveCanaryPermit, LiveCanaryUsage
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

_CANARY_LOCK = RLock()
_MONEY = Decimal("0.000001")
_CANARY_AUTHORIZATION_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "ai-director-platform/live-canary-permit-authorization/v1",
)
_CANARY_RECONCILIATION_NAMESPACE = uuid5(
    NAMESPACE_URL,
    "ai-director-platform/live-canary-usage-reconciliation/v1",
)


class LiveCanaryDenied(RuntimeError):
    pass


class LiveCanaryConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class CanaryReservation:
    usage_id: str
    permit_id: str
    provider: str
    model: str
    estimated_cost_usd: Decimal
    status: str
    replayed: bool


def _money(value: Decimal | str | float | int) -> Decimal:
    try:
        parsed = Decimal(str(value)).quantize(_MONEY)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("canary cost must be a decimal amount") from exc
    if parsed < 0:
        raise ValueError("canary cost cannot be negative")
    return parsed


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _new_permit(
    *,
    provider: str,
    model: str,
    max_requests: int,
    max_cost_usd: Decimal | str | float,
    expires_at: datetime,
    purpose: str,
) -> LiveCanaryPermit:
    provider = provider.strip()
    model = model.strip()
    purpose = purpose.strip()
    maximum = _money(max_cost_usd)
    normalized_expiry = _aware(expires_at)
    if not provider or not model or not purpose:
        raise ValueError("provider, model, and purpose are required")
    if max_requests < 1 or maximum <= 0:
        raise ValueError("canary limits must be positive")
    if normalized_expiry <= datetime.now(UTC):
        raise ValueError("canary permit must expire in the future")
    return LiveCanaryPermit(
        provider=provider,
        model=model,
        max_requests=max_requests,
        max_cost_usd=maximum,
        used_requests=0,
        reserved_cost_usd=Decimal("0"),
        actual_cost_usd=Decimal("0"),
        expires_at=normalized_expiry,
        purpose=purpose,
        status="ACTIVE",
        version=1,
    )


class LiveCanaryPermitService:
    """Durable request-and-cost hard stop for explicitly authorized live calls."""

    def __init__(self, database: Database):
        self.database = database

    def create(
        self,
        *,
        provider: str,
        model: str,
        max_requests: int,
        max_cost_usd: Decimal | str | float,
        expires_at: datetime,
        purpose: str,
    ) -> LiveCanaryPermit:
        permit = _new_permit(
            provider=provider,
            model=model,
            max_requests=max_requests,
            max_cost_usd=max_cost_usd,
            expires_at=expires_at,
            purpose=purpose,
        )
        with self.database.session() as session:
            session.add(permit)
            session.flush()
            return permit

    def create_authorized(
        self,
        *,
        provider: str,
        model: str,
        max_requests: int,
        max_cost_usd: Decimal | str | float,
        expires_at: datetime,
        purpose: str,
        explicit_confirmation: bool,
        actor_type: str,
        idempotency_key: str,
    ) -> tuple[LiveCanaryPermit, str, bool]:
        """Create an operational permit and its authorization audit atomically.

        This method only persists authorization state.  It deliberately has no
        provider registry or execution dependency, so granting a permit cannot
        itself cross a remote paid boundary.
        """

        if explicit_confirmation is not True:
            raise ValueError("explicit confirmation is required")
        if actor_type != "PLATFORM_API_KEY":
            raise ValueError("live canary permits require the PLATFORM_API_KEY actor")
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise ValueError("a bounded Idempotency-Key is required")
        permit = _new_permit(
            provider=provider,
            model=model,
            max_requests=max_requests,
            max_cost_usd=max_cost_usd,
            expires_at=expires_at,
            purpose=purpose,
        )
        request_facts = {
            "provider": permit.provider,
            "model": permit.model,
            "max_requests": permit.max_requests,
            "max_cost_usd": format(permit.max_cost_usd, "f"),
            "expires_at": _aware(permit.expires_at).isoformat(),
            "purpose": permit.purpose,
            "explicit_confirmation": True,
        }
        request_hash = hashlib.sha256(
            json.dumps(
                request_facts,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        idempotency_key_hash = hashlib.sha256(key.encode()).hexdigest()
        audit_id = str(uuid5(_CANARY_AUTHORIZATION_NAMESPACE, idempotency_key_hash))

        with _CANARY_LOCK:
            try:
                with self.database.session() as session:
                    replay = self._authorized_replay(
                        session,
                        audit_id=audit_id,
                        request_hash=request_hash,
                    )
                    if replay is not None:
                        return replay, audit_id, True
                    session.add(permit)
                    session.flush()
                    audit = DecisionRecord(
                        id=audit_id,
                        project_id=None,
                        shot_id=None,
                        decision_type="LIVE_CANARY_PERMIT_CREATED",
                        input_features={
                            **request_facts,
                            "permit_id": permit.id,
                            "request_hash": request_hash,
                            "idempotency_key_hash": idempotency_key_hash,
                            "server_actor": actor_type,
                        },
                        selected_action="CREATE_LIVE_CANARY_PERMIT",
                        reason_codes=["EXPLICIT_INTERNAL_CANARY_AUTHORIZATION"],
                        model_version="live-canary-permit-v1",
                        policy_version="live-canary-permit-v1",
                    )
                    session.add(audit)
                    session.flush()
                    return permit, audit.id, False
            except IntegrityError:
                # The deterministic audit primary key is the cross-process
                # uniqueness fence. A concurrent loser rolls back its permit,
                # then resolves the committed winner as a replay or conflict.
                with self.database.session() as session:
                    replay = self._authorized_replay(
                        session,
                        audit_id=audit_id,
                        request_hash=request_hash,
                    )
                    if replay is None:
                        raise
                    return replay, audit_id, True

    @staticmethod
    def _authorized_replay(
        session: Session,
        *,
        audit_id: str,
        request_hash: str,
    ) -> LiveCanaryPermit | None:
        audit = session.get(DecisionRecord, audit_id)
        if audit is None:
            return None
        features = audit.input_features if isinstance(audit.input_features, dict) else {}
        if (
            audit.decision_type != "LIVE_CANARY_PERMIT_CREATED"
            or audit.selected_action != "CREATE_LIVE_CANARY_PERMIT"
            or features.get("request_hash") != request_hash
        ):
            raise LiveCanaryConflict("Idempotency-Key was already used for different live canary facts")
        permit_id = features.get("permit_id")
        if not isinstance(permit_id, str):
            raise RuntimeError("live canary authorization audit is missing its permit")
        permit = session.get(LiveCanaryPermit, permit_id)
        if permit is None:
            raise RuntimeError("live canary authorization audit points to a missing permit")
        return permit

    def reserve(
        self,
        permit_id: str,
        *,
        provider: str,
        model: str,
        estimated_cost_usd: Decimal | str | float,
        idempotency_key: str,
    ) -> CanaryReservation:
        estimate = _money(estimated_cost_usd)
        key = idempotency_key.strip()
        if not key:
            raise ValueError("canary idempotency_key is required")
        with _CANARY_LOCK, self.database.session() as session:
            existing = session.scalar(
                select(LiveCanaryUsage).where(
                    LiveCanaryUsage.permit_id == permit_id,
                    LiveCanaryUsage.idempotency_key == key,
                )
            )
            if existing:
                self._assert_replay(existing, provider, model, estimate)
                return self._view(existing, replayed=True)
            permit = session.scalar(
                select(LiveCanaryPermit).where(LiveCanaryPermit.id == permit_id).with_for_update()
            )
            if permit is None:
                raise LiveCanaryDenied("live canary permit not found")
            now = datetime.now(UTC)
            if _aware(permit.expires_at) <= now:
                permit.status = "EXPIRED"
                raise LiveCanaryDenied("live canary permit expired")
            if permit.provider != provider or permit.model != model:
                raise LiveCanaryDenied("live canary permit does not match provider/model")
            if permit.used_requests >= permit.max_requests:
                permit.status = "EXHAUSTED"
                raise LiveCanaryDenied("live canary request limit reached")
            if permit.status != "ACTIVE":
                raise LiveCanaryDenied(f"live canary permit is {permit.status}")
            projected = _money(permit.actual_cost_usd + permit.reserved_cost_usd + estimate)
            if projected > _money(permit.max_cost_usd):
                permit.status = "EXHAUSTED"
                raise LiveCanaryDenied("live canary cost limit reached")
            usage = LiveCanaryUsage(
                permit_id=permit.id,
                idempotency_key=key,
                provider=provider,
                model=model,
                estimated_cost_usd=estimate,
                status="RESERVED",
            )
            permit.used_requests += 1
            permit.reserved_cost_usd = _money(permit.reserved_cost_usd + estimate)
            permit.version += 1
            if (
                permit.used_requests >= permit.max_requests
                or permit.actual_cost_usd + permit.reserved_cost_usd >= permit.max_cost_usd
            ):
                permit.status = "EXHAUSTED"
            session.add(usage)
            session.flush()
            return self._view(usage, replayed=False)

    def reserve_matching(
        self,
        *,
        provider: str,
        model: str,
        idempotency_key: str,
        estimated_cost_usd: Decimal | str | float | None = None,
    ) -> CanaryReservation:
        """Consume the oldest explicit active permit for a server-selected target.

        If a caller has no trustworthy quote, the entire remaining permit
        budget is held.  This is deliberately conservative: an unpriced live
        call may use one canary but can never fan out into several calls by
        pretending its estimate is zero.
        """

        key = idempotency_key.strip()
        if not key:
            raise ValueError("canary idempotency_key is required")
        now = datetime.now(UTC)
        with _CANARY_LOCK, self.database.session() as session:
            # The operation key is server-owned. Resolve it before selecting a
            # currently-active permit so a retry cannot consume a second permit
            # after the first permit became EXHAUSTED or was disabled.
            existing_rows = list(
                session.scalars(
                    select(LiveCanaryUsage).where(LiveCanaryUsage.idempotency_key == key).with_for_update()
                )
            )
            if len(existing_rows) > 1:
                raise LiveCanaryConflict(
                    "canary operation idempotency key is ambiguously bound to multiple permits"
                )
            if existing_rows:
                existing = existing_rows[0]
                if existing.provider != provider or existing.model != model:
                    raise LiveCanaryConflict(
                        "canary operation idempotency key was reused for another provider/model"
                    )
                if estimated_cost_usd is not None:
                    estimate = _money(estimated_cost_usd)
                    self._assert_replay(existing, provider, model, estimate)
                if existing.status != "RELEASED":
                    return self._view(existing, replayed=True)

                # A conclusively pre-boundary failure may retry the same logical
                # generation operation. Re-open the same usage row and charge
                # the same permit once; never create another usage/consumption.
                permit = session.scalar(
                    select(LiveCanaryPermit)
                    .where(LiveCanaryPermit.id == existing.permit_id)
                    .with_for_update()
                )
                if permit is None:
                    raise RuntimeError("canary permit disappeared")
                if _aware(permit.expires_at) <= now:
                    permit.status = "EXPIRED"
                    raise LiveCanaryDenied("live canary permit expired")
                if permit.status != "ACTIVE":
                    raise LiveCanaryDenied(f"live canary permit is {permit.status}")
                estimate = _money(existing.estimated_cost_usd)
                if permit.used_requests >= permit.max_requests:
                    permit.status = "EXHAUSTED"
                    raise LiveCanaryDenied("live canary request limit reached")
                projected = _money(permit.actual_cost_usd + permit.reserved_cost_usd + estimate)
                if projected > _money(permit.max_cost_usd):
                    permit.status = "EXHAUSTED"
                    raise LiveCanaryDenied("live canary cost limit reached")
                permit.used_requests += 1
                permit.reserved_cost_usd = _money(permit.reserved_cost_usd + estimate)
                permit.version += 1
                existing.status = "RESERVED"
                existing.actual_cost_usd = None
                existing.evidence_reference = None
                session.flush()
                return self._view(existing, replayed=True)

            permit = session.scalar(
                select(LiveCanaryPermit)
                .where(
                    LiveCanaryPermit.provider == provider,
                    LiveCanaryPermit.model == model,
                    LiveCanaryPermit.status == "ACTIVE",
                    LiveCanaryPermit.expires_at > now,
                )
                .order_by(LiveCanaryPermit.created_at, LiveCanaryPermit.id)
                .with_for_update()
            )
            if permit is None:
                permit = session.scalar(
                    select(LiveCanaryPermit)
                    .where(
                        LiveCanaryPermit.provider == provider,
                        LiveCanaryPermit.model == model,
                        LiveCanaryPermit.expires_at > now,
                    )
                    .order_by(LiveCanaryPermit.created_at, LiveCanaryPermit.id)
                    .with_for_update()
                )
                if permit is None:
                    raise LiveCanaryDenied(
                        "no active live canary permit matches the server-selected provider/model"
                    )
            # Another process may have committed this operation while this
            # transaction waited for the deterministic permit row lock.
            concurrent = session.scalar(
                select(LiveCanaryUsage).where(LiveCanaryUsage.idempotency_key == key).with_for_update()
            )
            if concurrent is not None:
                if concurrent.provider != provider or concurrent.model != model:
                    raise LiveCanaryConflict(
                        "canary operation idempotency key was reused for another provider/model"
                    )
                if estimated_cost_usd is not None:
                    self._assert_replay(
                        concurrent,
                        provider,
                        model,
                        _money(estimated_cost_usd),
                    )
                return self._view(concurrent, replayed=True)
            if permit.provider != provider or permit.model != model:
                raise LiveCanaryDenied("live canary permit does not match provider/model")
            if permit.used_requests >= permit.max_requests:
                permit.status = "EXHAUSTED"
                raise LiveCanaryDenied("live canary request limit reached")
            if permit.status != "ACTIVE":
                raise LiveCanaryDenied(f"live canary permit is {permit.status}")
            if estimated_cost_usd is None:
                estimate = _money(permit.max_cost_usd - permit.actual_cost_usd - permit.reserved_cost_usd)
                if estimate <= 0:
                    permit.status = "EXHAUSTED"
                    raise LiveCanaryDenied("live canary cost limit reached")
            else:
                estimate = _money(estimated_cost_usd)
            projected = _money(permit.actual_cost_usd + permit.reserved_cost_usd + estimate)
            if projected > _money(permit.max_cost_usd):
                permit.status = "EXHAUSTED"
                raise LiveCanaryDenied("live canary cost limit reached")
            usage = LiveCanaryUsage(
                permit_id=permit.id,
                idempotency_key=key,
                provider=provider,
                model=model,
                estimated_cost_usd=estimate,
                status="RESERVED",
            )
            permit.used_requests += 1
            permit.reserved_cost_usd = _money(permit.reserved_cost_usd + estimate)
            permit.version += 1
            if (
                permit.used_requests >= permit.max_requests
                or permit.actual_cost_usd + permit.reserved_cost_usd >= permit.max_cost_usd
            ):
                permit.status = "EXHAUSTED"
            session.add(usage)
            session.flush()
            return self._view(usage, replayed=False)

    def require_operation_boundary(
        self,
        *,
        provider: str,
        model: str,
        idempotency_key: str,
        allowed_statuses: frozenset[str] = frozenset({"UNCERTAIN"}),
        require_unexpired: bool = False,
        session: Session | None = None,
    ) -> CanaryReservation:
        """Assert the server-selected operation owns one durable canary usage.

        Passing the caller's transaction lets a provider boundary validate the
        canary and close its own paid-call fence without an intervening gap.
        """

        key = idempotency_key.strip()
        if not key:
            raise ValueError("canary idempotency_key is required")

        def resolve(active_session: Session) -> CanaryReservation:
            rows = list(
                active_session.scalars(
                    select(LiveCanaryUsage).where(LiveCanaryUsage.idempotency_key == key).with_for_update()
                )
            )
            if len(rows) != 1:
                raise LiveCanaryDenied("live generation operation has no unique durable canary usage")
            usage = rows[0]
            if usage.provider != provider or usage.model != model:
                raise LiveCanaryDenied(
                    "live generation canary does not match the server-selected provider/model"
                )
            if usage.status not in allowed_statuses:
                raise LiveCanaryDenied(f"live generation canary is not boundary-ready: {usage.status}")
            permit = active_session.scalar(
                select(LiveCanaryPermit).where(LiveCanaryPermit.id == usage.permit_id).with_for_update()
            )
            if permit is None:
                raise LiveCanaryDenied("live generation canary permit no longer exists")
            if permit.provider != provider or permit.model != model:
                raise LiveCanaryDenied(
                    "live generation permit does not match the server-selected provider/model"
                )
            # A successful reservation can itself exhaust a one-shot permit,
            # so EXHAUSTED remains valid for that already-owned operation.
            # No other permit state authorizes a new provider boundary.
            if permit.status not in {"ACTIVE", "EXHAUSTED"}:
                raise LiveCanaryDenied(f"live generation permit is {permit.status}")
            if require_unexpired and _aware(permit.expires_at) <= datetime.now(UTC):
                permit.status = "EXPIRED"
                raise LiveCanaryDenied("live generation permit expired before provider boundary")
            return self._view(usage, replayed=True)

        if session is not None:
            return resolve(session)
        with _CANARY_LOCK, self.database.session() as owned_session:
            return resolve(owned_session)

    def settle(
        self,
        usage_id: str,
        *,
        actual_cost_usd: Decimal | str | float,
        evidence_reference: str,
    ) -> CanaryReservation:
        actual = _money(actual_cost_usd)
        evidence = evidence_reference.strip()
        if not evidence:
            raise ValueError("canary settlement requires evidence_reference")
        with _CANARY_LOCK, self.database.session() as session:
            usage = session.scalar(
                select(LiveCanaryUsage).where(LiveCanaryUsage.id == usage_id).with_for_update()
            )
            if usage is None:
                raise LookupError("live canary usage not found")
            if usage.status == "SETTLED":
                if _money(usage.actual_cost_usd or 0) != actual or usage.evidence_reference != evidence:
                    raise LiveCanaryConflict("canary usage was settled with different evidence")
                return self._view(usage, replayed=True)
            if usage.status not in {"RESERVED", "UNCERTAIN"}:
                raise LiveCanaryConflict(f"cannot settle canary usage from {usage.status}")
            permit = session.scalar(
                select(LiveCanaryPermit).where(LiveCanaryPermit.id == usage.permit_id).with_for_update()
            )
            if permit is None:
                raise RuntimeError("canary permit disappeared")
            permit.reserved_cost_usd = _money(
                max(Decimal("0"), permit.reserved_cost_usd - usage.estimated_cost_usd)
            )
            permit.actual_cost_usd = _money(permit.actual_cost_usd + actual)
            usage.actual_cost_usd = actual
            usage.evidence_reference = evidence
            usage.status = "SETTLED"
            permit.version += 1
            if (
                permit.used_requests >= permit.max_requests
                or permit.actual_cost_usd + permit.reserved_cost_usd >= permit.max_cost_usd
            ):
                permit.status = "EXHAUSTED"
            return self._view(usage, replayed=False)

    def reconcile_uncertain(
        self,
        usage_id: str,
        *,
        action: str,
        actual_cost_usd: Decimal | str | float | None,
        idempotency_key: str,
        reason: str,
        evidence_reference: str,
        actor_type: str = "PLATFORM_API_KEY",
    ) -> tuple[CanaryReservation, str, bool]:
        """Close an UNCERTAIN usage with an operator's finding, and audit it.

        A usage goes UNCERTAIN the moment the request crosses the provider
        boundary, because from inside this process a timeout and a billed
        generation look identical. Only an operator reading the provider's own
        console can tell them apart, and until they do the permit keeps the
        whole estimate held in `reserved_cost_usd` — which is correct while the
        answer is unknown and wrong forever afterwards. A refused canary that is
        never reconciled goes on consuming the audit's global ceiling for
        attempts that cost nothing.

        `CONFIRM_PROVIDER_NOT_CREATED` records that finding: the attempt
        happened and stays counted against `used_requests`, and it settles at
        USD 0 because the provider created no job. `SETTLE_ACTUAL_COST` records
        the other one, at the figure the provider's own billing shows. Neither
        can be replayed into the other.
        """

        if action not in {"CONFIRM_PROVIDER_NOT_CREATED", "SETTLE_ACTUAL_COST"}:
            raise ValueError("unsupported live canary usage reconciliation action")
        if actor_type != "PLATFORM_API_KEY":
            raise ValueError("live canary reconciliation requires the PLATFORM_API_KEY actor")
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise ValueError("a bounded Idempotency-Key is required")
        detail = reason.strip()
        if not detail:
            raise ValueError("live canary reconciliation requires a reason")
        evidence = evidence_reference.strip()
        if not evidence:
            raise ValueError("live canary reconciliation requires evidence_reference")
        if action == "CONFIRM_PROVIDER_NOT_CREATED":
            if actual_cost_usd is not None and _money(actual_cost_usd) != Decimal("0"):
                raise ValueError("CONFIRM_PROVIDER_NOT_CREATED settles at zero cost")
            actual = Decimal("0").quantize(_MONEY)
        else:
            if actual_cost_usd is None:
                raise ValueError("SETTLE_ACTUAL_COST requires actual_cost_usd")
            actual = _money(actual_cost_usd)

        idempotency_key_hash = hashlib.sha256(key.encode()).hexdigest()
        audit_id = str(uuid5(_CANARY_RECONCILIATION_NAMESPACE, idempotency_key_hash))
        request_facts = {
            "usage_id": usage_id,
            "action": action,
            "actual_cost_usd": format(actual, "f"),
            "evidence_reference": evidence,
        }
        request_hash = hashlib.sha256(
            json.dumps(request_facts, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        with _CANARY_LOCK, self.database.session() as session:
            audit = session.get(DecisionRecord, audit_id)
            if audit is not None:
                features = audit.input_features if isinstance(audit.input_features, dict) else {}
                if (
                    audit.decision_type != "LIVE_CANARY_USAGE_RECONCILED"
                    or features.get("request_hash") != request_hash
                ):
                    raise LiveCanaryConflict(
                        "Idempotency-Key was already used for a different live canary finding"
                    )
                usage = session.scalar(select(LiveCanaryUsage).where(LiveCanaryUsage.id == usage_id))
                if usage is None:
                    raise RuntimeError("live canary reconciliation audit points to a missing usage")
                return self._view(usage, replayed=True), audit_id, True

            usage = session.scalar(
                select(LiveCanaryUsage).where(LiveCanaryUsage.id == usage_id).with_for_update()
            )
            if usage is None:
                raise LookupError("live canary usage not found")
            if usage.status != "UNCERTAIN":
                raise LiveCanaryConflict(
                    f"live canary reconciliation requires UNCERTAIN, got {usage.status}"
                )
            permit = session.scalar(
                select(LiveCanaryPermit).where(LiveCanaryPermit.id == usage.permit_id).with_for_update()
            )
            if permit is None:
                raise RuntimeError("canary permit disappeared")
            previous_status = usage.status
            permit.reserved_cost_usd = _money(
                max(Decimal("0"), permit.reserved_cost_usd - usage.estimated_cost_usd)
            )
            permit.actual_cost_usd = _money(permit.actual_cost_usd + actual)
            permit.version += 1
            usage.actual_cost_usd = actual
            usage.evidence_reference = evidence
            usage.status = "SETTLED"
            if (
                permit.used_requests >= permit.max_requests
                or permit.actual_cost_usd + permit.reserved_cost_usd >= permit.max_cost_usd
            ):
                permit.status = "EXHAUSTED"
            session.add(
                DecisionRecord(
                    id=audit_id,
                    project_id=None,
                    shot_id=None,
                    decision_type="LIVE_CANARY_USAGE_RECONCILED",
                    input_features={
                        **request_facts,
                        "permit_id": permit.id,
                        "provider": permit.provider,
                        "model": permit.model,
                        "previous_status": previous_status,
                        "reason": detail[:240],
                        "request_hash": request_hash,
                        "idempotency_key_hash": idempotency_key_hash,
                        "server_actor": actor_type,
                    },
                    selected_action=action,
                    reason_codes=["EXPLICIT_INTERNAL_CANARY_RECONCILIATION"],
                    model_version="live-canary-permit-v1",
                    policy_version="live-canary-permit-v1",
                )
            )
            session.flush()
            return self._view(usage, replayed=False), audit_id, False

    def mark_uncertain(self, usage_id: str, *, evidence_reference: str = "") -> CanaryReservation:
        with _CANARY_LOCK, self.database.session() as session:
            usage = session.scalar(
                select(LiveCanaryUsage).where(LiveCanaryUsage.id == usage_id).with_for_update()
            )
            if usage is None:
                raise LookupError("live canary usage not found")
            if usage.status == "UNCERTAIN":
                return self._view(usage, replayed=True)
            if usage.status != "RESERVED":
                raise LiveCanaryConflict(f"cannot mark canary usage uncertain from {usage.status}")
            usage.status = "UNCERTAIN"
            usage.evidence_reference = evidence_reference.strip() or None
            return self._view(usage, replayed=False)

    def release(self, usage_id: str) -> CanaryReservation:
        """Release only when no provider boundary was crossed."""

        with _CANARY_LOCK, self.database.session() as session:
            usage = session.scalar(
                select(LiveCanaryUsage).where(LiveCanaryUsage.id == usage_id).with_for_update()
            )
            if usage is None:
                raise LookupError("live canary usage not found")
            if usage.status == "RELEASED":
                return self._view(usage, replayed=True)
            if usage.status != "RESERVED":
                raise LiveCanaryConflict(f"cannot release canary usage from {usage.status}")
            permit = session.scalar(
                select(LiveCanaryPermit).where(LiveCanaryPermit.id == usage.permit_id).with_for_update()
            )
            if permit is None:
                raise RuntimeError("canary permit disappeared")
            permit.reserved_cost_usd = _money(
                max(Decimal("0"), permit.reserved_cost_usd - usage.estimated_cost_usd)
            )
            permit.used_requests = max(0, permit.used_requests - 1)
            permit.version += 1
            if permit.status == "EXHAUSTED" and _aware(permit.expires_at) > datetime.now(UTC):
                permit.status = "ACTIVE"
            usage.status = "RELEASED"
            return self._view(usage, replayed=False)

    def release_pre_boundary(
        self,
        usage_id: str,
        *,
        evidence_reference: str,
    ) -> CanaryReservation:
        """Release a prepared usage only with proof no provider boundary ran.

        GenerationGateway marks a usage UNCERTAIN before any possible live
        transport. A synchronous, conclusively local failure may then release
        that hold through this explicit method. Crashes and ambiguous failures
        remain UNCERTAIN and therefore fail closed.
        """

        evidence = evidence_reference.strip()
        if not evidence:
            raise ValueError("pre-boundary release requires evidence_reference")
        with _CANARY_LOCK, self.database.session() as session:
            usage = session.scalar(
                select(LiveCanaryUsage).where(LiveCanaryUsage.id == usage_id).with_for_update()
            )
            if usage is None:
                raise LookupError("live canary usage not found")
            if usage.status == "RELEASED":
                if usage.evidence_reference != evidence:
                    raise LiveCanaryConflict("canary usage was released with different pre-boundary evidence")
                return self._view(usage, replayed=True)
            if usage.status not in {"RESERVED", "UNCERTAIN"}:
                raise LiveCanaryConflict(f"cannot release pre-boundary canary usage from {usage.status}")
            permit = session.scalar(
                select(LiveCanaryPermit).where(LiveCanaryPermit.id == usage.permit_id).with_for_update()
            )
            if permit is None:
                raise RuntimeError("canary permit disappeared")
            permit.reserved_cost_usd = _money(
                max(Decimal("0"), permit.reserved_cost_usd - usage.estimated_cost_usd)
            )
            permit.used_requests = max(0, permit.used_requests - 1)
            permit.version += 1
            if permit.status == "EXHAUSTED" and _aware(permit.expires_at) > datetime.now(UTC):
                permit.status = "ACTIVE"
            usage.status = "RELEASED"
            usage.evidence_reference = evidence
            return self._view(usage, replayed=False)

    @staticmethod
    def _assert_replay(
        usage: LiveCanaryUsage,
        provider: str,
        model: str,
        estimate: Decimal,
    ) -> None:
        if usage.provider != provider or usage.model != model or _money(usage.estimated_cost_usd) != estimate:
            raise LiveCanaryConflict("canary idempotency key was reused with different facts")

    @staticmethod
    def _view(usage: LiveCanaryUsage, *, replayed: bool) -> CanaryReservation:
        return CanaryReservation(
            usage_id=usage.id,
            permit_id=usage.permit_id,
            provider=usage.provider,
            model=usage.model,
            estimated_cost_usd=_money(usage.estimated_cost_usd),
            status=usage.status,
            replayed=replayed,
        )


__all__ = [
    "CanaryReservation",
    "LiveCanaryConflict",
    "LiveCanaryDenied",
    "LiveCanaryPermitService",
]
