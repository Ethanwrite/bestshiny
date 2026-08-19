from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from browser_runtime import WorkerDisconnected
from production_domain.models import BrowserWorker, ProviderAccount, ProviderCredential, WorkerStatus


def test_scheduler_uses_capacity_model_credits_and_load(container, account_worker):
    first_id, _ = account_worker
    with container.database.session() as session:
        second = ProviderAccount(
            provider="google_flow",
            account_identifier="low@example.com",
            tier="FREE",
            credits=2,
            image_capacity=1,
            video_capacity=1,
            video_inflight=1,
            supported_models=["veo"],
        )
        session.add(second)
        session.flush()
        worker = BrowserWorker(
            id="worker-2",
            provider="google_flow",
            account_id=second.id,
            connection_id="connection-2",
            status=WorkerStatus.READY.value,
            capabilities=["video"],
        )
        session.add(worker)
        second.worker_id = worker.id
    account, worker = container.scheduler.select_account("google_flow", "video", "veo")
    assert account.id == first_id
    assert worker.id == "worker-1"
    container.scheduler.release(account.id, worker.id, "video", success=True)
    with container.database.session() as session:
        assert session.get(ProviderAccount, first_id).video_inflight == 0


@pytest.mark.asyncio
async def test_worker_disconnect_then_reconnect(container):
    worker = container.runtime.register(
        "browser-1",
        "google_flow",
        account_id=None,
        capabilities=["video"],
        max_jobs=1,
        connection_id="old-connection",
    )
    container.runtime.mark_offline(worker.id, worker.connection_id)
    with pytest.raises(WorkerDisconnected):
        await container.runtime.dispatch(worker.id, "provider.request", {"x": 1}, timeout_seconds=0.2)

    container.runtime.register(
        worker.id,
        "google_flow",
        account_id=None,
        capabilities=["video"],
        max_jobs=1,
        connection_id="new-connection",
    )
    task = asyncio.create_task(
        container.runtime.dispatch(worker.id, "provider.request", {"x": 2}, timeout_seconds=1)
    )
    await asyncio.sleep(0.05)
    commands = container.runtime.claim_commands(worker.id, "new-connection")
    assert len(commands) == 1
    container.runtime.complete_command(
        worker.id,
        "new-connection",
        commands[0].id,
        response={"status": 200, "data": {"ok": True}},
    )
    assert (await task)["data"]["ok"] is True


def test_old_connection_cannot_heartbeat_after_reconnect(container):
    container.runtime.register(
        "browser", "google_flow", account_id=None, capabilities=["video"], max_jobs=1, connection_id="first"
    )
    container.runtime.register(
        "browser", "google_flow", account_id=None, capabilities=["video"], max_jobs=1, connection_id="second"
    )
    with pytest.raises(WorkerDisconnected):
        container.runtime.heartbeat("browser", "first")


def test_worker_registration_and_credits_sync_to_account(container):
    with container.database.session() as session:
        account = ProviderAccount(
            provider="google_flow",
            account_identifier="sync@example.com",
            credits=1,
            supported_models=["veo"],
        )
        session.add(account)
        session.flush()
        account_id = account.id
    worker = container.runtime.register(
        "sync-worker",
        "google_flow",
        account_id=account_id,
        capabilities=["video"],
        max_jobs=1,
        connection_id="sync-connection",
    )
    container.runtime.heartbeat(worker.id, worker.connection_id, credits=73)
    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        assert account.worker_id == worker.id
        assert account.credits == 73


def test_scheduler_skips_expired_credentials(container, account_worker):
    valid_account_id, _ = account_worker
    with container.database.session() as session:
        credential = ProviderCredential(
            provider="google_flow",
            secret_ciphertext="encrypted",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        session.add(credential)
        session.flush()
        expired = ProviderAccount(
            provider="google_flow",
            account_identifier="expired@example.com",
            credits=10_000,
            credential_id=credential.id,
            supported_models=["veo"],
        )
        session.add(expired)
        session.flush()
        worker = BrowserWorker(
            id="expired-worker",
            provider="google_flow",
            account_id=expired.id,
            connection_id="expired-connection",
            capabilities=["video"],
        )
        session.add(worker)
        expired.worker_id = worker.id
    account, worker = container.scheduler.select_account("google_flow", "video", "veo")
    assert account.id == valid_account_id
    assert worker.id == "worker-1"
    container.scheduler.release(account.id, worker.id, "video", success=True)
