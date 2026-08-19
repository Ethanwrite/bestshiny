from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from platform_database import Database
from production_domain.models import (
    BrowserWorker,
    ProviderAccount,
    WorkerCommand,
    WorkerStatus,
    utcnow,
)
from sqlalchemy import select


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
            worker = session.get(BrowserWorker, worker_id)
            if worker is None or worker.connection_id != connection_id:
                raise WorkerDisconnected("worker registration is stale")
            worker.last_heartbeat = utcnow()
            commands = session.scalars(
                select(WorkerCommand)
                .where(
                    WorkerCommand.worker_id == worker_id,
                    WorkerCommand.status == "PENDING",
                )
                .order_by(WorkerCommand.created_at)
                .limit(max(1, min(limit, 50)))
            ).all()
            for command in commands:
                command.status = "CLAIMED"
                command.claimed_at = utcnow()
            session.flush()
            return list(commands)

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
            worker = session.get(BrowserWorker, worker_id)
            command = session.get(WorkerCommand, command_id)
            if (
                worker is None
                or worker.connection_id != connection_id
                or command is None
                or command.worker_id != worker_id
            ):
                raise WorkerDisconnected("worker or command is stale")
            command.response = response
            command.error = error
            command.status = "FAILED" if error else "COMPLETED"
            command.completed_at = utcnow()
            worker.last_heartbeat = utcnow()
            session.flush()
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
