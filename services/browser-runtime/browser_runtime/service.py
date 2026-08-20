from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    BrowserWorker,
    ProviderAccount,
    WorkerCommand,
    WorkerStatus,
    utcnow,
)
from sqlalchemy import select, update


class WorkerDisconnected(RuntimeError):
    pass


class BrowserCommandTimeout(TimeoutError):
    pass


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class BrowserRuntime:
    """Database-backed browser worker protocol shared by API and job-worker processes."""

    def __init__(self, database: Database, *, heartbeat_timeout_seconds: int = 30):
        self.database = database
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds

    def register(
        self,
        worker_id: str,
        provider: str,
        *,
        account_id: str | None,
        capabilities: list[str],
        max_jobs: int,
        connection_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BrowserWorker:
        connection_id = connection_id or secrets.token_urlsafe(16)
        with self.database.session() as session:
            account = session.get(ProviderAccount, account_id) if account_id else None
            if account_id and (account is None or account.provider != provider):
                raise WorkerDisconnected("worker account is invalid for provider")
            worker = session.get(BrowserWorker, worker_id)
            if worker is None:
                worker = BrowserWorker(id=worker_id, provider=provider, connection_id=connection_id)
                session.add(worker)
            worker.provider = provider
            worker.account_id = account_id
            worker.connection_id = connection_id
            worker.capabilities = capabilities
            worker.max_jobs = max(1, max_jobs)
            worker.status = WorkerStatus.READY.value
            worker.last_heartbeat = utcnow()
            worker.metadata_json = metadata or {}
            if account:
                account.worker_id = worker.id
            session.flush()
            return worker

    def heartbeat(
        self,
        worker_id: str,
        connection_id: str,
        *,
        status: str = WorkerStatus.READY.value,
        credits: int | None = None,
        current_jobs: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BrowserWorker:
        with self.database.session() as session:
            worker = session.get(BrowserWorker, worker_id)
            if worker is None or worker.connection_id != connection_id:
                raise WorkerDisconnected("worker registration is missing or replaced by a newer connection")
            worker.last_heartbeat = utcnow()
            worker.status = status
            if credits is not None:
                worker.credits = credits
                if worker.account_id:
                    account = session.get(ProviderAccount, worker.account_id)
                    if account:
                        account.credits = credits
            if current_jobs is not None:
                worker.current_jobs = max(0, current_jobs)
            if metadata:
                worker.metadata_json = {**worker.metadata_json, **metadata}
            session.flush()
            return worker

    def mark_offline(self, worker_id: str, connection_id: str | None = None) -> None:
        with self.database.session() as session:
            worker = session.get(BrowserWorker, worker_id)
            if worker and (connection_id is None or worker.connection_id == connection_id):
                worker.status = WorkerStatus.OFFLINE.value

    def expire_stale_workers(self) -> int:
        cutoff = utcnow() - timedelta(seconds=self.heartbeat_timeout_seconds)
        count = 0
        with self.database.session() as session:
            workers = session.scalars(
                select(BrowserWorker).where(BrowserWorker.last_heartbeat < cutoff)
            ).all()
            for worker in workers:
                if worker.status != WorkerStatus.OFFLINE.value:
                    worker.status = WorkerStatus.OFFLINE.value
                    count += 1
        return count

    def available_workers(self, provider: str, capability: str | None = None) -> list[BrowserWorker]:
        self.expire_stale_workers()
        with self.database.session() as session:
            workers = session.scalars(
                select(BrowserWorker)
                .where(
                    BrowserWorker.provider == provider,
                    BrowserWorker.status.in_([WorkerStatus.READY.value, WorkerStatus.BUSY.value]),
                    BrowserWorker.current_jobs < BrowserWorker.max_jobs,
                )
                .order_by(BrowserWorker.current_jobs, BrowserWorker.last_heartbeat.desc())
            ).all()
            return [worker for worker in workers if not capability or capability in worker.capabilities]

    def enqueue(
        self, worker_id: str, message_type: str, payload: dict[str, Any], generation_job_id: str | None = None
    ) -> WorkerCommand:
        with self.database.session() as session:
            worker = session.get(BrowserWorker, worker_id)
            if worker is None or worker.status in {
                WorkerStatus.OFFLINE.value,
                WorkerStatus.NEEDS_USER_ACTION.value,
            }:
                raise WorkerDisconnected(f"browser worker {worker_id} is not ready")
            command = WorkerCommand(
                worker_id=worker_id,
                generation_job_id=generation_job_id,
                message_type=message_type,
                payload=payload,
            )
            session.add(command)
            session.flush()
            return command

    def claim_commands(self, worker_id: str, connection_id: str, limit: int = 10) -> list[WorkerCommand]:
        with self.database.session() as session:
            claimed_at = utcnow()
            worker_claim = session.execute(
                update(BrowserWorker)
                .where(
                    BrowserWorker.id == worker_id,
                    BrowserWorker.connection_id == connection_id,
                )
                .values(last_heartbeat=claimed_at)
            )
            if affected_rows(worker_claim) != 1:
                raise WorkerDisconnected("worker registration is stale")

            # Selecting IDs is deliberately separate from claiming them. Multiple
            # pollers may observe the same pending ID, but only the conditional
            # PENDING -> CLAIMED update below can win. This works on SQLite and
            # PostgreSQL without relying on backend-specific SKIP LOCKED support.
            command_ids = session.scalars(
                select(WorkerCommand.id)
                .where(
                    WorkerCommand.worker_id == worker_id,
                    WorkerCommand.status == "PENDING",
                )
                .order_by(WorkerCommand.created_at)
                .limit(max(1, min(limit, 50)))
            ).all()
            claimed: list[WorkerCommand] = []
            for command_id in command_ids:
                result = session.execute(
                    update(WorkerCommand)
                    .where(
                        WorkerCommand.id == command_id,
                        WorkerCommand.worker_id == worker_id,
                        WorkerCommand.status == "PENDING",
                    )
                    .values(
                        status="CLAIMED",
                        claimed_at=claimed_at,
                        claim_connection_id=connection_id,
                    )
                )
                if affected_rows(result) == 1:
                    command = session.get(WorkerCommand, command_id)
                    if command is not None:
                        claimed.append(command)
            return claimed

    def complete_command(
        self,
        worker_id: str,
        connection_id: str,
        command_id: str,
        *,
        response: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> WorkerCommand:
        with self.database.session() as session:
            completed_at = utcnow()
            worker_completion = session.execute(
                update(BrowserWorker)
                .where(
                    BrowserWorker.id == worker_id,
                    BrowserWorker.connection_id == connection_id,
                )
                .values(last_heartbeat=completed_at)
            )
            if affected_rows(worker_completion) != 1:
                raise WorkerDisconnected("worker registration is stale")

            command_completion = session.execute(
                update(WorkerCommand)
                .where(
                    WorkerCommand.id == command_id,
                    WorkerCommand.worker_id == worker_id,
                    WorkerCommand.status == "CLAIMED",
                    WorkerCommand.claim_connection_id == connection_id,
                )
                .values(
                    response=response,
                    error=error,
                    status="FAILED" if error else "COMPLETED",
                    completed_at=completed_at,
                )
            )
            if affected_rows(command_completion) != 1:
                raise WorkerDisconnected("command is not claimed by this connection")
            command = session.get(WorkerCommand, command_id)
            if command is None:  # Defensive: the conditional update just succeeded.
                raise WorkerDisconnected("worker command disappeared")
            return command

    async def dispatch(
        self,
        worker_id: str,
        message_type: str,
        payload: dict[str, Any],
        *,
        generation_job_id: str | None = None,
        timeout_seconds: float = 75,
    ) -> dict[str, Any]:
        command = self.enqueue(worker_id, message_type, payload, generation_job_id)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            with self.database.session() as session:
                current = session.get(WorkerCommand, command.id)
                if current is None:
                    raise BrowserCommandTimeout("browser command disappeared")
                if current.status == "COMPLETED":
                    return current.response or {}
                if current.status == "FAILED":
                    raise RuntimeError(current.error or "browser command failed")
                worker = session.get(BrowserWorker, worker_id)
                heartbeat = _aware(worker.last_heartbeat) if worker else None
                if (
                    not worker
                    or worker.status == WorkerStatus.OFFLINE.value
                    or (
                        heartbeat
                        and heartbeat < datetime.now(UTC) - timedelta(seconds=self.heartbeat_timeout_seconds)
                    )
                ):
                    raise WorkerDisconnected(f"browser worker {worker_id} disconnected")
            await asyncio.sleep(0.15)
        raise BrowserCommandTimeout(f"browser command {command.id} timed out")
