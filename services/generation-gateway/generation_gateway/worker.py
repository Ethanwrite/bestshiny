from __future__ import annotations

import asyncio
import logging

from production_domain.models import GenerationJob, JobStatus, utcnow
from sqlalchemy import and_, or_, select

logger = logging.getLogger(__name__)


async def process_next_job(container) -> bool:  # type: ignore[no-untyped-def]
    """Process at most one job and quarantine failures to that job."""

    now = utcnow()
    due = or_(GenerationJob.next_retry_at.is_(None), GenerationJob.next_retry_at <= now)
    with container.database.session() as session:
        job = session.scalar(
            select(GenerationJob)
            .where(
                or_(
                    GenerationJob.status.in_(
                        [
                            JobStatus.NEW.value,
                            JobStatus.QUEUED.value,
                        ]
                    ),
                    and_(
                        GenerationJob.status.in_([JobStatus.SUBMITTED.value, JobStatus.RUNNING.value]),
                        due,
                    ),
                    and_(GenerationJob.status == JobStatus.RETRY_WAIT.value, due),
                    and_(
                        GenerationJob.status == JobStatus.RESERVED.value,
                        or_(
                            GenerationJob.claim_expires_at.is_(None),
                            GenerationJob.claim_expires_at <= now,
                        ),
                    ),
                ),
            )
            .order_by(GenerationJob.priority.desc(), GenerationJob.created_at)
        )
        job_id = job.id if job else None
    if not job_id:
        return False
    try:
        processed = await container.gateway.process(job_id)
        if processed.status == JobStatus.COMPLETED.value and processed.candidate_id:
            container.cost.record_job(
                processed.id,
                estimated_cost=processed.cost_estimate,
                actual_cost=processed.actual_cost,
            )
            container.candidates.sync_candidate(processed.candidate_id)
    except Exception as exc:
        logger.exception("generation job %s failed outside the gateway", job_id)
        try:
            container.gateway.fail_processing(job_id, exc)
        except Exception:
            logger.exception("generation job %s could not be quarantined", job_id)
    return True


def sweep_expired_uploads_once(container) -> int:  # type: ignore[no-untyped-def]
    """Reclaim abandoned direct uploads. Never fatal to the worker.

    An upload whose presigned PUT expired can never complete, so its row and its
    quota hold are dead capacity until something observes them. `expires_at` has
    been written and indexed since the feature shipped and nothing read it; the
    maintenance endpoint made it *reclaimable* and this makes it reclaimed —
    "the sweep exists" and "the sweep runs" were two different claims and only
    the first was true.

    Each upload is claimed under its own row lock in the completion path's lock
    order, so running beside the API — or beside a second worker, or beside an
    operator hitting the endpoint — is safe.
    """

    from media_service import WorkspaceStorageQuota, sweep_expired_uploads

    result = sweep_expired_uploads(
        database=container.database,
        uploads=container.direct_uploads,
        quota=WorkspaceStorageQuota(container.database),
        limit=max(1, container.settings.expired_upload_sweep_limit),
        reservation_stale_after_seconds=(
            container.settings.storage_reservation_stale_after_seconds
        ),
    )
    if result.swept:
        logger.info(
            "reclaimed %d expired direct upload(s); %d object(s) remain in storage",
            len(result.swept),
            sum(1 for item in result.swept if item["orphaned_object"]),
        )
    if result.reservations_needing_reconciliation:
        # Deliberately not released here either: a hold whose registration
        # succeeded and whose settlement failed must survive for an operator.
        logger.warning(
            "%d storage reservation(s) need operator reconciliation",
            len(result.reservations_needing_reconciliation),
        )
    return len(result.swept)


def sweep_generation_staging_once(container) -> int:  # type: ignore[no-untyped-def]
    """Reclaim staged generation output nothing can ever adopt again.

    A staged artefact whose completion transaction never committed is invisible
    to the database — the crash that stranded it is exactly the crash that left
    no row behind — so the sweep enumerates storage and asks the database only
    whether each slot is still claimable. A slot whose job is live, or whose
    key a media row adopted, is kept and counted, never deleted.
    """

    from media_service import sweep_generation_staging

    result = sweep_generation_staging(
        database=container.database,
        storage=container.media.storage,
        ttl_seconds=container.settings.generation_staging_ttl_seconds,
        limit=max(1, container.settings.generation_staging_sweep_limit),
    )
    if result.deleted:
        logger.info(
            "reclaimed %d staged generation object(s); kept %d live, %d adopted",
            len(result.deleted),
            result.kept_job_active,
            result.kept_referenced,
        )
    if result.failed:
        logger.warning("%d staged generation object(s) could not be deleted", len(result.failed))
    return len(result.deleted)


def sweep_character_evidence_once(container) -> int:  # type: ignore[no-untyped-def]
    """Run the shadow Character Evidence lifecycle pass. Never fatal.

    Enqueues candidates whose video output was registered, dispatches PENDING
    submissions through the configured producer, and moves silent ACCEPTED
    jobs to RECONCILIATION_REQUIRED. Shadow observation only — nothing here
    can change a candidate's gate.
    """

    result = container.character_evidence_tracker.sweep(
        limit=max(1, container.settings.character_evidence_sweep_limit)
    )
    if result.enqueued or result.dispatched or result.failed or result.timed_out:
        logger.info(
            "character evidence sweep: %d enqueued, %d dispatched, %d skipped, "
            "%d retried, %d failed, %d timed out",
            result.enqueued,
            result.dispatched,
            result.skipped,
            result.retried,
            result.failed,
            result.timed_out,
        )
    if result.timed_out:
        logger.warning(
            "%d character evidence acceptance(s) went silent and now require "
            "operator reconciliation",
            result.timed_out,
        )
    return result.dispatched


async def run_loop(container) -> None:  # type: ignore[no-untyped-def]
    container.gateway.recover_after_restart()
    upload_interval = max(0, int(container.settings.expired_upload_sweep_interval_seconds))
    staging_interval = max(0, int(container.settings.generation_staging_sweep_interval_seconds))
    evidence_interval = max(0, int(container.settings.character_evidence_sweep_interval_seconds))
    # Due immediately on start, then on the interval. A worker that restarts
    # often would otherwise never reach its first sweep.
    next_upload_sweep = asyncio.get_running_loop().time() if upload_interval else None
    next_staging_sweep = asyncio.get_running_loop().time() if staging_interval else None
    next_evidence_sweep = asyncio.get_running_loop().time() if evidence_interval else None
    while True:
        if next_upload_sweep is not None and asyncio.get_running_loop().time() >= next_upload_sweep:
            try:
                await asyncio.to_thread(sweep_expired_uploads_once, container)
            except Exception:
                # Maintenance must never take the job loop down with it.
                logger.exception("expired upload sweep failed")
            next_upload_sweep = asyncio.get_running_loop().time() + upload_interval
        if next_staging_sweep is not None and asyncio.get_running_loop().time() >= next_staging_sweep:
            try:
                await asyncio.to_thread(sweep_generation_staging_once, container)
            except Exception:
                logger.exception("generation staging sweep failed")
            next_staging_sweep = asyncio.get_running_loop().time() + staging_interval
        if (
            next_evidence_sweep is not None
            and asyncio.get_running_loop().time() >= next_evidence_sweep
        ):
            try:
                await asyncio.to_thread(sweep_character_evidence_once, container)
            except Exception:
                logger.exception("character evidence sweep failed")
            next_evidence_sweep = asyncio.get_running_loop().time() + evidence_interval
        if not await process_next_job(container):
            await asyncio.sleep(container.settings.worker_poll_interval_seconds)


def run() -> None:
    from video_platform_api.container import build_container

    asyncio.run(run_loop(build_container()))
