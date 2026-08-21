from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from platform_database import Database
from production_domain.models import (
    AccountStatus,
    BrowserWorker,
    GenerationJob,
    JobStatus,
    ProviderAccount,
    ProviderCredential,
    ProviderProjectBinding,
    ProviderProjectBindingStatus,
    WorkerStatus,
    utcnow,
)
from sqlalchemy import and_, case, exists, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session


def _aware(value):  # type: ignore[no-untyped-def]
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


def _updated_one_row(result: Any) -> bool:
    return int(getattr(result, "rowcount", 0)) == 1


class NoAccountAvailable(RuntimeError):
    pass


class _ReservationConflict(RuntimeError):
    pass


class AccountScheduler:
    """Persistent load-aware scheduling adapted from flow2api's reservation model."""

    tier_rank = {
        "FREE": 0,
        "PRO": 1,
        "ULTIMATE": 2,
        "PAYGATE_TIER_NOT_PAID": 0,
        "PAYGATE_TIER_ONE": 1,
        "PAYGATE_TIER_TWO": 2,
    }

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _supports_model(account: ProviderAccount, model: str) -> bool:
        if not account.supported_models or model in account.supported_models:
            return True
        aliases = {
            "flow-veo-3.1": {"veo"},
            "veo": {"flow-veo-3.1"},
        }
        return bool(aliases.get(model, set()).intersection(account.supported_models))

    def select_account(
        self,
        provider: str,
        capability: str,
        model: str,
        priority: int = 0,
        *,
        project_id: str | None = None,
        generation_job_id: str | None = None,
        claim_token: str | None = None,
    ) -> tuple[ProviderAccount, BrowserWorker]:
        del priority
        if capability not in {"image", "video"}:
            raise ValueError(f"unsupported generation capability: {capability}")
        if (generation_job_id is None) != (claim_token is None):
            raise ValueError("generation_job_id and claim_token must be supplied together")
        with self.database.session() as session:
            accounts = session.scalars(
                select(ProviderAccount).where(
                    ProviderAccount.provider == provider,
                    ProviderAccount.status.in_([AccountStatus.READY.value, AccountStatus.BUSY.value]),
                    ProviderAccount.credits > 0,
                )
            ).all()
            if project_id:
                project_bindings = list(
                    session.scalars(
                        select(ProviderProjectBinding).where(
                            ProviderProjectBinding.local_project_id == project_id,
                            ProviderProjectBinding.provider == provider,
                        )
                    )
                )
                if project_bindings or provider == "google_flow":
                    bound_account_ids = {
                        binding.provider_account_id
                        for binding in project_bindings
                        if binding.status == ProviderProjectBindingStatus.READY.value
                    }
                    accounts = [account for account in accounts if account.id in bound_account_ids]
            candidates: list[tuple[tuple, str, str]] = []
            now = datetime.now(UTC)
            for account in accounts:
                if account.cooldown_until and _aware(account.cooldown_until) > now:
                    continue
                credential = (
                    session.get(ProviderCredential, account.credential_id) if account.credential_id else None
                )
                if credential and credential.expires_at and _aware(credential.expires_at) <= now:
                    continue
                if not self._supports_model(account, model):
                    continue
                capacity = account.image_capacity if capability == "image" else account.video_capacity
                inflight = account.image_inflight if capability == "image" else account.video_inflight
                if capacity > 0 and inflight >= capacity:
                    continue
                worker = (
                    session.scalar(
                        select(BrowserWorker).where(
                            BrowserWorker.id == account.worker_id,
                            BrowserWorker.account_id == account.id,
                            BrowserWorker.status.in_([WorkerStatus.READY.value, WorkerStatus.BUSY.value]),
                            BrowserWorker.current_jobs < BrowserWorker.max_jobs,
                        )
                    )
                    if account.worker_id
                    else None
                )
                if worker is None or capability not in worker.capabilities:
                    continue
                error_rate = account.error_count / max(1, account.success_count + account.error_count)
                success_time = _aware(account.last_success_at).timestamp() if account.last_success_at else 0.0
                score = (
                    inflight + account.pending_jobs,
                    error_rate,
                    -account.credits,
                    -self.tier_rank.get(account.tier, 0),
                    success_time,
                )
                candidates.append((score, account.id, worker.id))
            if not candidates:
                raise NoAccountAvailable(f"no ready {provider} account for {capability}/{model}")
        for _, account_id, worker_id in sorted(candidates, key=lambda item: item[0]):
            reserved = self._try_reserve(
                account_id,
                worker_id,
                provider=provider,
                capability=capability,
                model=model,
                project_id=project_id,
                generation_job_id=generation_job_id,
                claim_token=claim_token,
            )
            if reserved is not None:
                return reserved
        raise NoAccountAvailable(f"no ready {provider} account for {capability}/{model}")

    def _try_reserve(
        self,
        account_id: str,
        worker_id: str,
        *,
        provider: str,
        capability: str,
        model: str,
        project_id: str | None,
        generation_job_id: str | None,
        claim_token: str | None,
    ) -> tuple[ProviderAccount, BrowserWorker] | None:
        """CAS the account, worker, and optional job owner in one transaction."""

        inflight = ProviderAccount.image_inflight if capability == "image" else ProviderAccount.video_inflight
        capacity = ProviderAccount.image_capacity if capability == "image" else ProviderAccount.video_capacity
        now = datetime.now(UTC)
        conditions = [
            ProviderAccount.id == account_id,
            ProviderAccount.provider == provider,
            ProviderAccount.status.in_([AccountStatus.READY.value, AccountStatus.BUSY.value]),
            ProviderAccount.credits > 0,
            or_(ProviderAccount.cooldown_until.is_(None), ProviderAccount.cooldown_until <= now),
            or_(capacity <= 0, inflight < capacity),
            or_(
                ProviderAccount.credential_id.is_(None),
                exists(
                    select(ProviderCredential.id).where(
                        ProviderCredential.id == ProviderAccount.credential_id,
                        or_(
                            ProviderCredential.expires_at.is_(None),
                            ProviderCredential.expires_at > now,
                        ),
                    )
                ),
            ),
        ]
        if project_id:
            binding_scope = (
                ProviderProjectBinding.local_project_id == project_id,
                ProviderProjectBinding.provider == provider,
            )
            any_binding = exists(select(ProviderProjectBinding.id).where(*binding_scope))
            ready_binding = exists(
                select(ProviderProjectBinding.id).where(
                    *binding_scope,
                    ProviderProjectBinding.provider_account_id == ProviderAccount.id,
                    ProviderProjectBinding.status == "READY",
                )
            )
            # Flow account/project context is sticky. Only the allocator may
            # select an unbound account while creating the first remote project.
            # Ordinary generation must never fall through to another account.
            conditions.append(
                ready_binding if provider == "google_flow" else or_(~any_binding, ready_binding)
            )

        account_values = {
            "pending_jobs": ProviderAccount.pending_jobs + 1,
            "status": AccountStatus.BUSY.value,
        }
        account_values[f"{capability}_inflight"] = inflight + 1
        next_worker_jobs = BrowserWorker.current_jobs + 1
        try:
            with self.database.session() as session:
                account_update = session.execute(
                    update(ProviderAccount)
                    .where(*conditions)
                    .values(**account_values)
                    .execution_options(synchronize_session=False)
                )
                if not _updated_one_row(account_update):
                    raise _ReservationConflict
                account = session.get(ProviderAccount, account_id)
                worker = session.get(BrowserWorker, worker_id)
                if account is None or not self._supports_model(account, model):
                    raise _ReservationConflict
                if worker is None or capability not in worker.capabilities:
                    raise _ReservationConflict
                worker_update = session.execute(
                    update(BrowserWorker)
                    .where(
                        BrowserWorker.id == worker_id,
                        BrowserWorker.account_id == account_id,
                        BrowserWorker.provider == provider,
                        BrowserWorker.status.in_([WorkerStatus.READY.value, WorkerStatus.BUSY.value]),
                        BrowserWorker.current_jobs < BrowserWorker.max_jobs,
                    )
                    .values(
                        current_jobs=next_worker_jobs,
                        status=case(
                            (next_worker_jobs >= BrowserWorker.max_jobs, WorkerStatus.BUSY.value),
                            else_=WorkerStatus.READY.value,
                        ),
                    )
                    .execution_options(synchronize_session=False)
                )
                if not _updated_one_row(worker_update):
                    raise _ReservationConflict
                if generation_job_id is not None:
                    job_update = session.execute(
                        update(GenerationJob)
                        .where(
                            GenerationJob.id == generation_job_id,
                            GenerationJob.status == JobStatus.RESERVED.value,
                            GenerationJob.claim_token == claim_token,
                            GenerationJob.claim_expires_at > now,
                            GenerationJob.submission_state == "NOT_SENT",
                        )
                        .values(
                            account_id=account_id,
                            worker_id=worker_id,
                            reservation_released_at=None,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if not _updated_one_row(job_update):
                        raise _ReservationConflict
                session.expire_all()
                account = session.get(ProviderAccount, account_id)
                worker = session.get(BrowserWorker, worker_id)
                if account is None or worker is None:  # pragma: no cover - protected by both CAS updates.
                    raise _ReservationConflict
                return account, worker
        except _ReservationConflict:
            return None
        except OperationalError as exc:
            if self.database.engine.dialect.name == "sqlite" and "locked" in str(exc).casefold():
                return None
            raise

    def release_job_in_session(
        self,
        session: Session,
        job_id: str,
        *,
        success: bool | None,
        error: str | None = None,
        clear_routing: bool = False,
    ) -> bool:
        """Release one job-owned reservation exactly once in the caller's transaction."""

        job = session.get(GenerationJob, job_id)
        if job is None or job.reservation_released_at is not None or not job.account_id or not job.worker_id:
            return False
        if job.generation_type not in {"image", "video"}:
            raise ValueError(f"unsupported generation capability: {job.generation_type}")

        account_id = job.account_id
        worker_id = job.worker_id
        capability = job.generation_type
        now = utcnow()
        job_values: dict[str, object] = {"reservation_released_at": now}
        if clear_routing:
            job_values.update(account_id=None, worker_id=None, provider_project_id=None)
        ownership_update = session.execute(
            update(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.account_id == account_id,
                GenerationJob.worker_id == worker_id,
                GenerationJob.reservation_released_at.is_(None),
            )
            .values(**job_values)
            .execution_options(synchronize_session=False)
        )
        if not _updated_one_row(ownership_update):
            return False

        inflight = ProviderAccount.image_inflight if capability == "image" else ProviderAccount.video_inflight
        next_capability_inflight = case((inflight > 0, inflight - 1), else_=0)
        next_image = next_capability_inflight if capability == "image" else ProviderAccount.image_inflight
        next_video = next_capability_inflight if capability == "video" else ProviderAccount.video_inflight
        next_pending = case(
            (ProviderAccount.pending_jobs > 0, ProviderAccount.pending_jobs - 1),
            else_=0,
        )
        account_values: dict[str, object] = {
            f"{capability}_inflight": next_capability_inflight,
            "pending_jobs": next_pending,
            "status": case(
                (
                    ProviderAccount.status.in_([AccountStatus.READY.value, AccountStatus.BUSY.value]),
                    case(
                        (
                            and_(next_image == 0, next_video == 0, next_pending == 0),
                            AccountStatus.READY.value,
                        ),
                        else_=AccountStatus.BUSY.value,
                    ),
                ),
                else_=ProviderAccount.status,
            ),
        }
        if success is True:
            account_values.update(
                success_count=ProviderAccount.success_count + 1,
                last_success_at=now,
            )
        elif success is False:
            account_values.update(
                error_count=ProviderAccount.error_count + 1,
                last_error_at=now,
                last_error=error,
            )
        session.execute(
            update(ProviderAccount)
            .where(ProviderAccount.id == account_id)
            .values(**account_values)
            .execution_options(synchronize_session=False)
        )

        next_worker_jobs = case(
            (BrowserWorker.current_jobs > 0, BrowserWorker.current_jobs - 1),
            else_=0,
        )
        session.execute(
            update(BrowserWorker)
            .where(
                BrowserWorker.id == worker_id,
                BrowserWorker.account_id == account_id,
            )
            .values(
                current_jobs=next_worker_jobs,
                status=case(
                    (
                        BrowserWorker.status.in_(
                            [WorkerStatus.NEEDS_USER_ACTION.value, WorkerStatus.OFFLINE.value]
                        ),
                        BrowserWorker.status,
                    ),
                    (next_worker_jobs >= BrowserWorker.max_jobs, WorkerStatus.BUSY.value),
                    else_=WorkerStatus.READY.value,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        job.reservation_released_at = now
        if clear_routing:
            job.account_id = None
            job.worker_id = None
            job.provider_project_id = None
        return True

    def release_job(
        self,
        job_id: str,
        *,
        success: bool | None,
        error: str | None = None,
        clear_routing: bool = False,
    ) -> bool:
        with self.database.session() as session:
            return self.release_job_in_session(
                session,
                job_id,
                success=success,
                error=error,
                clear_routing=clear_routing,
            )

    def release(
        self, account_id: str, worker_id: str, capability: str, *, success: bool, error: str | None = None
    ) -> None:
        if capability not in {"image", "video"}:
            raise ValueError(f"unsupported generation capability: {capability}")
        now = utcnow()
        inflight = ProviderAccount.image_inflight if capability == "image" else ProviderAccount.video_inflight
        next_image = (
            ProviderAccount.image_inflight - 1 if capability == "image" else ProviderAccount.image_inflight
        )
        next_video = (
            ProviderAccount.video_inflight - 1 if capability == "video" else ProviderAccount.video_inflight
        )
        next_pending = case(
            (ProviderAccount.pending_jobs > 0, ProviderAccount.pending_jobs - 1),
            else_=0,
        )
        active_status = case(
            (and_(next_image == 0, next_video == 0, next_pending == 0), AccountStatus.READY.value),
            else_=AccountStatus.BUSY.value,
        )
        account_values: dict[str, object] = {
            f"{capability}_inflight": inflight - 1,
            "pending_jobs": next_pending,
            "status": case(
                (
                    ProviderAccount.status.in_([AccountStatus.READY.value, AccountStatus.BUSY.value]),
                    active_status,
                ),
                else_=ProviderAccount.status,
            ),
        }
        if success:
            account_values.update(
                success_count=ProviderAccount.success_count + 1,
                last_success_at=now,
            )
        else:
            account_values.update(
                error_count=ProviderAccount.error_count + 1,
                last_error_at=now,
                last_error=error,
            )
        next_worker_jobs = case(
            (BrowserWorker.current_jobs > 0, BrowserWorker.current_jobs - 1),
            else_=0,
        )
        with self.database.session() as session:
            account_update = session.execute(
                update(ProviderAccount)
                .where(ProviderAccount.id == account_id, inflight > 0)
                .values(**account_values)
                .execution_options(synchronize_session=False)
            )
            if not _updated_one_row(account_update):
                return
            session.execute(
                update(BrowserWorker)
                .where(
                    BrowserWorker.id == worker_id,
                    BrowserWorker.account_id == account_id,
                )
                .values(
                    current_jobs=next_worker_jobs,
                    status=case(
                        (
                            BrowserWorker.status.in_(
                                [WorkerStatus.NEEDS_USER_ACTION.value, WorkerStatus.OFFLINE.value]
                            ),
                            BrowserWorker.status,
                        ),
                        (next_worker_jobs >= BrowserWorker.max_jobs, WorkerStatus.BUSY.value),
                        else_=WorkerStatus.READY.value,
                    ),
                )
                .execution_options(synchronize_session=False)
            )
