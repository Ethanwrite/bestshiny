from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from platform_shared import affected_rows
from production_domain.models import (
    CostRecord,
    GenerationJob,
    Project,
    Workspace,
    WorkspaceCreditEntry,
    WorkspaceCreditEvent,
    utcnow,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session


class InsufficientWorkspaceCredits(PermissionError):
    pass


class WorkspaceCreditConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceCreditBalance:
    workspace_id: str | None
    project_id: str
    plan_tier: str
    balance: int | None
    starter_grant: int = 50


@dataclass(frozen=True)
class WorkspaceCreditCharge:
    """Compatibility result name for a credit reservation."""

    applied: bool
    replayed: bool
    entry_id: str | None
    credits: int
    balance_after: int | None
    status: str | None = None


@dataclass(frozen=True)
class WorkspaceCreditTransition:
    applied: bool
    replayed: bool
    entry_id: str | None
    previous_status: str | None
    status: str | None
    reserved_credits: int
    settled_credits: int
    refunded_credits: int
    balance_after: int | None


ReconcileAction = Literal["SETTLE_RESERVED", "REFUND_RESERVED"]


class WorkspaceCreditService:
    """Transactional Free-plan reservation, settlement, refund and reconciliation.

    ``Workspace.credit_balance`` is available credit, so reserving decreases it
    immediately. A reservation remains held while generation is running. Only a
    successful terminal result settles it; an explicitly pre-submit terminal
    result refunds it. Any ambiguous paid-provider outcome stays held in
    ``RECONCILIATION_REQUIRED`` until an internal, audited decision resolves it.
    """

    starter_grant = 50
    pricing_version = "workspace-credits-v2"
    statuses = frozenset({"RESERVED", "SETTLED", "REFUNDED", "RECONCILIATION_REQUIRED"})

    @staticmethod
    def balance_in_session(session: Session, project_id: str) -> WorkspaceCreditBalance:
        project = session.get(Project, project_id)
        if not project:
            raise LookupError("project not found")
        if not project.workspace_id:
            return WorkspaceCreditBalance(None, project.id, "ALL", None)
        workspace = session.get(Workspace, project.workspace_id)
        if not workspace:
            raise LookupError("project workspace not found")
        if workspace.status != "ACTIVE" or project.status != "ACTIVE":
            raise InsufficientWorkspaceCredits("workspace or project is not active")
        return WorkspaceCreditBalance(
            workspace.id,
            project.id,
            workspace.plan_tier,
            workspace.credit_balance,
        )

    def reserve_generation(
        self,
        session: Session,
        job: GenerationJob,
        *,
        idempotency_key: str,
        credits: int,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceCreditCharge:
        if credits < 1 or credits > 100_000:
            raise ValueError("generation credits must be between 1 and 100000")
        key = idempotency_key.strip()
        if not key:
            raise ValueError("generation credit idempotency key is required")
        balance = self.balance_in_session(session, job.project_id)
        if balance.workspace_id is None or balance.plan_tier != "FREE":
            return WorkspaceCreditCharge(False, False, None, 0, balance.balance, None)

        existing = session.scalar(
            select(WorkspaceCreditEntry).where(
                WorkspaceCreditEntry.project_id == job.project_id,
                WorkspaceCreditEntry.idempotency_key == key,
            )
        )
        if existing:
            self._validate_reservation_replay(existing, job, credits)
            return WorkspaceCreditCharge(
                True,
                True,
                existing.id,
                existing.credits,
                existing.balance_after,
                existing.status,
            )

        reserved = session.execute(
            update(Workspace)
            .where(
                Workspace.id == balance.workspace_id,
                Workspace.status == "ACTIVE",
                Workspace.plan_tier == "FREE",
                Workspace.credit_balance >= credits,
            )
            .values(credit_balance=Workspace.credit_balance - credits)
        )
        if affected_rows(reserved) != 1:
            current = session.get(Workspace, balance.workspace_id)
            if current is not None:
                session.refresh(current, ["credit_balance"])
            available = current.credit_balance if current else 0
            raise InsufficientWorkspaceCredits(
                f"insufficient workspace credits: required={credits}, available={available}"
            )
        workspace = session.get(Workspace, balance.workspace_id)
        if workspace is None:  # pragma: no cover - guarded by the conditional update.
            raise LookupError("workspace disappeared while reserving credits")
        session.refresh(workspace, ["credit_balance"])
        now = utcnow()
        entry = WorkspaceCreditEntry(
            workspace_id=workspace.id,
            project_id=job.project_id,
            generation_job_id=job.id,
            idempotency_key=key,
            credits=credits,
            balance_after=workspace.credit_balance,
            settled_credits=0,
            refunded_credits=0,
            status="RESERVED",
            reason="GENERATION_RESERVED",
            version=1,
            reserved_at=now,
            metadata_json={
                "provider": job.provider,
                "model": job.model,
                "generation_type": job.generation_type,
                "request_hash": job.request_hash,
                "pricing_version": self.pricing_version,
                **dict(metadata or {}),
            },
        )
        session.add(entry)
        session.flush([entry])
        self._record_event(
            session,
            entry,
            event_key="reserve",
            event_type="RESERVED",
            credits=credits,
            balance_delta=-credits,
            balance_after=workspace.credit_balance,
            reason="GENERATION_RESERVED",
        )
        return WorkspaceCreditCharge(
            True,
            False,
            entry.id,
            entry.credits,
            entry.balance_after,
            entry.status,
        )

    # Kept as a narrow compatibility alias for callers outside this repository.
    def charge_generation(self, *args: Any, **kwargs: Any) -> WorkspaceCreditCharge:
        return self.reserve_generation(*args, **kwargs)

    def record_submission_boundary(
        self,
        session: Session,
        job: GenerationJob,
        *,
        attempt: int,
    ) -> WorkspaceCreditTransition:
        entry = self._entry_for_job(session, job, require_for_free=True)
        if entry is None:
            return self._not_applied()
        if entry.status != "RESERVED":
            raise WorkspaceCreditConflict(
                f"cannot start provider submission from credit state {entry.status}"
            )
        event_key = f"provider-boundary:{attempt}"
        replayed = self._event_exists(session, entry.id, event_key)
        if not replayed:
            current_balance = self._workspace_balance(session, entry)
            self._record_event(
                session,
                entry,
                event_key=event_key,
                event_type="PROVIDER_SUBMISSION_STARTED",
                credits=entry.credits,
                balance_delta=0,
                balance_after=current_balance,
                reason="PAID_PROVIDER_BOUNDARY",
                metadata={"attempt": attempt},
            )
        return self._snapshot(entry, previous_status=entry.status, replayed=replayed)

    def record_submission_confirmed(
        self,
        session: Session,
        job: GenerationJob,
        *,
        attempt: int,
        provider_job_id: str,
    ) -> WorkspaceCreditTransition:
        entry = self._entry_for_job(session, job, require_for_free=True)
        if entry is None:
            return self._not_applied()
        if entry.status not in {"RESERVED", "SETTLED", "RECONCILIATION_REQUIRED"}:
            raise WorkspaceCreditConflict(
                f"cannot confirm provider submission from credit state {entry.status}"
            )
        event_key = f"provider-confirmed:{attempt}"
        replayed = self._event_exists(session, entry.id, event_key)
        if not replayed:
            current_balance = self._workspace_balance(session, entry)
            self._record_event(
                session,
                entry,
                event_key=event_key,
                event_type="PROVIDER_SUBMISSION_CONFIRMED",
                credits=entry.credits,
                balance_delta=0,
                balance_after=current_balance,
                reason="PROVIDER_JOB_ID_CONFIRMED",
                metadata={"attempt": attempt, "provider_job_id": provider_job_id},
            )
        return self._snapshot(entry, previous_status=entry.status, replayed=replayed)

    def settle_generation(
        self,
        session: Session,
        job: GenerationJob,
        *,
        reason: str = "GENERATION_COMPLETED",
    ) -> WorkspaceCreditTransition:
        return self._settle(
            session,
            job,
            allowed_statuses={"RESERVED", "RECONCILIATION_REQUIRED"},
            event_key="settle",
            event_type="SETTLED",
            reason=reason,
            actor_type="SYSTEM",
        )

    def refund_generation(
        self,
        session: Session,
        job: GenerationJob,
        *,
        reason: str,
    ) -> WorkspaceCreditTransition:
        return self._refund(
            session,
            job,
            allowed_statuses={"RESERVED"},
            event_key="refund",
            event_type="REFUNDED",
            reason=reason,
            actor_type="SYSTEM",
        )

    def require_reconciliation(
        self,
        session: Session,
        job: GenerationJob,
        *,
        reason: str,
    ) -> WorkspaceCreditTransition:
        entry = self._entry_for_job(session, job, require_for_free=True)
        if entry is None:
            return self._not_applied()
        if entry.status == "RECONCILIATION_REQUIRED":
            return self._snapshot(entry, previous_status=entry.status, replayed=True)
        if entry.status == "SETTLED":
            # A trusted manual decision may settle an accepted request before
            # a late provider response or terminal job update arrives. Later
            # ambiguous transport handling must not reopen that wallet fact.
            return self._snapshot(entry, previous_status=entry.status, replayed=True)
        if entry.status != "RESERVED":
            raise WorkspaceCreditConflict(f"cannot require reconciliation from credit state {entry.status}")
        previous_status = entry.status
        expected_version = entry.version
        now = utcnow()
        result = session.execute(
            update(WorkspaceCreditEntry)
            .where(
                WorkspaceCreditEntry.id == entry.id,
                WorkspaceCreditEntry.status == "RESERVED",
                WorkspaceCreditEntry.version == expected_version,
            )
            .values(
                status="RECONCILIATION_REQUIRED",
                reason="RECONCILIATION_REQUIRED",
                reconciliation_required_at=now,
                reconciliation_reason=reason[:240],
                version=WorkspaceCreditEntry.version + 1,
                updated_at=now,
            )
        )
        if affected_rows(result) != 1:
            return self._resolve_transition_race(
                session,
                entry.id,
                desired_status="RECONCILIATION_REQUIRED",
                previous_status=previous_status,
            )
        session.expire(entry)
        session.refresh(entry)
        entry.balance_after = self._workspace_balance(session, entry)
        self._record_event(
            session,
            entry,
            event_key="reconciliation-required",
            event_type="RECONCILIATION_REQUIRED",
            credits=entry.credits,
            balance_delta=0,
            balance_after=entry.balance_after,
            reason=reason,
        )
        return self._snapshot(entry, previous_status=previous_status, replayed=False)

    def reconcile_generation(
        self,
        session: Session,
        job: GenerationJob,
        *,
        action: ReconcileAction,
        idempotency_key: str,
        reason: str,
        evidence_reference: str | None = None,
        actor_type: str = "PLATFORM_API_KEY",
    ) -> WorkspaceCreditTransition:
        key = idempotency_key.strip()
        if not key or len(key) > 120:
            raise ValueError("credit reconciliation idempotency key must be 1 to 120 characters")
        if action not in {"SETTLE_RESERVED", "REFUND_RESERVED"}:
            raise ValueError("unsupported credit reconciliation action")
        entry = self._entry_for_job(session, job, require_for_free=True)
        if entry is None:
            raise LookupError("generation has no workspace credit reservation")
        event_key = f"manual:{key}"
        existing_event = session.scalar(
            select(WorkspaceCreditEvent).where(
                WorkspaceCreditEvent.credit_entry_id == entry.id,
                WorkspaceCreditEvent.event_key == event_key,
            )
        )
        expected_type = "RECONCILED_SETTLED" if action == "SETTLE_RESERVED" else "RECONCILED_REFUNDED"
        desired_status = "SETTLED" if action == "SETTLE_RESERVED" else "REFUNDED"
        if existing_event:
            if existing_event.event_type != expected_type:
                raise WorkspaceCreditConflict(
                    "credit reconciliation idempotency key was used for a different action"
                )
            session.refresh(entry)
            replay_previous = str((existing_event.metadata_json or {}).get("previous_status") or entry.status)
            return self._snapshot(entry, previous_status=replay_previous, replayed=True)
        if entry.status == desired_status:
            raise WorkspaceCreditConflict(
                "credit reservation already reached this terminal state under a different decision"
            )
        if entry.status != "RECONCILIATION_REQUIRED":
            raise WorkspaceCreditConflict(
                f"credit reconciliation requires RECONCILIATION_REQUIRED, got {entry.status}"
            )
        metadata: dict[str, Any] = {"previous_status": "RECONCILIATION_REQUIRED"}
        if evidence_reference:
            metadata["evidence_reference"] = evidence_reference
        if action == "SETTLE_RESERVED":
            return self._settle(
                session,
                job,
                allowed_statuses={"RECONCILIATION_REQUIRED"},
                event_key=event_key,
                event_type=expected_type,
                reason=reason,
                actor_type=actor_type,
                metadata=metadata,
            )
        return self._refund(
            session,
            job,
            allowed_statuses={"RECONCILIATION_REQUIRED"},
            event_key=event_key,
            event_type=expected_type,
            reason=reason,
            actor_type=actor_type,
            metadata=metadata,
        )

    def entry_for_job_in_session(
        self,
        session: Session,
        generation_job_id: str,
    ) -> WorkspaceCreditEntry | None:
        return session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == generation_job_id)
        )

    @staticmethod
    def entries_in_session(session: Session, workspace_id: str) -> list[WorkspaceCreditEntry]:
        return list(
            session.scalars(
                select(WorkspaceCreditEntry)
                .where(WorkspaceCreditEntry.workspace_id == workspace_id)
                .order_by(WorkspaceCreditEntry.created_at, WorkspaceCreditEntry.id)
            )
        )

    @staticmethod
    def events_in_session(session: Session, workspace_id: str) -> list[WorkspaceCreditEvent]:
        return list(
            session.scalars(
                select(WorkspaceCreditEvent)
                .where(WorkspaceCreditEvent.workspace_id == workspace_id)
                .order_by(WorkspaceCreditEvent.created_at, WorkspaceCreditEvent.id)
            )
        )

    @staticmethod
    def _validate_reservation_replay(
        entry: WorkspaceCreditEntry,
        job: GenerationJob,
        credits: int,
    ) -> None:
        facts = entry.metadata_json or {}
        if (
            entry.generation_job_id != job.id
            or entry.project_id != job.project_id
            or entry.credits != credits
            or facts.get("request_hash") != job.request_hash
        ):
            raise WorkspaceCreditConflict(
                "generation credit idempotency key already belongs to different reservation facts"
            )

    def _entry_for_job(
        self,
        session: Session,
        job: GenerationJob,
        *,
        require_for_free: bool,
    ) -> WorkspaceCreditEntry | None:
        entry = self.entry_for_job_in_session(session, job.id)
        if entry is not None:
            if entry.status not in self.statuses:
                raise WorkspaceCreditConflict(f"unknown workspace credit state: {entry.status}")
            if (
                entry.project_id != job.project_id
                or not job.workspace_credit_required
                or entry.credits != job.quoted_credits
            ):
                raise WorkspaceCreditConflict(
                    "workspace credit reservation does not match immutable generation billing facts"
                )
            return entry
        if require_for_free and job.workspace_credit_required:
            raise WorkspaceCreditConflict("generation requires a server-owned workspace credit reservation")
        return None

    def _settle(
        self,
        session: Session,
        job: GenerationJob,
        *,
        allowed_statuses: set[str],
        event_key: str,
        event_type: str,
        reason: str,
        actor_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceCreditTransition:
        entry = self._entry_for_job(session, job, require_for_free=True)
        if entry is None:
            return self._not_applied()
        if entry.status == "SETTLED":
            return self._snapshot(entry, previous_status=entry.status, replayed=True)
        if entry.status not in allowed_statuses:
            raise WorkspaceCreditConflict(f"cannot settle credit state {entry.status}")
        previous_status = entry.status
        expected_version = entry.version
        now = utcnow()
        result = session.execute(
            update(WorkspaceCreditEntry)
            .where(
                WorkspaceCreditEntry.id == entry.id,
                WorkspaceCreditEntry.status.in_(allowed_statuses),
                WorkspaceCreditEntry.version == expected_version,
            )
            .values(
                status="SETTLED",
                settled_credits=WorkspaceCreditEntry.credits,
                refunded_credits=0,
                reason=reason[:120],
                settled_at=now,
                reconciled_at=(now if previous_status == "RECONCILIATION_REQUIRED" else None),
                version=WorkspaceCreditEntry.version + 1,
                updated_at=now,
            )
        )
        if affected_rows(result) != 1:
            return self._resolve_transition_race(
                session,
                entry.id,
                desired_status="SETTLED",
                previous_status=previous_status,
            )
        session.expire(entry)
        session.refresh(entry)
        entry.balance_after = self._workspace_balance(session, entry)
        cost = session.scalar(select(CostRecord).where(CostRecord.generation_job_id == job.id))
        if cost:
            cost.credits = float(entry.credits)
        self._record_event(
            session,
            entry,
            event_key=event_key,
            event_type=event_type,
            credits=entry.credits,
            balance_delta=0,
            balance_after=entry.balance_after,
            reason=reason,
            actor_type=actor_type,
            metadata=metadata,
        )
        return self._snapshot(entry, previous_status=previous_status, replayed=False)

    def _refund(
        self,
        session: Session,
        job: GenerationJob,
        *,
        allowed_statuses: set[str],
        event_key: str,
        event_type: str,
        reason: str,
        actor_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> WorkspaceCreditTransition:
        entry = self._entry_for_job(session, job, require_for_free=True)
        if entry is None:
            return self._not_applied()
        if entry.status == "REFUNDED":
            return self._snapshot(entry, previous_status=entry.status, replayed=True)
        if entry.status not in allowed_statuses:
            raise WorkspaceCreditConflict(f"cannot refund credit state {entry.status}")
        previous_status = entry.status
        expected_version = entry.version
        now = utcnow()
        claimed = session.execute(
            update(WorkspaceCreditEntry)
            .where(
                WorkspaceCreditEntry.id == entry.id,
                WorkspaceCreditEntry.status.in_(allowed_statuses),
                WorkspaceCreditEntry.version == expected_version,
            )
            .values(
                status="REFUNDED",
                settled_credits=0,
                refunded_credits=WorkspaceCreditEntry.credits,
                reason=reason[:120],
                refunded_at=now,
                reconciled_at=(now if previous_status == "RECONCILIATION_REQUIRED" else None),
                version=WorkspaceCreditEntry.version + 1,
                updated_at=now,
            )
        )
        if affected_rows(claimed) != 1:
            return self._resolve_transition_race(
                session,
                entry.id,
                desired_status="REFUNDED",
                previous_status=previous_status,
            )
        restored = session.execute(
            update(Workspace)
            .where(Workspace.id == entry.workspace_id)
            .values(credit_balance=Workspace.credit_balance + entry.credits)
        )
        if affected_rows(restored) != 1:
            raise LookupError("workspace disappeared while refunding credits")
        workspace = session.get(Workspace, entry.workspace_id)
        if workspace is None:  # pragma: no cover - guarded by the conditional update.
            raise LookupError("workspace disappeared while refunding credits")
        session.refresh(workspace, ["credit_balance"])
        session.expire(entry)
        session.refresh(entry)
        entry.balance_after = workspace.credit_balance
        cost = session.scalar(select(CostRecord).where(CostRecord.generation_job_id == job.id))
        if cost:
            cost.credits = 0.0
        self._record_event(
            session,
            entry,
            event_key=event_key,
            event_type=event_type,
            credits=entry.credits,
            balance_delta=entry.credits,
            balance_after=workspace.credit_balance,
            reason=reason,
            actor_type=actor_type,
            metadata=metadata,
        )
        return self._snapshot(entry, previous_status=previous_status, replayed=False)

    @staticmethod
    def _record_event(
        session: Session,
        entry: WorkspaceCreditEntry,
        *,
        event_key: str,
        event_type: str,
        credits: int,
        balance_delta: int,
        balance_after: int,
        reason: str,
        actor_type: str = "SYSTEM",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            WorkspaceCreditEvent(
                credit_entry_id=entry.id,
                workspace_id=entry.workspace_id,
                project_id=entry.project_id,
                generation_job_id=entry.generation_job_id,
                event_key=event_key,
                event_type=event_type,
                credits=credits,
                balance_delta=balance_delta,
                balance_after=balance_after,
                reason=reason[:240],
                actor_type=actor_type,
                metadata_json=dict(metadata or {}),
            )
        )

    @staticmethod
    def _event_exists(session: Session, entry_id: str, event_key: str) -> bool:
        return (
            session.scalar(
                select(WorkspaceCreditEvent.id).where(
                    WorkspaceCreditEvent.credit_entry_id == entry_id,
                    WorkspaceCreditEvent.event_key == event_key,
                )
            )
            is not None
        )

    @staticmethod
    def _workspace_balance(session: Session, entry: WorkspaceCreditEntry) -> int:
        workspace = session.get(Workspace, entry.workspace_id)
        if workspace is None:
            raise LookupError("workspace disappeared while recording credit lifecycle")
        session.refresh(workspace, ["credit_balance"])
        return workspace.credit_balance

    @staticmethod
    def _snapshot(
        entry: WorkspaceCreditEntry,
        *,
        previous_status: str,
        replayed: bool,
    ) -> WorkspaceCreditTransition:
        return WorkspaceCreditTransition(
            applied=True,
            replayed=replayed,
            entry_id=entry.id,
            previous_status=previous_status,
            status=entry.status,
            reserved_credits=entry.credits,
            settled_credits=entry.settled_credits,
            refunded_credits=entry.refunded_credits,
            balance_after=entry.balance_after,
        )

    @staticmethod
    def _not_applied() -> WorkspaceCreditTransition:
        return WorkspaceCreditTransition(False, False, None, None, None, 0, 0, 0, None)

    def _resolve_transition_race(
        self,
        session: Session,
        entry_id: str,
        *,
        desired_status: str,
        previous_status: str,
    ) -> WorkspaceCreditTransition:
        session.expire_all()
        current = session.get(WorkspaceCreditEntry, entry_id)
        if current is None:
            raise LookupError("workspace credit reservation disappeared")
        if current.status == desired_status:
            return self._snapshot(current, previous_status=previous_status, replayed=True)
        raise WorkspaceCreditConflict(
            f"credit transition lost a race: requested={desired_status}, current={current.status}"
        )


__all__ = [
    "InsufficientWorkspaceCredits",
    "ReconcileAction",
    "WorkspaceCreditBalance",
    "WorkspaceCreditCharge",
    "WorkspaceCreditConflict",
    "WorkspaceCreditService",
    "WorkspaceCreditTransition",
]
