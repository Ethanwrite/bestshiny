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


async def run_loop(container) -> None:  # type: ignore[no-untyped-def]
    container.gateway.recover_after_restart()
    while True:
        if not await process_next_job(container):
            await asyncio.sleep(container.settings.worker_poll_interval_seconds)


def run() -> None:
    from video_platform_api.container import build_container

    asyncio.run(run_loop(build_container()))
