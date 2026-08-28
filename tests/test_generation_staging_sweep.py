"""The staging sweeper deletes only what no completion could ever adopt.

Storage is the ground truth for what staging holds — a crash that stranded an
object left no row to enumerate — and the database is the safety check: a slot
is reclaimed only when it is past the TTL, its job (parsed from the key) is
terminal or unknown, and no media row adopted the key. Everything else is kept
and counted.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta

from media_service import (
    GENERATION_STAGING_PREFIX,
    generation_staging_prefix,
    job_id_from_staging_key,
    sweep_generation_staging,
)
from production_domain.models import (
    GenerationJob,
    JobStatus,
    MediaAsset,
    utcnow,
)


def _stage_object(container, job_id: str, *, index: int = 0, content: bytes = b"staged-bytes") -> str:  # type: ignore[no-untyped-def]
    key_prefix = generation_staging_prefix(job_id, "provider-job")
    stored = container.media.storage.put_exact(
        io.BytesIO(content), key=f"{key_prefix}{index:02d}.png", mime_type="image/png"
    )
    return stored.key


def _add_job(container, project_id: str, status: str) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        job = GenerationJob(
            project_id=project_id,
            generation_type="image",
            provider="fake",
            model="fake-model",
            status=status,
            request_json={"prompt": "swept"},
            request_hash="0" * 64,
        )
        session.add(job)
        session.flush()
        return job.id


def _future() -> datetime:
    """A `now` far past any TTL, so age never protects an object in these tests."""

    return utcnow() + timedelta(days=30)


def test_the_key_scheme_round_trips_the_job_id() -> None:
    prefix = generation_staging_prefix("job-123", "provider-job-abc")
    assert prefix.startswith(GENERATION_STAGING_PREFIX)
    assert job_id_from_staging_key(prefix + "00.png") == "job-123"
    assert job_id_from_staging_key("somewhere/else/00.png") is None
    # Deterministic per (job, provider attempt); disjoint across attempts.
    assert prefix == generation_staging_prefix("job-123", "provider-job-abc")
    assert prefix != generation_staging_prefix("job-123", "provider-job-def")


def test_put_exact_overwrites_its_slot_instead_of_accreting(container) -> None:  # type: ignore[no-untyped-def]
    storage = container.media.storage
    first = storage.put_exact(io.BytesIO(b"attempt-one"), key="staging/generation/j/a/00.png")
    second = storage.put_exact(io.BytesIO(b"attempt-two"), key="staging/generation/j/a/00.png")
    assert first.key == second.key
    keys = [key for key, _ in storage.list_keys(GENERATION_STAGING_PREFIX)]
    assert keys == ["staging/generation/j/a/00.png"]
    with storage.open(second.key) as stream:
        assert stream.read() == b"attempt-two"
    assert storage.delete(second.key) is True
    assert storage.delete(second.key) is False
    assert storage.list_keys(GENERATION_STAGING_PREFIX) == []


def test_sweep_keeps_young_objects_whatever_their_job(container, project) -> None:  # type: ignore[no-untyped-def]
    job_id = _add_job(container, project.id, JobStatus.FAILED.value)
    _stage_object(container, job_id)
    sweep = sweep_generation_staging(
        database=container.database,
        storage=container.media.storage,
        ttl_seconds=86_400,
    )
    assert sweep.deleted == []
    assert sweep.kept_young == 1


def test_sweep_keeps_slots_of_live_jobs_however_old(container, project) -> None:  # type: ignore[no-untyped-def]
    for status in (
        JobStatus.RESERVED.value,
        JobStatus.RUNNING.value,
        JobStatus.RETRY_WAIT.value,
        JobStatus.WORKER_NEEDS_USER_ACTION.value,
    ):
        _stage_object(container, _add_job(container, project.id, status))
    sweep = sweep_generation_staging(
        database=container.database,
        storage=container.media.storage,
        ttl_seconds=3600,
        now=_future(),
    )
    assert sweep.deleted == []
    assert sweep.kept_job_active == 4


def test_sweep_deletes_terminal_and_orphaned_slots(container, project) -> None:  # type: ignore[no-untyped-def]
    failed = _add_job(container, project.id, JobStatus.FAILED.value)
    cancelled = _add_job(container, project.id, JobStatus.CANCELLED.value)
    _stage_object(container, failed)
    _stage_object(container, cancelled)
    # A job the database has never heard of: the crash-before-create shape.
    _stage_object(container, "no-such-job")
    sweep = sweep_generation_staging(
        database=container.database,
        storage=container.media.storage,
        ttl_seconds=3600,
        now=_future(),
    )
    assert len(sweep.deleted) == 3
    assert container.media.storage.list_keys(GENERATION_STAGING_PREFIX) == []


def test_sweep_never_deletes_an_adopted_slot(container, project) -> None:  # type: ignore[no-untyped-def]
    job_id = _add_job(container, project.id, JobStatus.COMPLETED.value)
    key = _stage_object(container, job_id, content=b"adopted-bytes")
    with container.database.session() as session:
        session.add(
            MediaAsset(
                project_id=project.id,
                asset_type="IMAGE",
                sha256="a" * 64,
                lineage_key="shared",
                storage_key=key,
                mime_type="image/png",
                size_bytes=13,
            )
        )
    # An unadopted sibling of the same completed job is still reclaimable.
    _stage_object(container, job_id, index=1, content=b"never-adopted")
    sweep = sweep_generation_staging(
        database=container.database,
        storage=container.media.storage,
        ttl_seconds=3600,
        now=_future(),
    )
    assert sweep.kept_referenced == 1
    assert [item["key"] for item in sweep.deleted] == [
        generation_staging_prefix(job_id, "provider-job") + "01.png"
    ]
    remaining = [k for k, _ in container.media.storage.list_keys(GENERATION_STAGING_PREFIX)]
    assert remaining == [key]


def test_sweep_respects_its_limit_and_continues_next_run(container, project) -> None:  # type: ignore[no-untyped-def]
    job_id = _add_job(container, project.id, JobStatus.FAILED.value)
    for index in range(5):
        _stage_object(container, job_id, index=index)
    first = sweep_generation_staging(
        database=container.database,
        storage=container.media.storage,
        ttl_seconds=3600,
        limit=2,
        now=_future(),
    )
    assert len(first.deleted) == 2
    second = sweep_generation_staging(
        database=container.database,
        storage=container.media.storage,
        ttl_seconds=3600,
        limit=10,
        now=_future(),
    )
    assert len(second.deleted) == 3
    assert container.media.storage.list_keys(GENERATION_STAGING_PREFIX) == []


def test_worker_and_endpoint_run_the_same_sweep(container, project) -> None:  # type: ignore[no-untyped-def]
    """Both operational faces call one implementation with configured bounds."""

    from fastapi.testclient import TestClient
    from generation_gateway.worker import sweep_generation_staging_once
    from video_platform_api.main import create_app

    job_id = _add_job(container, project.id, JobStatus.FAILED.value)
    _stage_object(container, job_id)
    with container.database.session() as session:
        stored = session.get(GenerationJob, job_id)
        assert stored is not None

    # Freshly staged objects are inside the TTL, so the wired sweeps keep them.
    assert sweep_generation_staging_once(container) == 0
    with TestClient(create_app(container)) as client:
        denied = client.post("/internal/maintenance/generation-staging")
        assert denied.status_code == 401
        response = client.post(
            "/internal/maintenance/generation-staging",
            headers={"Authorization": f"Bearer {container.settings.platform_api_key}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["deleted_count"] == 0
        assert body["kept_young"] == 1
