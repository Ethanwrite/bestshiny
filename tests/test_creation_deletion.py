"""Deleting a creation: what leaves, what stays, and what is reclaimed later.

Removing a creation from Productions is the one destructive action a creator
can take on their own history, and it sits on top of the money. What is pinned
here:

1. deletion is a stamp, never a row delete — the credit ledger entries, the
   credit events, the cost records and the provider billing evidence are
   byte-for-byte identical before and after, and nothing is refunded;
2. every user-facing surface stops answering for it: the project listing, the
   per-creation route, retry, cancel and reconcile, and the worker's own job
   pickup;
3. a creation with work in flight is stopped first, and one that cannot be
   stopped safely is refused rather than hidden;
4. a provider result that lands *during* the deletion never brings it back;
5. repeating the request is a success that changes nothing;
6. it cannot be done from another workspace;
7. the media is reclaimed after the transaction commits, only ever when this
   creation alone owned it, and a storage failure retries instead of undoing
   the deletion.
"""

from __future__ import annotations

import asyncio
import io
import threading

import pytest
from fastapi.testclient import TestClient
from media_service import sweep_creation_media_cleanup
from platform_contracts import GenerationRequest
from production_domain.models import (
    Asset,
    AssetVersion,
    AssetVersionMedia,
    BrowserWorker,
    CostRecord,
    CreationMediaCleanup,
    Episode,
    GenerationEvent,
    GenerationJob,
    JobStatus,
    MediaAsset,
    Project,
    ProviderAccount,
    Scene,
    Shot,
    User,
    Workspace,
    WorkspaceCreditEntry,
    WorkspaceCreditEvent,
    utcnow,
)
from provider_sdk import GenerationProvider, ProviderJob, ProviderSubmission
from sqlalchemy import func, select
from video_platform_api.main import create_app


# --------------------------------------------------------------------------
# Fixtures for a workspace that can actually pay for a generation.
# --------------------------------------------------------------------------
def _workspace_project(container, *, email: str, title: str = "Productions"):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        user = User(email=email, display_name="Creator")
        session.add(user)
        session.flush()
        workspace = Workspace(
            owner_user_id=user.id, name=f"{title} workspace", status="ACTIVE", plan_tier="FREE"
        )
        session.add(workspace)
        session.flush()
        project = Project(workspace_id=workspace.id, title=title, status="ACTIVE")
        session.add(project)
        session.flush()
        return workspace.id, project.id, user.id


def _reserve(container, project_id: str, *, key: str, credits: int = 10) -> GenerationJob:  # type: ignore[no-untyped-def]
    job, replayed = container.gateway.create(
        GenerationRequest(
            project_id=project_id,
            type="video",
            provider="google_flow",
            model="flow-veo-3.1",
            prompt="A single visible action",
            idempotency_key=key,
        ),
        estimated_credits=credits,
        pricing_version="creation-deletion-test-v1",
    )
    assert replayed is False
    return job


def _register_image(container, project_id: str, *, data: bytes, asset_type: str = "PLATE"):  # type: ignore[no-untyped-def]
    return container.media.register(
        project_id,
        asset_type,
        io.BytesIO(data),
        filename="frame.png",
        mime_type="image/png",
    )[0]


def _attach_output(container, job_id: str, asset_id: str, *, status: str = "COMPLETED") -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        job = session.get(GenerationJob, job_id)
        job.output_asset_id = asset_id
        job.status = status
        job.completed_at = utcnow()
        session.flush()


def _financial_snapshot(container, job_id: str) -> dict:  # type: ignore[no-untyped-def]
    """Everything about a creation that costs money, as comparable values."""

    with container.database.session() as session:
        entry = session.scalar(
            select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job_id)
        )
        events = sorted(
            (event.event_type, event.credits)
            for event in session.scalars(
                select(WorkspaceCreditEvent).where(
                    WorkspaceCreditEvent.generation_job_id == job_id
                )
            )
        )
        costs = sorted(
            (record.id, record.credits, record.estimated_cost, record.actual_cost)
            for record in session.scalars(
                select(CostRecord).where(CostRecord.generation_job_id == job_id)
            )
        )
        workspace_balances = sorted(
            (workspace.id, workspace.credit_balance)
            for workspace in session.scalars(select(Workspace))
        )
        return {
            "entry": None
            if entry is None
            else (entry.id, entry.status, entry.credits, entry.settled_credits, entry.refunded_credits),
            "events": events,
            "costs": costs,
            "balances": workspace_balances,
        }


def _client(container) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(create_app(container))


def _sign_in(client: TestClient, email: str) -> dict[str, str]:
    registered = client.post(
        "/api/auth/register", json={"email": email, "password": "correct horse battery staple"}
    ).json()
    return {"Authorization": f"Bearer {registered['access_token']}"}


def _listed_ids(client: TestClient, headers: dict[str, str], project_id: str) -> list[str]:
    listing = client.get(f"/v1/generations?project_id={project_id}", headers=headers)
    assert listing.status_code == 200
    return [job["id"] for job in listing.json()["jobs"]]


# --------------------------------------------------------------------------
# 1. The money is untouched, and the creation leaves every user surface.
# --------------------------------------------------------------------------
def test_deleting_a_charged_creation_changes_no_financial_record(container) -> None:  # type: ignore[no-untyped-def]
    """The sharp form of the rule: a finished creation's money is frozen.

    Its credits have been settled, so there is nothing left to release and
    nothing the deletion is entitled to touch. Every financial row is compared
    before and after.
    """

    _, project_id, _ = _workspace_project(container, email="ledger@example.com")
    job = _reserve(container, project_id, key="ledger-untouched")
    with container.database.session() as session:
        row = session.get(GenerationJob, job.id)
        row.status = JobStatus.COMPLETED.value
        row.completed_at = utcnow()
        settled = container.workspace_credits.settle_generation(session, row, reason="TEST_SETTLED")
        assert settled.applied
    before = _financial_snapshot(container, job.id)
    assert before["entry"] is not None, "the test needs a real credit reservation to protect"
    assert before["entry"][1] == "SETTLED"

    with _client(container) as client:
        deleted = client.delete(f"/v1/generations/{job.id}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

    assert _financial_snapshot(container, job.id) == before

    with container.database.session() as session:
        row = session.get(GenerationJob, job.id)
        assert row is not None, "a creation is never removed from the table"
        assert row.deleted_at is not None


def test_deleting_an_unstarted_creation_costs_exactly_what_cancelling_costs(container) -> None:  # type: ignore[no-untyped-def]
    """Deleting adds no credit movement of its own.

    A creation that never reached the model is stopped first, and stopping it
    releases its hold — that is the Cancel action's behaviour, and the reason
    deleting is allowed to perform it is that leaving the hold on a creation
    the user can no longer see would strand it forever. What this pins is that
    deleting does nothing *beyond* that: the two paths end in the same place.
    """

    _, project_id, _ = _workspace_project(container, email="parity@example.com")
    control = _reserve(container, project_id, key="parity-control")
    subject = _reserve(container, project_id, key="parity-subject")

    asyncio.run(container.gateway.cancel(control.id))
    control_after_cancel = _financial_snapshot(container, control.id)

    with _client(container) as client:
        assert client.delete(f"/v1/generations/{control.id}").status_code == 200
        assert client.delete(f"/v1/generations/{subject.id}").status_code == 200

    # Deleting the already cancelled one moved no money at all.
    assert _financial_snapshot(container, control.id)["entry"] == control_after_cancel["entry"]
    assert _financial_snapshot(container, control.id)["events"] == control_after_cancel["events"]
    # And the one deleted straight from the queue landed in the same shape.
    control_entry = _financial_snapshot(container, control.id)["entry"]
    subject_entry = _financial_snapshot(container, subject.id)["entry"]
    assert control_entry[1:] == subject_entry[1:]


def test_a_deleted_creation_leaves_every_user_facing_surface(container) -> None:  # type: ignore[no-untyped-def]
    with _client(container) as client:
        headers = _sign_in(client, "surfaces@example.com")
        project_id = client.post("/v1/projects", headers=headers, json={"title": "Surfaces"}).json()["id"]
        kept = _reserve(container, project_id, key="surface-kept")
        gone = _reserve(container, project_id, key="surface-gone")
        # A finished result, so the promote fence below is reached on its own
        # merits rather than on "this creation produced nothing".
        _attach_output(
            container, gone.id, _register_image(container, project_id, data=b"surface-bytes").id
        )

        assert sorted(_listed_ids(client, headers, project_id)) == sorted([kept.id, gone.id])

        assert client.delete(f"/v1/generations/{gone.id}", headers=headers).status_code == 200

        assert _listed_ids(client, headers, project_id) == [kept.id]
        assert client.get(f"/v1/generations/{gone.id}", headers=headers).status_code == 404
        for action in ("retry", "cancel", "reconcile"):
            response = client.post(f"/v1/generations/{gone.id}/{action}", headers=headers, json={})
            assert response.status_code == 404, action
        # Nor can its output be saved into the project: that would outrun the
        # media reclamation the deletion queued.
        promoted = client.post(
            f"/api/generations/{gone.id}/promote",
            headers=headers,
            json={"asset_type": "CHARACTER", "name": "Rescued"},
        )
        assert promoted.status_code == 404


def test_the_worker_never_picks_up_a_deleted_creation(container) -> None:  # type: ignore[no-untyped-def]
    """The second fence: deletion stops the generation, this stops a resume."""

    from generation_gateway.worker import process_next_job

    _, project_id, _ = _workspace_project(container, email="worker@example.com")
    job = _reserve(container, project_id, key="worker-skips")
    with container.database.session() as session:
        session.get(GenerationJob, job.id).status = JobStatus.QUEUED.value
    with _client(container) as client:
        assert client.delete(f"/v1/generations/{job.id}").status_code == 200

    assert asyncio.run(process_next_job(container)) is False


# --------------------------------------------------------------------------
# 2. Idempotency and the workspace fence.
# --------------------------------------------------------------------------
def test_repeating_the_deletion_succeeds_and_changes_nothing(container) -> None:  # type: ignore[no-untyped-def]
    _, project_id, _ = _workspace_project(container, email="idempotent@example.com")
    job = _reserve(container, project_id, key="delete-twice")

    with _client(container) as client:
        first = client.delete(f"/v1/generations/{job.id}")
        second = client.delete(f"/v1/generations/{job.id}")

    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["deleted"] is True

    with container.database.session() as session:
        stamps = session.scalar(
            select(func.count(GenerationEvent.id)).where(
                GenerationEvent.generation_job_id == job.id,
                GenerationEvent.event_type == "CREATION_DELETED",
            )
        )
        queued = session.scalar(
            select(func.count(CreationMediaCleanup.id)).where(
                CreationMediaCleanup.generation_job_id == job.id
            )
        )
    assert stamps == 1
    assert queued == 1


def test_a_creation_cannot_be_deleted_from_another_workspace(container) -> None:  # type: ignore[no-untyped-def]
    with _client(container) as client:
        owner = _sign_in(client, "owner@example.com")
        stranger = _sign_in(client, "stranger@example.com")
        project_id = client.post("/v1/projects", headers=owner, json={"title": "Private"}).json()["id"]
        job = _reserve(container, project_id, key="cross-workspace")

        refused = client.delete(f"/v1/generations/{job.id}", headers=stranger)
        assert refused.status_code in (403, 404)

    with container.database.session() as session:
        assert session.get(GenerationJob, job.id).deleted_at is None


def test_naming_the_wrong_project_never_deletes(container) -> None:  # type: ignore[no-untyped-def]
    with _client(container) as client:
        headers = _sign_in(client, "scoped@example.com")
        mine = client.post("/v1/projects", headers=headers, json={"title": "Mine"}).json()["id"]
        other = client.post("/v1/projects", headers=headers, json={"title": "Other"}).json()["id"]
        job = _reserve(container, mine, key="wrong-project")

        refused = client.delete(f"/v1/generations/{job.id}?project_id={other}", headers=headers)
        assert refused.status_code == 404

        accepted = client.delete(f"/v1/generations/{job.id}?project_id={mine}", headers=headers)
        assert accepted.status_code == 200

    with container.database.session() as session:
        assert session.get(GenerationJob, job.id).deleted_at is not None


# --------------------------------------------------------------------------
# 3. Work in flight is stopped first.
# --------------------------------------------------------------------------
class _StubProvider(GenerationProvider):
    """Confirms a submission and accepts a cancellation."""

    name = "deletion_stub"

    def __init__(self, *, cancels: bool = True) -> None:
        self.cancels = cancels
        self.cancel_calls = 0

    async def generate_image(self, request, *, account_id, worker_id):  # type: ignore[no-untyped-def]
        return ProviderSubmission("stub-provider-job")

    async def generate_video(self, request, *, account_id, worker_id):  # type: ignore[no-untyped-def]
        return ProviderSubmission("stub-provider-job")

    async def upload_asset(self, asset, *, account_id, worker_id):  # type: ignore[no-untyped-def]
        return "stub-media"

    async def validate_asset(self, provider_media_id, *, account_id, worker_id):  # type: ignore[no-untyped-def]
        return True

    async def get_job(self, provider_job_id, *, account_id, worker_id, generation_type):  # type: ignore[no-untyped-def]
        return ProviderJob(provider_job_id, "RUNNING", progress=0.4)

    async def cancel_job(self, provider_job_id, *, account_id, worker_id):  # type: ignore[no-untyped-def]
        self.cancel_calls += 1
        return self.cancels

    async def get_credits(self, *, account_id, worker_id):  # type: ignore[no-untyped-def]
        return 100

    async def health(self):  # type: ignore[no-untyped-def]
        from provider_sdk import ProviderHealth

        return ProviderHealth(True, "stub")


def _register_stub(container, provider: _StubProvider, *, model: str = "stub-video"):  # type: ignore[no-untyped-def]
    container.providers.register(provider)
    container.providers.register_model(provider.name, model, "video")
    with container.database.session() as session:
        account = ProviderAccount(
            provider=provider.name,
            account_identifier=f"{provider.name}@example.com",
            credits=100,
            supported_models=[model],
            video_capacity=1,
            image_capacity=0,
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id=f"{provider.name}-worker",
            provider=provider.name,
            account_id=account.id,
            connection_id=f"{provider.name}-connection",
            capabilities=["video", "poll"],
            max_jobs=1,
        )
        session.add(worker)
        account.worker_id = worker.id
        session.flush()
        return account.id, worker.id


def _make_running(container, job_id: str, account_id: str, worker_id: str) -> None:  # type: ignore[no-untyped-def]
    """The durable shape of a creation the provider has confirmed and is running."""

    with container.database.session() as session:
        job = session.get(GenerationJob, job_id)
        job.status = JobStatus.RESERVED.value
        job.submission_state = "CONFIRMED"
        job.provider_job_id = "stub-provider-job"
        job.account_id = account_id
        job.worker_id = worker_id
        job.claim_token = None
        job.claim_expires_at = None
        session.flush()


def test_a_running_creation_is_stopped_before_it_is_deleted(container) -> None:  # type: ignore[no-untyped-def]
    provider = _StubProvider(cancels=True)
    account_id, worker_id = _register_stub(container, provider)
    _, project_id, _ = _workspace_project(container, email="running@example.com")
    job = container.gateway.create(
        GenerationRequest(
            project_id=project_id,
            type="video",
            provider=provider.name,
            model="stub-video",
            prompt="A single visible action",
            idempotency_key="running-delete",
        ),
        estimated_credits=10,
        pricing_version="creation-deletion-test-v1",
    )[0]
    _make_running(container, job.id, account_id, worker_id)

    with _client(container) as client:
        response = client.delete(f"/v1/generations/{job.id}")

    assert response.status_code == 200
    assert response.json()["cancelled"] is True
    assert provider.cancel_calls == 1
    with container.database.session() as session:
        row = session.get(GenerationJob, job.id)
        assert row.status == JobStatus.CANCELLED.value
        assert row.deleted_at is not None


def test_a_creation_that_cannot_be_stopped_is_refused_not_hidden(container) -> None:  # type: ignore[no-untyped-def]
    """An unconfirmed submission may already be spending. It stays visible."""

    _, project_id, _ = _workspace_project(container, email="unconfirmed@example.com")
    job = _reserve(container, project_id, key="unstoppable")

    with _client(container) as client:
        # After startup, so the worker's own restart recovery cannot rewrite
        # the state this test is about.
        with container.database.session() as session:
            row = session.get(GenerationJob, job.id)
            row.status = JobStatus.SUBMITTED.value
            row.submission_state = "SENT_UNCONFIRMED"
            row.safe_to_retry = False
            session.flush()

        response = client.delete(f"/v1/generations/{job.id}")
        assert response.status_code == 409
        assert "Recheck credits" in response.json()["detail"]
        # And it is still there, with its money still accounted for.
        assert job.id in _listed_ids(client, {}, project_id)

    with container.database.session() as session:
        assert session.get(GenerationJob, job.id).deleted_at is None


def test_a_result_that_lands_during_the_stop_never_returns_to_productions(container) -> None:  # type: ignore[no-untyped-def]
    """The race requirement: the provider wins, the creation still leaves.

    The provider refuses the cancellation because it has already finished. The
    creation is therefore COMPLETED when the deletion stamps it — and the stamp
    is what the listing reads, not the status, so it does not come back.
    """

    provider = _StubProvider(cancels=False)
    account_id, worker_id = _register_stub(container, provider, model="stub-race")
    _, project_id, _ = _workspace_project(container, email="race@example.com")
    job = container.gateway.create(
        GenerationRequest(
            project_id=project_id,
            type="video",
            provider=provider.name,
            model="stub-race",
            prompt="A single visible action",
            idempotency_key="race-delete",
        ),
        estimated_credits=10,
        pricing_version="creation-deletion-test-v1",
    )[0]
    _make_running(container, job.id, account_id, worker_id)

    original_cancel = container.gateway.cancel

    async def completing_cancel(job_id: str):  # type: ignore[no-untyped-def]
        # The provider finishes while the cancellation is in flight.
        with container.database.session() as session:
            row = session.get(GenerationJob, job_id)
            row.status = JobStatus.COMPLETED.value
            row.completed_at = utcnow()
            session.flush()
        return await original_cancel(job_id)

    container.gateway.cancel = completing_cancel  # type: ignore[assignment]
    try:
        with _client(container) as client:
            response = client.delete(f"/v1/generations/{job.id}")
            assert response.status_code == 200
            assert response.json()["cancelled"] is False
            assert _listed_ids(client, {}, project_id) == []
            assert client.get(f"/v1/generations/{job.id}").status_code == 404
    finally:
        container.gateway.cancel = original_cancel  # type: ignore[assignment]

    with container.database.session() as session:
        row = session.get(GenerationJob, job.id)
        assert row.status == JobStatus.COMPLETED.value
        assert row.deleted_at is not None


def test_a_completion_after_the_deletion_stays_deleted(container) -> None:  # type: ignore[no-untyped-def]
    """The other order: the deletion commits, then the provider result lands."""

    _, project_id, _ = _workspace_project(container, email="late@example.com")
    job = _reserve(container, project_id, key="late-completion")

    with _client(container) as client:
        assert client.delete(f"/v1/generations/{job.id}").status_code == 200
        asset = _register_image(container, project_id, data=b"late-result-bytes")
        _attach_output(container, job.id, asset.id)
        assert _listed_ids(client, {}, project_id) == []


# --------------------------------------------------------------------------
# 4. Media reclamation, after the transaction and never at the cost of a share.
# --------------------------------------------------------------------------
def _sweep(container, **kwargs):  # type: ignore[no-untyped-def]
    return sweep_creation_media_cleanup(
        database=container.database, storage=container.storage, **kwargs
    )


def test_media_only_this_creation_owned_is_reclaimed(container) -> None:  # type: ignore[no-untyped-def]
    _, project_id, _ = _workspace_project(container, email="exclusive@example.com")
    job = _reserve(container, project_id, key="exclusive-media")
    asset = _register_image(container, project_id, data=b"exclusive-frame-bytes")
    _attach_output(container, job.id, asset.id)
    stored = container.storage.path_for(asset.storage_key)
    assert stored.is_file()

    with _client(container) as client:
        assert client.delete(f"/v1/generations/{job.id}").status_code == 200
    # Nothing has been deleted yet: the transaction committed without storage.
    assert stored.is_file()

    result = _sweep(container)
    assert result.completed == 1 and result.objects_deleted == 1
    assert not stored.exists()
    with container.database.session() as session:
        row = session.get(MediaAsset, asset.id)
        assert row is not None, "the asset row survives; evidence still points at it"
        assert row.metadata_json["creation_deleted"]["object_deleted"] is True
        queued = session.scalar(
            select(CreationMediaCleanup).where(CreationMediaCleanup.generation_job_id == job.id)
        )
        assert queued.status == "DONE"


@pytest.mark.parametrize("holder", ["shot", "saved_asset", "other_creation"])
def test_media_something_else_still_needs_is_never_deleted(container, holder: str) -> None:  # type: ignore[no-untyped-def]
    _, project_id, _ = _workspace_project(container, email=f"shared-{holder}@example.com")
    job = _reserve(container, project_id, key=f"shared-{holder}")
    asset = _register_image(container, project_id, data=f"shared-{holder}-bytes".encode())
    _attach_output(container, job.id, asset.id)

    with container.database.session() as session:
        if holder == "shot":
            episode = Episode(project_id=project_id, title="One", episode_number=1)
            session.add(episode)
            session.flush()
            scene = Scene(episode_id=episode.id, sequence=1, description="One")
            session.add(scene)
            session.flush()
            session.add(
                Shot(
                    scene_id=scene.id,
                    sequence=1,
                    user_prompt="A single visible action",
                    prompt="A single visible action",
                    start_frame_asset_id=asset.id,
                )
            )
        elif holder == "saved_asset":
            logical = Asset(project_id=project_id, asset_type="CHARACTER", name="Hero")
            session.add(logical)
            session.flush()
            version = AssetVersion(asset_id=logical.id, version=1, primary_media_asset_id=asset.id)
            session.add(version)
            session.flush()
            session.add(
                AssetVersionMedia(asset_version_id=version.id, media_asset_id=asset.id, role="MASTER")
            )
        else:
            neighbour = _reserve(container, project_id, key=f"neighbour-{holder}")
            _attach_output(container, neighbour.id, asset.id)
        session.flush()

    stored = container.storage.path_for(asset.storage_key)
    with _client(container) as client:
        assert client.delete(f"/v1/generations/{job.id}").status_code == 200

    result = _sweep(container)
    assert result.kept_shared == 1
    assert result.objects_deleted == 0
    assert stored.is_file(), "an asset something else references keeps its bytes"
    with container.database.session() as session:
        queued = session.scalar(
            select(CreationMediaCleanup).where(CreationMediaCleanup.generation_job_id == job.id)
        )
        assert queued.status == "KEPT_SHARED"
        assert queued.detail_json["kept_for"], "the holder is recorded, not just the refusal"
        assert "creation_deleted" not in (session.get(MediaAsset, asset.id).metadata_json or {})


def test_a_storage_failure_never_undoes_the_deletion_and_retries(container, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, project_id, _ = _workspace_project(container, email="storage-fail@example.com")
    job = _reserve(container, project_id, key="storage-fails")
    asset = _register_image(container, project_id, data=b"retry-frame-bytes")
    _attach_output(container, job.id, asset.id)

    with _client(container) as client:
        assert client.delete(f"/v1/generations/{job.id}").status_code == 200

    def unavailable(key: str) -> bool:
        raise RuntimeError("object storage is unreachable")

    monkeypatch.setattr(container.storage, "delete", unavailable)
    failed = _sweep(container)
    assert failed.retried == 1 and failed.completed == 0

    with container.database.session() as session:
        row = session.get(GenerationJob, job.id)
        assert row.deleted_at is not None, "a bucket outage cannot restore a deleted creation"
        queued = session.scalar(
            select(CreationMediaCleanup).where(CreationMediaCleanup.generation_job_id == job.id)
        )
        assert queued.status == "PENDING"
        assert queued.attempts == 1
        assert queued.next_attempt_at is not None
        assert "unreachable" in queued.last_error
        # Make it due again without waiting out the backoff.
        queued.next_attempt_at = utcnow()

    monkeypatch.undo()
    recovered = _sweep(container)
    assert recovered.completed == 1 and recovered.objects_deleted == 1
    assert not container.storage.path_for(asset.storage_key).exists()


def test_a_result_that_arrives_after_the_deletion_is_still_reclaimed(container) -> None:  # type: ignore[no-untyped-def]
    """The queue points at the creation, so a late output is not stranded."""

    _, project_id, _ = _workspace_project(container, email="late-media@example.com")
    job = _reserve(container, project_id, key="late-media")

    with _client(container) as client:
        assert client.delete(f"/v1/generations/{job.id}").status_code == 200

    asset = _register_image(container, project_id, data=b"arrived-after-deletion")
    _attach_output(container, job.id, asset.id)

    result = _sweep(container)
    assert result.completed == 1 and result.objects_deleted == 1
    assert not container.storage.path_for(asset.storage_key).exists()


def test_a_sweep_of_a_creation_that_produced_nothing_closes_cleanly(container) -> None:  # type: ignore[no-untyped-def]
    _, project_id, _ = _workspace_project(container, email="nothing@example.com")
    job = _reserve(container, project_id, key="no-output")
    with _client(container) as client:
        assert client.delete(f"/v1/generations/{job.id}").status_code == 200

    result = _sweep(container)
    assert result.completed == 1 and result.objects_deleted == 0
    assert _sweep(container).examined == 0, "a finished row is not swept again"


# --------------------------------------------------------------------------
# 5. Real concurrency.
# --------------------------------------------------------------------------
@pytest.mark.postgres_only
def test_two_simultaneous_deletions_stamp_once(container) -> None:  # type: ignore[no-untyped-def]
    _, project_id, _ = _workspace_project(container, email="concurrent@example.com")
    job = _reserve(container, project_id, key="concurrent-delete")
    responses: list[int] = []
    barrier = threading.Barrier(2)

    def delete() -> None:
        with _client(container) as client:
            barrier.wait(timeout=5)
            responses.append(client.delete(f"/v1/generations/{job.id}").status_code)

    threads = [threading.Thread(target=delete) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert responses == [200, 200]
    with container.database.session() as session:
        stamps = session.scalar(
            select(func.count(GenerationEvent.id)).where(
                GenerationEvent.generation_job_id == job.id,
                GenerationEvent.event_type == "CREATION_DELETED",
            )
        )
        queued = session.scalar(
            select(func.count(CreationMediaCleanup.id)).where(
                CreationMediaCleanup.generation_job_id == job.id
            )
        )
    assert stamps == 1
    assert queued == 1


@pytest.mark.postgres_only
def test_two_sweepers_never_delete_the_same_object_twice(container) -> None:  # type: ignore[no-untyped-def]
    _, project_id, _ = _workspace_project(container, email="two-sweepers@example.com")
    job = _reserve(container, project_id, key="two-sweepers")
    asset = _register_image(container, project_id, data=b"contended-frame-bytes")
    _attach_output(container, job.id, asset.id)
    with _client(container) as client:
        assert client.delete(f"/v1/generations/{job.id}").status_code == 200

    results: list[object] = []

    def sweep() -> None:
        results.append(_sweep(container))

    threads = [threading.Thread(target=sweep) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert sum(getattr(result, "objects_deleted", 0) for result in results) == 1
    assert sum(getattr(result, "completed", 0) for result in results) == 1
