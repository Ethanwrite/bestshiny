"""Make generation-job cost recording exactly once.

Revision ID: 0015_cost_record_job_idempotency
Revises: 0014_worker_scoped_credentials
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_cost_record_job_idempotency"
down_revision: str | None = "0014_worker_scoped_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_cost_records_generation_job_id"
BUSINESS_COLUMNS = (
    "project_id",
    "shot_id",
    "candidate_id",
    "provider",
    "model",
    "duration",
    "resolution",
    "credits",
    "estimated_cost",
    "actual_cost",
    "retry_cost",
    "accepted",
    "wasted",
)


def _table_exists() -> bool:
    return "cost_records" in sa.inspect(op.get_bind()).get_table_names()


def _index() -> dict | None:  # type: ignore[type-arg]
    return next(
        (
            item
            for item in sa.inspect(op.get_bind()).get_indexes("cost_records")
            if item.get("name") == INDEX_NAME
        ),
        None,
    )


def _deduplicate_exact_legacy_rows() -> None:
    bind = op.get_bind()
    selected_columns = ", ".join(("id", "generation_job_id", *BUSINESS_COLUMNS))
    rows = bind.execute(
        sa.text(
            f"SELECT {selected_columns} FROM cost_records "  # noqa: S608 -- static identifiers only
            "WHERE generation_job_id IS NOT NULL ORDER BY created_at, id"
        )
    ).mappings()
    retained: dict[str, tuple[object, ...]] = {}
    duplicate_ids: list[str] = []
    for row in rows:
        job_id = str(row["generation_job_id"])
        signature = tuple(row[column] for column in BUSINESS_COLUMNS)
        previous = retained.get(job_id)
        if previous is None:
            retained[job_id] = signature
            continue
        if signature != previous:
            raise RuntimeError(
                "conflicting cost records exist for generation job "
                f"{job_id}; reconcile the ledger before upgrading"
            )
        duplicate_ids.append(str(row["id"]))
    for duplicate_id in duplicate_ids:
        bind.execute(sa.text("DELETE FROM cost_records WHERE id = :id"), {"id": duplicate_id})


def upgrade() -> None:
    if not _table_exists():
        return
    _deduplicate_exact_legacy_rows()
    current = _index()
    if current and current.get("unique"):
        return
    if current:
        op.drop_index(INDEX_NAME, table_name="cost_records")
    op.create_index(
        INDEX_NAME,
        "cost_records",
        ["generation_job_id"],
        unique=True,
    )


def downgrade() -> None:
    if not _table_exists():
        return
    current = _index()
    if current:
        op.drop_index(INDEX_NAME, table_name="cost_records")
    op.create_index(
        INDEX_NAME,
        "cost_records",
        ["generation_job_id"],
        unique=False,
    )
