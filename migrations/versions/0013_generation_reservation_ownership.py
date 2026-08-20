"""Track idempotent provider-capacity release per generation job.

Revision ID: 0013_generation_reservation_ownership
Revises: 0012_project_scoped_idempotency
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_generation_reservation_ownership"
down_revision: str | None = "0012_project_scoped_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if "generation_jobs" not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns("generation_jobs")}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _backfill_and_rebuild_capacity() -> None:
    """Convert aggregate legacy reservations into explicit per-job ownership.

    Before this revision cancelled jobs could leak capacity, while pre-submit
    RETRY_WAIT jobs retained routing after their aggregate reservation had
    already been released. Deriving the aggregates from active remote work
    repairs both cases without guessing from the old counters.
    """

    tables = _tables()
    if not {"generation_jobs", "provider_accounts", "browser_workers"}.issubset(tables):
        return
    bind = op.get_bind()
    job_columns = {column["name"] for column in sa.inspect(bind).get_columns("generation_jobs")}
    required_job_columns = {
        "account_id",
        "worker_id",
        "generation_type",
        "status",
        "provider_job_id",
        "submission_state",
        "reservation_released_at",
    }
    if not required_job_columns.issubset(job_columns):
        return

    # Keep ownership only for work that may still occupy a provider slot.
    # SENT_UNCONFIRMED remains active even without provider_job_id: releasing
    # it would be an unsafe assumption about a potentially paid submission.
    bind.execute(
        sa.text(
            """UPDATE generation_jobs
            SET reservation_released_at = CURRENT_TIMESTAMP
            WHERE NOT (
                account_id IS NOT NULL
                AND worker_id IS NOT NULL
                AND (
                    status IN ('RESERVED', 'SUBMITTED', 'RUNNING')
                    OR (
                        status IN ('RETRY_WAIT', 'WORKER_NEEDS_USER_ACTION')
                        AND (
                            provider_job_id IS NOT NULL
                            OR submission_state <> 'NOT_SENT'
                        )
                    )
                )
            )"""
        )
    )
    active_job = "j.reservation_released_at IS NULL"
    bind.execute(
        sa.text(
            f"""UPDATE provider_accounts
            SET image_inflight = (
                    SELECT COUNT(*) FROM generation_jobs AS j
                    WHERE j.account_id = provider_accounts.id
                      AND j.generation_type = 'image'
                      AND {active_job}
                ),
                video_inflight = (
                    SELECT COUNT(*) FROM generation_jobs AS j
                    WHERE j.account_id = provider_accounts.id
                      AND j.generation_type = 'video'
                      AND {active_job}
                ),
                pending_jobs = (
                    SELECT COUNT(*) FROM generation_jobs AS j
                    WHERE j.account_id = provider_accounts.id
                      AND {active_job}
                ),
                status = CASE
                    WHEN status IN ('READY', 'BUSY') THEN CASE
                        WHEN EXISTS (
                            SELECT 1 FROM generation_jobs AS j
                            WHERE j.account_id = provider_accounts.id
                              AND {active_job}
                        ) THEN 'BUSY'
                        ELSE 'READY'
                    END
                    ELSE status
                END"""
        )
    )
    bind.execute(
        sa.text(
            f"""UPDATE browser_workers
            SET current_jobs = (
                    SELECT COUNT(*) FROM generation_jobs AS j
                    WHERE j.worker_id = browser_workers.id
                      AND {active_job}
                ),
                status = CASE
                    WHEN status IN ('READY', 'BUSY') THEN CASE
                        WHEN (
                            SELECT COUNT(*) FROM generation_jobs AS j
                            WHERE j.worker_id = browser_workers.id
                              AND {active_job}
                        ) >= max_jobs THEN 'BUSY'
                        ELSE 'READY'
                    END
                    ELSE status
                END"""
        )
    )


def upgrade() -> None:
    columns = _columns()
    if columns and "reservation_released_at" not in columns:
        op.add_column(
            "generation_jobs",
            sa.Column("reservation_released_at", sa.DateTime(timezone=True), nullable=True),
        )
    _backfill_and_rebuild_capacity()


def downgrade() -> None:
    columns = _columns()
    if columns and "reservation_released_at" in columns:
        op.drop_column("generation_jobs", "reservation_released_at")
