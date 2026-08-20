from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from browser_runtime import WorkerDisconnected
from generation_gateway.scheduler import NoAccountAvailable
from production_domain.models import (
    AccountStatus,
    BrowserWorker,
    ProviderAccount,
    ProviderCredential,
    ProviderProjectBinding,
    WorkerCommand,
    WorkerStatus,
)


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


def test_concurrent_command_poll_delivers_each_command_once(container):
    worker = container.runtime.register(
        "claim-worker",
        "google_flow",
        account_id=None,
        capabilities=["video"],
        max_jobs=1,
        connection_id="claim-connection",
    )
    command = container.runtime.enqueue(worker.id, "provider.request", {"prompt": "one action"})
    barrier = Barrier(2)

    def claim_once() -> list[str]:
        barrier.wait()
        return [
            item.id
            for item in container.runtime.claim_commands(
                worker.id,
                worker.connection_id,
            )
        ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim_once(), range(2)))

    assert sorted(len(result) for result in results) == [0, 1]
    assert [command_id for result in results for command_id in result] == [command.id]
    with container.database.session() as session:
        persisted = session.get(WorkerCommand, command.id)
        assert persisted is not None
        assert persisted.status == "CLAIMED"
        assert persisted.claim_connection_id == worker.connection_id


def test_stale_connection_and_duplicate_completion_are_rejected(container):
    worker = container.runtime.register(
        "completion-worker",
        "google_flow",
        account_id=None,
        capabilities=["video"],
        max_jobs=1,
        connection_id="old-connection",
    )
    stale_command = container.runtime.enqueue(worker.id, "provider.request", {"attempt": 1})
    assert container.runtime.claim_commands(worker.id, "old-connection")[0].id == stale_command.id

    container.runtime.register(
        worker.id,
        "google_flow",
        account_id=None,
        capabilities=["video"],
        max_jobs=1,
        connection_id="new-connection",
    )
    with pytest.raises(WorkerDisconnected, match="registration is stale"):
        container.runtime.complete_command(
            worker.id,
            "old-connection",
            stale_command.id,
            response={"unsafe": "late old response"},
        )
    with pytest.raises(WorkerDisconnected, match="not claimed by this connection"):
        container.runtime.complete_command(
            worker.id,
            "new-connection",
            stale_command.id,
            response={"unsafe": "unclaimed new response"},
        )
    assert container.runtime.claim_commands(worker.id, "new-connection") == []

    current_command = container.runtime.enqueue(worker.id, "provider.request", {"attempt": 2})
    assert container.runtime.claim_commands(worker.id, "new-connection")[0].id == current_command.id
    completed = container.runtime.complete_command(
        worker.id,
        "new-connection",
        current_command.id,
        response={"ok": True},
    )
    assert completed.status == "COMPLETED"
    with pytest.raises(WorkerDisconnected, match="not claimed by this connection"):
        container.runtime.complete_command(
            worker.id,
            "new-connection",
            current_command.id,
            response={"ok": False},
        )

    with container.database.session() as session:
        stale = session.get(WorkerCommand, stale_command.id)
        current = session.get(WorkerCommand, current_command.id)
        assert stale is not None and stale.status == "CLAIMED"
        assert stale.claim_connection_id == "old-connection"
        assert stale.response is None
        assert current is not None and current.status == "COMPLETED"
        assert current.response == {"ok": True}


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


def test_scheduler_honors_explicit_project_account_binding(container, project, account_worker):
    unbound_account_id, _ = account_worker
    with container.database.session() as session:
        bound = ProviderAccount(
            provider="google_flow",
            account_identifier="bound@example.com",
            credits=25,
            video_capacity=1,
            supported_models=["veo"],
        )
        session.add(bound)
        session.flush()
        worker = BrowserWorker(
            id="bound-worker",
            provider="google_flow",
            account_id=bound.id,
            connection_id="bound-connection",
            status=WorkerStatus.READY.value,
            capabilities=["video"],
        )
        session.add(worker)
        bound.worker_id = worker.id
        session.add(
            ProviderProjectBinding(
                local_project_id=project.id,
                provider="google_flow",
                provider_account_id=bound.id,
                provider_project_id="flow-project-bound",
            )
        )
        bound_id = bound.id
    selected, worker = container.scheduler.select_account(
        "google_flow",
        "video",
        "flow-veo-3.1",
        project_id=project.id,
    )
    assert selected.id == bound_id
    assert selected.id != unbound_account_id
    assert worker.id == "bound-worker"
    container.scheduler.release(selected.id, worker.id, "video", success=True)


def test_scheduler_never_falls_back_when_project_binding_is_not_ready(container, project, account_worker):
    unbound_account_id, _ = account_worker
    with container.database.session() as session:
        session.add(
            ProviderProjectBinding(
                local_project_id=project.id,
                provider="google_flow",
                provider_account_id=unbound_account_id,
                provider_project_id="flow-project-paused",
                status="DISABLED",
            )
        )

    with pytest.raises(NoAccountAvailable):
        container.scheduler.select_account(
            "google_flow",
            "video",
            "flow-veo-3.1",
            project_id=project.id,
        )


@pytest.mark.parametrize(
    ("account_capacity", "worker_capacity"),
    [(1, 8), (8, 1)],
)
def test_scheduler_atomically_enforces_account_and_worker_capacity(
    container,
    account_worker,
    account_capacity,
    worker_capacity,
):
    account_id, worker_id = account_worker
    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, worker_id)
        account.video_capacity = account_capacity
        account.video_inflight = 0
        account.pending_jobs = 0
        account.status = AccountStatus.READY.value
        worker.max_jobs = worker_capacity
        worker.current_jobs = 0
        worker.status = WorkerStatus.READY.value

    attempts = 8
    barrier = Barrier(attempts)

    def reserve_once():
        barrier.wait()
        try:
            account, worker = container.scheduler.select_account("google_flow", "video", "veo")
        except NoAccountAvailable:
            return None
        return account.id, worker.id

    with ThreadPoolExecutor(max_workers=attempts) as executor:
        results = list(executor.map(lambda _: reserve_once(), range(attempts)))

    assert [result for result in results if result is not None] == [(account_id, worker_id)]
    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, worker_id)
        assert account.video_inflight == 1
        assert account.pending_jobs == 1
        assert worker.current_jobs == 1

    container.scheduler.release(account_id, worker_id, "video", success=True)


def test_scheduler_retries_next_candidate_after_cas_conflict(
    container,
    account_worker,
    monkeypatch,
):
    preferred_account_id, _ = account_worker
    with container.database.session() as session:
        fallback = ProviderAccount(
            provider="google_flow",
            account_identifier="cas-fallback@example.com",
            tier="FREE",
            credits=10,
            video_capacity=1,
            supported_models=["veo"],
        )
        session.add(fallback)
        session.flush()
        fallback_worker = BrowserWorker(
            id="cas-fallback-worker",
            provider="google_flow",
            account_id=fallback.id,
            connection_id="cas-fallback-connection",
            capabilities=["video"],
            max_jobs=1,
        )
        session.add(fallback_worker)
        fallback.worker_id = fallback_worker.id
        fallback_id = fallback.id

    original_try_reserve = container.scheduler._try_reserve
    attempted_accounts: list[str] = []

    def conflict_first(account_id, *args, **kwargs):
        attempted_accounts.append(account_id)
        if account_id == preferred_account_id:
            return None
        return original_try_reserve(account_id, *args, **kwargs)

    monkeypatch.setattr(container.scheduler, "_try_reserve", conflict_first)

    account, worker = container.scheduler.select_account("google_flow", "video", "veo")

    assert attempted_accounts[:2] == [preferred_account_id, fallback_id]
    assert account.id == fallback_id
    assert worker.id == "cas-fallback-worker"
    container.scheduler.release(account.id, worker.id, "video", success=True)


def test_release_is_atomic_idempotent_and_does_not_mark_busy_resources_ready(
    container,
    account_worker,
):
    account_id, worker_id = account_worker
    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, worker_id)
        account.image_inflight = 1
        account.video_inflight = 1
        account.pending_jobs = 2
        account.status = AccountStatus.BUSY.value
        worker.current_jobs = 2
        worker.max_jobs = 1
        worker.status = WorkerStatus.BUSY.value

    container.scheduler.release(account_id, worker_id, "video", success=False, error="failed")
    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, worker_id)
        assert (account.image_inflight, account.video_inflight, account.pending_jobs) == (1, 0, 1)
        assert account.status == AccountStatus.BUSY.value
        assert account.error_count == 1
        assert worker.current_jobs == 1
        assert worker.status == WorkerStatus.BUSY.value

    # A duplicate video release is a no-op; it cannot consume the image reservation.
    container.scheduler.release(account_id, worker_id, "video", success=True)
    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, worker_id)
        assert (account.image_inflight, account.video_inflight, account.pending_jobs) == (1, 0, 1)
        assert account.success_count == 0
        assert worker.current_jobs == 1

    container.scheduler.release(account_id, worker_id, "image", success=True)
    container.scheduler.release(account_id, worker_id, "image", success=True)
    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, worker_id)
        assert (account.image_inflight, account.video_inflight, account.pending_jobs) == (0, 0, 0)
        assert account.status == AccountStatus.READY.value
        assert account.success_count == 1
        assert worker.current_jobs == 0
        assert worker.status == WorkerStatus.READY.value


def test_concurrent_duplicate_release_decrements_and_records_success_once(
    container,
    account_worker,
):
    account_id, worker_id = account_worker
    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, worker_id)
        account.video_inflight = 1
        account.pending_jobs = 1
        account.success_count = 0
        account.status = AccountStatus.BUSY.value
        worker.current_jobs = 1
        worker.max_jobs = 1
        worker.status = WorkerStatus.BUSY.value

    attempts = 8
    barrier = Barrier(attempts)

    def release_once(_):
        barrier.wait()
        container.scheduler.release(account_id, worker_id, "video", success=True)

    with ThreadPoolExecutor(max_workers=attempts) as executor:
        list(executor.map(release_once, range(attempts)))

    with container.database.session() as session:
        account = session.get(ProviderAccount, account_id)
        worker = session.get(BrowserWorker, worker_id)
        assert (account.video_inflight, account.pending_jobs, account.success_count) == (0, 0, 1)
        assert account.status == AccountStatus.READY.value
        assert worker.current_jobs == 0
        assert worker.status == WorkerStatus.READY.value
