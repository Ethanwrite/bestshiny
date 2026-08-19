from __future__ import annotations

import asyncio

from production_domain.models import GenerationJob, JobStatus, utcnow
from sqlalchemy import select


async def run_loop(container) -> None:  # type: ignore[no-untyped-def]
    container.gateway.recover_after_restart()
    while True:
        with container.database.session() as session:
            job = session.scalar(
                select(GenerationJob)
                .where(
                    GenerationJob.status.in_(
                        [
                            JobStatus.NEW.value,
                            JobStatus.SUBMITTED.value,
                            JobStatus.RUNNING.value,
                            JobStatus.RETRY_WAIT.value,
                        ]
                    ),
                    (GenerationJob.next_retry_at.is_(None)) | (GenerationJob.next_retry_at <= utcnow()),
                )
                .order_by(GenerationJob.priority.desc(), GenerationJob.created_at)
            )
            job_id = job.id if job else None
        if job_id:
            await container.gateway.process(job_id)
        else:
            await asyncio.sleep(container.settings.worker_poll_interval_seconds)


def run() -> None:
    from video_platform_api.container import build_container

    asyncio.run(run_loop(build_container()))
