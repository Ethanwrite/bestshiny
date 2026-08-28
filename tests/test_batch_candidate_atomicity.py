"""A paid batch is atomic: candidates, media and job bindings commit together.

Provider output is staged to deterministic storage slots first; the database
then learns about all of it — sibling candidates, media rows, job completion,
billing, settlement — in one transaction. These tests kill the process at every
seam of that flow and let a successor gateway (fresh objects, shared database
and storage, the honest crash-recovery shape) finish the job. After every crash
point the same invariants must hold:

* no empty CREATED candidate exists at any moment, crashed or recovered;
* recovery converges to exactly one completed job, three candidates and three
  media rows — never a duplicate, never a half batch;
* staging never accretes: a re-run overwrites its own slots, so storage holds
  exactly one object per artefact when the dust settles.

The crash is a ``BaseException`` on purpose: the gateway's error handling
catches ``Exception`` to quarantine provider failures, and a process death is
precisely the failure that no ``except`` block gets to see.
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

import pytest
from generation_gateway import GenerationGateway
from media_service import GENERATION_STAGING_PREFIX, MediaRegistry, sweep_generation_staging
from platform_contracts import GenerationRequest
from production_domain.models import (
    BrowserWorker,
    CandidateStatus,
    Episode,
    GenerationCandidate,
    GenerationEvent,
    GenerationIdempotency,
    GenerationJob,
    JobStatus,
    MediaAsset,
    ProviderAccount,
    Scene,
    Shot,
    utcnow,
)
from provider_sdk import (
    GenerationProvider,
    ProviderHealth,
    ProviderInlineOutput,
    ProviderJob,
    ProviderSubmission,
)
from sqlalchemy import func, select


class SimulatedCrash(BaseException):
    """Process death. Not an Exception: no handler in the gateway may see it."""


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buffer, format="PNG")
    return buffer.getvalue()


# Deterministic across polls, exactly like a provider re-serving one result.
_BATCH = [
    ProviderInlineOutput(content=_png_bytes((200, 30, 30)), mime_type="image/png"),
    ProviderInlineOutput(content=_png_bytes((30, 200, 30)), mime_type="image/png"),
    ProviderInlineOutput(content=_png_bytes((30, 30, 200)), mime_type="image/png"),
]


class BatchImageProvider(GenerationProvider):
    name = "fake"

    def __init__(self) -> None:
        self.poll_count = 0

    async def generate_image(self, request: dict[str, Any], *, account_id: str, worker_id: str):
        return ProviderSubmission("batch-provider-job")

    async def generate_video(self, request: dict[str, Any], *, account_id: str, worker_id: str):
        return ProviderSubmission("batch-provider-job")

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str):
        return "provider-media-1"

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str):
        return True

    async def get_job(self, provider_job_id: str, *, account_id: str, worker_id: str, generation_type: str):
        self.poll_count += 1
        return ProviderJob(provider_job_id, "COMPLETED", progress=1, outputs=list(_BATCH))

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str):
        return False

    async def get_credits(self, *, account_id: str, worker_id: str):
        return 100

    async def health(self):
        return ProviderHealth(True, "ready")


def _add_batch_route(container, provider: BatchImageProvider, model: str = "fake-model") -> None:  # type: ignore[no-untyped-def]
    container.providers.register(provider)
    container.providers.register_model("fake", model, "image")
    with container.database.session() as session:
        account = ProviderAccount(
            provider="fake",
            account_identifier="atomicity@example.com",
            credits=100,
            supported_models=[model],
            video_capacity=2,
            image_capacity=2,
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id="atomicity-worker",
            provider="fake",
            account_id=account.id,
            connection_id="atomicity-connection",
            capabilities=["image", "poll"],
            max_jobs=2,
        )
        session.add(worker)
        account.worker_id = worker.id
        session.flush()


def _make_shot_with_primary(container, project_id: str) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(project_id=project_id, title="Episode", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="alley")
        session.add(scene)
        session.flush()
        shot = Shot(scene_id=scene.id, sequence=1, prompt="a lantern-lit alley", duration=4)
        session.add(shot)
        session.flush()
        primary = GenerationCandidate(shot_id=shot.id, attempt_number=1, status="CREATED")
        session.add(primary)
        session.flush()
        return shot.id, primary.id


async def _submitted_batch_job(container, project_id: str, shot_id: str, primary_id: str):  # type: ignore[no-untyped-def]
    job, _replayed = container.gateway.create(
        GenerationRequest(
            project_id=project_id,
            shot_id=shot_id,
            candidate_id=primary_id,
            type="image",
            provider="fake",
            model="fake-model",
            prompt="a lantern-lit alley after rain",
            image_count=3,
            idempotency_key="batch-atomicity-1",
        )
    )
    submitted = await container.gateway.process(job.id)
    assert submitted.status == JobStatus.SUBMITTED.value
    _make_job_due(container, job.id)
    return job


def _make_job_due(container, job_id: str) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        stored = session.get(GenerationJob, job_id)
        stored.next_retry_at = utcnow() - timedelta(seconds=1)


def _expire_claim(container, job_id: str) -> None:  # type: ignore[no-untyped-def]
    """What time does to a dead worker's lease, without the waiting."""

    with container.database.session() as session:
        stored = session.get(GenerationJob, job_id)
        if stored.claim_expires_at is not None:
            stored.claim_expires_at = utcnow() - timedelta(seconds=1)
        stored.next_retry_at = utcnow() - timedelta(seconds=1)


def _successor_gateway(container) -> GenerationGateway:  # type: ignore[no-untyped-def]
    """A fresh worker process: new gateway, new registry, shared database/storage.

    Instance-level crash patches on the dead gateway and its registry stay with
    the corpse; the successor shares nothing with it but durable state.
    """

    media = MediaRegistry(container.database, container.media.storage)
    return GenerationGateway(
        container.database,
        container.providers,
        media,
        container.gateway.scheduler,
        continuity=container.gateway.continuity,
        retry_policy=container.gateway.retry_policy,
        workspace_credits=container.gateway.workspace_credits,
        model_infrastructure=container.gateway.model_infrastructure,
        provider_mode=container.gateway.provider_mode,
        flow_affinity=container.gateway.flow_affinity,
        live_canary=container.gateway.live_canary,
    )


def _counts(container, project_id: str, shot_id: str) -> dict[str, int]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        return {
            "candidates": session.scalar(
                select(func.count(GenerationCandidate.id)).where(GenerationCandidate.shot_id == shot_id)
            ),
            "assets": session.scalar(
                select(func.count(MediaAsset.id)).where(MediaAsset.project_id == project_id)
            ),
            "empty_created": session.scalar(
                select(func.count(GenerationCandidate.id)).where(
                    GenerationCandidate.status == CandidateStatus.CREATED.value,
                    GenerationCandidate.generation_job_id.is_(None),
                )
            ),
        }


def _staged_keys(container) -> list[str]:  # type: ignore[no-untyped-def]
    return [key for key, _modified in container.media.storage.list_keys(GENERATION_STAGING_PREFIX)]


def _assert_converged(container, project_id: str, shot_id: str, job_id: str) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        job = session.get(GenerationJob, job_id)
        assert job.status == JobStatus.COMPLETED.value
        assert job.output_asset_id is not None
        candidates = session.scalars(
            select(GenerationCandidate)
            .where(GenerationCandidate.shot_id == shot_id)
            .order_by(GenerationCandidate.attempt_number)
        ).all()
        assert len(candidates) == 3
        assert [item.attempt_number for item in candidates] == [1, 2, 3]
        outputs = [item.output_asset_id for item in candidates]
        assert all(outputs) and len(set(outputs)) == 3
        assert all(item.status == CandidateStatus.VALIDATING.value for item in candidates)
        assert {item.generation_job_id for item in candidates} == {job_id}
        assets = session.scalars(select(MediaAsset).where(MediaAsset.project_id == project_id)).all()
        assert len(assets) == 3
        assert {asset.id for asset in assets} == set(outputs)
        assert all(asset.storage_key.startswith(GENERATION_STAGING_PREFIX + job_id) for asset in assets)
        idem = session.scalar(
            select(GenerationIdempotency).where(GenerationIdempotency.generation_job_id == job_id)
        )
        assert idem.status == "SUCCEEDED"
        assert idem.result_asset_id == job.output_asset_id
        completions = session.scalar(
            select(func.count(GenerationEvent.id)).where(
                GenerationEvent.generation_job_id == job_id,
                GenerationEvent.event_type == "JOB_COMPLETED",
            )
        )
        assert completions == 1
    # One slot per artefact, however many attempts it took to fill them.
    assert len(_staged_keys(container)) == 3
    assert _counts(container, project_id, shot_id)["empty_created"] == 0


CRASH_POINTS = [
    "before_first_media_staging",
    "after_first_media_staging",
    "after_all_staging_before_transaction",
    "in_transaction_after_primary_registration",
    "in_transaction_before_candidate_creation",
    "in_transaction_after_candidate_creation",
    "after_completion_commit",
]


def _install_crash(monkeypatch, container, point: str) -> None:  # type: ignore[no-untyped-def]
    gateway = container.gateway
    media = container.media
    if point == "before_first_media_staging":

        def crash_stage(*args: Any, **kwargs: Any):
            raise SimulatedCrash(point)

        monkeypatch.setattr(media, "stage_inline_provider_output", crash_stage)
    elif point == "after_first_media_staging":
        real_stage = media.stage_inline_provider_output
        calls = {"n": 0}

        def crash_after_one(*args: Any, **kwargs: Any):
            calls["n"] += 1
            if calls["n"] > 1:
                raise SimulatedCrash(point)
            return real_stage(*args, **kwargs)

        monkeypatch.setattr(media, "stage_inline_provider_output", crash_after_one)
    elif point == "after_all_staging_before_transaction":

        def crash_finalize(*args: Any, **kwargs: Any):
            raise SimulatedCrash(point)

        monkeypatch.setattr(gateway, "_finalize_completed_generation", crash_finalize)
    elif point == "in_transaction_after_primary_registration":
        # Billing evidence runs inside the completion transaction, after the
        # primary artefact was registered. Dying here must roll all of it back.
        def crash_billing(*args: Any, **kwargs: Any):
            raise SimulatedCrash(point)

        monkeypatch.setattr(gateway, "_record_provider_billing_evidence", crash_billing)
    elif point == "in_transaction_before_candidate_creation":

        def crash_allocate(*args: Any, **kwargs: Any):
            raise SimulatedCrash(point)

        monkeypatch.setattr(gateway, "_allocate_sibling_candidates_in", crash_allocate)
    elif point == "in_transaction_after_candidate_creation":
        real_allocate = gateway._allocate_sibling_candidates_in

        def crash_after_allocate(session: Any, shot_id: str, count: int):
            allocated = real_allocate(session, shot_id, count)
            assert len(allocated) == count
            raise SimulatedCrash(point)

        monkeypatch.setattr(gateway, "_allocate_sibling_candidates_in", crash_after_allocate)
    elif point == "after_completion_commit":

        def crash_settle(*args: Any, **kwargs: Any):
            raise SimulatedCrash(point)

        monkeypatch.setattr(gateway, "_settle_live_generation_canary", crash_settle)
    else:  # pragma: no cover - parametrization names every point.
        raise AssertionError(f"unknown crash point: {point}")


@pytest.mark.parametrize("point", CRASH_POINTS)
async def test_process_death_at_every_seam_converges_without_duplicates(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    monkeypatch,  # type: ignore[no-untyped-def]
    point: str,
) -> None:
    provider = BatchImageProvider()
    _add_batch_route(container, provider)
    shot_id, primary_id = _make_shot_with_primary(container, project.id)
    job = await _submitted_batch_job(container, project.id, shot_id, primary_id)

    _install_crash(monkeypatch, container, point)
    with pytest.raises(SimulatedCrash):
        await container.gateway.process(job.id)
    monkeypatch.undo()

    crashed = _counts(container, project.id, shot_id)
    # At no point does an empty CREATED candidate exist — crashed included.
    assert crashed["empty_created"] == 0
    if point == "after_completion_commit":
        # The database work was already atomic and durable; only the
        # post-commit canary bookkeeping was lost.
        _assert_converged(container, project.id, shot_id, job.id)
    else:
        # Everything before the commit is all-or-nothing: no candidates beyond
        # the primary, no media rows, job not completed.
        assert crashed == {"candidates": 1, "assets": 0, "empty_created": 0}
        with container.database.session() as session:
            stored = session.get(GenerationJob, job.id)
            assert stored.status != JobStatus.COMPLETED.value
            assert stored.output_asset_id is None

    # A successor worker finds the lease expired and finishes the job.
    _expire_claim(container, job.id)
    successor = _successor_gateway(container)
    recovered = await successor.process(job.id)
    assert recovered.status == JobStatus.COMPLETED.value
    _assert_converged(container, project.id, shot_id, job.id)

    # Re-processing a completed job changes nothing: same rows, same events.
    again = await successor.process(job.id)
    assert again.status == JobStatus.COMPLETED.value
    _assert_converged(container, project.id, shot_id, job.id)


async def test_finalize_fence_refuses_a_stale_claim(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
) -> None:
    """The completion transaction is idempotent through its fence, not by luck.

    A finalize replayed with a superseded claim token — a dead worker waking up
    after its lease was taken over — must create nothing: no candidates, no
    media rows, no second completion.
    """

    provider = BatchImageProvider()
    _add_batch_route(container, provider)
    shot_id, primary_id = _make_shot_with_primary(container, project.id)
    job = await _submitted_batch_job(container, project.id, shot_id, primary_id)

    completed = await container.gateway.process(job.id)
    assert completed.status == JobStatus.COMPLETED.value
    _assert_converged(container, project.id, shot_id, job.id)

    with container.database.session() as session:
        stored_keys = [
            asset.storage_key
            for asset in session.scalars(select(MediaAsset).where(MediaAsset.project_id == project.id))
        ]
    primary_key = next(key for key in stored_keys if key.endswith("00.png"))
    from media_service import StagedProviderOutput

    stale_primary = StagedProviderOutput(
        storage_key=primary_key,
        sha256="0" * 64,
        size_bytes=1,
        mime_type="image/png",
        local_path=None,
        public_url=None,
    )
    replay = container.gateway._finalize_completed_generation(
        job.id,
        claim_token="a-token-time-forgot",
        provider_job_id="batch-provider-job",
        poll_fence_conditions=[],
        provider_name="fake",
        project_id=project.id,
        shot_id=shot_id,
        candidate_id=primary_id,
        asset_type="IMAGE",
        primary=stale_primary,
        extras=[],
        raw=None,
    )
    assert replay is None
    _assert_converged(container, project.id, shot_id, job.id)


async def test_adopted_slots_survive_the_sweeper_and_recovery_overwrites_in_place(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """Staging never accretes and never loses adopted media to the TTL.

    A crash between staging and the transaction leaves slots behind; the retry
    overwrites those same slots. Once adopted, the slots are media: a sweep far
    past the TTL must keep every one of them.
    """

    provider = BatchImageProvider()
    _add_batch_route(container, provider)
    shot_id, primary_id = _make_shot_with_primary(container, project.id)
    job = await _submitted_batch_job(container, project.id, shot_id, primary_id)

    _install_crash(monkeypatch, container, "after_all_staging_before_transaction")
    with pytest.raises(SimulatedCrash):
        await container.gateway.process(job.id)
    monkeypatch.undo()

    stranded = _staged_keys(container)
    assert len(stranded) == 3

    _expire_claim(container, job.id)
    successor = _successor_gateway(container)
    recovered = await successor.process(job.id)
    assert recovered.status == JobStatus.COMPLETED.value
    assert sorted(_staged_keys(container)) == sorted(stranded)

    sweep = sweep_generation_staging(
        database=container.database,
        storage=container.media.storage,
        ttl_seconds=3600,
        now=utcnow() + timedelta(days=30),
    )
    assert sweep.deleted == []
    assert sweep.kept_referenced == 3
    assert len(_staged_keys(container)) == 3
