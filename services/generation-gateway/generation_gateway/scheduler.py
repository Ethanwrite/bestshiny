from __future__ import annotations

from datetime import UTC, datetime

from platform_database import Database
from production_domain.models import (
    AccountStatus,
    BrowserWorker,
    ProviderAccount,
    ProviderCredential,
    WorkerStatus,
    utcnow,
)
from sqlalchemy import select


def _aware(value):  # type: ignore[no-untyped-def]
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class NoAccountAvailable(RuntimeError):
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

    def select_account(
        self, provider: str, capability: str, model: str, priority: int = 0
    ) -> tuple[ProviderAccount, BrowserWorker]:
        del priority
        with self.database.session() as session:
            accounts = session.scalars(
                select(ProviderAccount).where(
                    ProviderAccount.provider == provider,
                    ProviderAccount.status.in_([AccountStatus.READY.value, AccountStatus.BUSY.value]),
                    ProviderAccount.credits > 0,
                )
            ).all()
            candidates: list[tuple[tuple, ProviderAccount, BrowserWorker]] = []
            now = datetime.now(UTC)
            for account in accounts:
                if account.cooldown_until and _aware(account.cooldown_until) > now:
                    continue
                credential = (
                    session.get(ProviderCredential, account.credential_id) if account.credential_id else None
                )
                if credential and credential.expires_at and _aware(credential.expires_at) <= now:
                    continue
                if account.supported_models and model not in account.supported_models:
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
                candidates.append((score, account, worker))
            if not candidates:
                raise NoAccountAvailable(f"no ready {provider} account for {capability}/{model}")
            _, account, worker = min(candidates, key=lambda item: item[0])
            if capability == "image":
                account.image_inflight += 1
            else:
                account.video_inflight += 1
            account.pending_jobs += 1
            account.status = AccountStatus.BUSY.value
            worker.current_jobs += 1
            worker.status = (
                WorkerStatus.BUSY.value
                if worker.current_jobs >= worker.max_jobs
                else WorkerStatus.READY.value
            )
            session.flush()
            return account, worker

    def release(
        self, account_id: str, worker_id: str, capability: str, *, success: bool, error: str | None = None
    ) -> None:
        with self.database.session() as session:
            account = session.get(ProviderAccount, account_id)
            worker = session.get(BrowserWorker, worker_id)
            if account:
                if capability == "image":
                    account.image_inflight = max(0, account.image_inflight - 1)
                else:
                    account.video_inflight = max(0, account.video_inflight - 1)
                account.pending_jobs = max(0, account.pending_jobs - 1)
                account.status = AccountStatus.READY.value
                if success:
                    account.success_count += 1
                    account.last_success_at = utcnow()
                else:
                    account.error_count += 1
                    account.last_error_at = utcnow()
                    account.last_error = error
            if worker:
                worker.current_jobs = max(0, worker.current_jobs - 1)
                if worker.status != WorkerStatus.NEEDS_USER_ACTION.value:
                    worker.status = WorkerStatus.READY.value
