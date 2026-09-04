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
                # A deleted creation is never resumed. Deletion stops the
                # generation first, so this is the second fence rather than
                # the first: it is what guarantees that a row which slipped
                # into a runnable state cannot spend money for a creation the
                # user no longer has.
                GenerationJob.deleted_at.is_(None),
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


def sweep_rendition_gc_once(container) -> int:  # type: ignore[no-untyped-def]
    """Collect idle derived renditions whose constraint profile no provider declares.

    Originals are untouchable by construction; the claim/lease keeps a second
    worker or the operator endpoint from double-deleting; DELETED rows remain
    as reconcilable tombstones and revive in place if the constraints return.
    """

    from media_service import active_reference_profiles, sweep_rendition_gc

    result = sweep_rendition_gc(
        database=container.database,
        storage=container.storage,
        active_constraint_profiles=active_reference_profiles(container.providers._providers),
        min_idle_seconds=container.settings.rendition_gc_min_idle_seconds,
        lease_seconds=container.settings.rendition_gc_lease_seconds,
        limit=max(1, container.settings.rendition_gc_limit),
    )
    if result.deleted_rows:
        logger.info(
            "rendition GC: %d row(s) tombstoned, %d object(s) deleted, "
            "%d shared object(s) kept",
            len(result.deleted_rows),
            result.objects_deleted,
            result.objects_kept_shared,
        )
    return len(result.deleted_rows)


def sweep_creation_media_once(container) -> int:  # type: ignore[no-untyped-def]
    """Reclaim the media of deleted creations. Never fatal to the worker.

    Deleting a creation commits without touching object storage, on purpose:
    a bucket that is briefly unreachable must not roll back a deletion the
    user already saw succeed. This is the other half — retried under a
    backoff, and only ever deleting an object nothing else references.
    """

    from media_service import sweep_creation_media_cleanup

    result = sweep_creation_media_cleanup(
        database=container.database,
        storage=container.storage,
        limit=max(1, container.settings.creation_media_cleanup_limit),
        lease_seconds=container.settings.creation_media_cleanup_lease_seconds,
        max_attempts=container.settings.creation_media_cleanup_max_attempts,
        backoff_seconds=container.settings.creation_media_cleanup_backoff_seconds,
    )
    if result.failed:
        logger.warning(
            "creation media cleanup: %d row(s) exhausted their attempts and need an operator",
            result.failed,
        )
    if result.completed or result.kept_shared:
        logger.info(
            "creation media cleanup: %d reclaimed (%d object(s), %d rendition(s)), %d kept shared",
            result.completed,
            result.objects_deleted,
            result.renditions_deleted,
            result.kept_shared,
        )
    return result.completed


def verify_media_once(container) -> int:  # type: ignore[no-untyped-def]
    """Fully verify directly uploaded media. Never fatal to the worker.

    Completion adopted the object from a header read; this decodes the whole
    file (and re-checks its SHA) before the asset may serve providers.
    Failures remain charged while their retained evidence object lands in
    INVALID or QUARANTINED, never in a silent retry loop.
    """

    from media_service import verify_pending_assets

    result = verify_pending_assets(
        database=container.database,
        storage=container.storage,
        limit=max(1, container.settings.media_verification_limit),
        lease_seconds=container.settings.media_verification_lease_seconds,
    )
    if result.invalid or result.quarantined:
        logger.warning(
            "media verification: %d ready, %d invalid, %d quarantined",
            result.verified_ready,
            result.invalid,
            result.quarantined,
        )
    elif result.verified_ready:
        logger.info("media verification: %d asset(s) promoted to READY", result.verified_ready)
    return result.verified_ready


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


def drain_memory_index_once(container) -> int:  # type: ignore[no-untyped-def]
    """Embed the advisory vector memories queued by whoever wrote Canon.

    Deliberately the weakest sweep in the loop: it never touches Canon, and a
    failure here leaves the rows queued for the next pass. Generation keeps
    using the structured Canon, the timeline and the ledger regardless.
    """

    worker = getattr(container, "memory_outbox_worker", None)
    if worker is None:
        return 0
    result = worker.drain(limit=max(1, container.settings.memory_index_sweep_limit))
    if result.indexed or result.failed or result.retried:
        logger.info(
            "memory index outbox: %d indexed, %d retried, %d failed, %d deferred",
            result.indexed,
            result.retried,
            result.failed,
            result.deferred,
        )
    return result.indexed


def sweep_eip3009_payments_once(container) -> int:  # type: ignore[no-untyped-def]
    """Finish gas-sponsored payments even after the buyer closes their browser."""

    result = container.eip3009_relayer.sweep(
        limit=max(1, container.settings.relayer_sweep_limit)
    )
    if result.expired or result.confirmed or result.failed:
        logger.info(
            "EIP-3009 sweep: %d expired, %d confirmed, %d pending, %d failed",
            result.expired,
            result.confirmed,
            result.pending,
            result.failed,
        )
    return result.confirmed


async def run_loop(container) -> None:  # type: ignore[no-untyped-def]
    container.gateway.recover_after_restart()
    upload_interval = max(0, int(container.settings.expired_upload_sweep_interval_seconds))
    staging_interval = max(0, int(container.settings.generation_staging_sweep_interval_seconds))
    evidence_interval = max(0, int(container.settings.character_evidence_sweep_interval_seconds))
    memory_index_interval = max(
        0, int(getattr(container.settings, "memory_index_sweep_interval_seconds", 0))
    )
    rendition_gc_interval = max(0, int(container.settings.rendition_gc_interval_seconds))
    verification_interval = max(0, int(container.settings.media_verification_interval_seconds))
    creation_media_interval = max(
        0, int(getattr(container.settings, "creation_media_cleanup_interval_seconds", 0))
    )
    # Older embedders/tests may supply a deliberately narrow settings object.
    # Missing means disabled, never "run with guessed defaults".
    relayer_interval = max(
        0,
        int(getattr(container.settings, "relayer_sweep_interval_seconds", 0)),
    )
    # Due immediately on start, then on the interval. A worker that restarts
    # often would otherwise never reach its first sweep.
    next_upload_sweep = asyncio.get_running_loop().time() if upload_interval else None
    next_staging_sweep = asyncio.get_running_loop().time() if staging_interval else None
    next_evidence_sweep = asyncio.get_running_loop().time() if evidence_interval else None
    next_memory_index = asyncio.get_running_loop().time() if memory_index_interval else None
    next_rendition_gc = asyncio.get_running_loop().time() if rendition_gc_interval else None
    next_verification = asyncio.get_running_loop().time() if verification_interval else None
    next_creation_media = asyncio.get_running_loop().time() if creation_media_interval else None
    next_relayer_sweep = (
        asyncio.get_running_loop().time()
        if relayer_interval and container.eip3009_relayer.configured
        else None
    )
    while True:
        if next_relayer_sweep is not None and asyncio.get_running_loop().time() >= next_relayer_sweep:
            try:
                await asyncio.to_thread(sweep_eip3009_payments_once, container)
            except Exception:
                logger.exception("EIP-3009 payment sweep failed")
            next_relayer_sweep = asyncio.get_running_loop().time() + relayer_interval
        if (
            next_creation_media is not None
            and asyncio.get_running_loop().time() >= next_creation_media
        ):
            try:
                await asyncio.to_thread(sweep_creation_media_once, container)
            except Exception:
                logger.exception("creation media cleanup sweep failed")
            next_creation_media = asyncio.get_running_loop().time() + creation_media_interval
        if next_verification is not None and asyncio.get_running_loop().time() >= next_verification:
            try:
                await asyncio.to_thread(verify_media_once, container)
            except Exception:
                logger.exception("media verification sweep failed")
            next_verification = asyncio.get_running_loop().time() + verification_interval
        if (
            next_rendition_gc is not None
            and asyncio.get_running_loop().time() >= next_rendition_gc
        ):
            try:
                await asyncio.to_thread(sweep_rendition_gc_once, container)
            except Exception:
                logger.exception("rendition GC sweep failed")
            next_rendition_gc = asyncio.get_running_loop().time() + rendition_gc_interval
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
        if (
            next_memory_index is not None
            and asyncio.get_running_loop().time() >= next_memory_index
        ):
            try:
                await asyncio.to_thread(drain_memory_index_once, container)
            except Exception:
                logger.exception("memory index outbox drain failed")
            next_memory_index = asyncio.get_running_loop().time() + memory_index_interval
        if not await process_next_job(container):
            await asyncio.sleep(container.settings.worker_poll_interval_seconds)


def run() -> None:
    from video_platform_api.container import build_container

    asyncio.run(run_loop(build_container()))
