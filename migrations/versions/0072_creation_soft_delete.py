"""Soft deletion for creations, and the queue that reclaims their media.

A creation (one row of ``generation_jobs``) is the anchor every financial and
evidential record points at: credit ledger entries, reservation settlements,
provider execution records, cost rows, billing evidence and the audit log all
carry its id. Removing one from a user's project therefore cannot be a row
delete — it is ``deleted_at``/``deleted_by``, which takes the creation out of
every user-facing surface and leaves the paid history exactly as written.

Object storage cannot join the database transaction, so the media a deleted
creation exclusively owned is reclaimed afterwards through
``creation_media_cleanups``: one row per deleted creation, retried under a
backoff, resolved against the creation's *current* output so a provider result
that lands after the deletion is still collected.

Revision ID: 0072_creation_soft_delete
Revises: 0071_voyage_official_provider
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072_creation_soft_delete"
down_revision: str | None = "0071_voyage_official_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLE = "creation_media_cleanups"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "generation_jobs" not in tables:
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return
    if NEW_TABLE in tables:
        raise RuntimeError(f"{NEW_TABLE} already exists before its own migration")
    op.add_column(
        "generation_jobs", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("generation_jobs", sa.Column("deleted_by", sa.String(length=36), nullable=True))
    op.create_index("ix_generation_jobs_deleted_at", "generation_jobs", ["deleted_at"])
    op.create_index("ix_generation_jobs_deleted_by", "generation_jobs", ["deleted_by"])
    # The listing this feature exists for: a project's live creations, newest
    # first. Partial on PostgreSQL so the index holds only what is listed.
    op.create_index(
        "ix_generation_jobs_project_live",
        "generation_jobs",
        ["project_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "creation_media_cleanups",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "generation_job_id",
            sa.String(length=36),
            sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("media_asset_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_id", sa.String(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("detail_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("generation_job_id", name="uq_creation_media_cleanup_job"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CLAIMED', 'DONE', 'KEPT_SHARED', 'FAILED')",
            name="ck_creation_media_cleanup_status",
        ),
    )
    op.create_index(
        "ix_creation_media_cleanups_generation_job_id",
        "creation_media_cleanups",
        ["generation_job_id"],
    )
    op.create_index(
        "ix_creation_media_cleanups_project_id", "creation_media_cleanups", ["project_id"]
    )
    op.create_index(
        "ix_creation_media_cleanups_media_asset_id", "creation_media_cleanups", ["media_asset_id"]
    )
    op.create_index(
        "ix_creation_media_cleanups_next_attempt_at",
        "creation_media_cleanups",
        ["next_attempt_at"],
    )
    op.create_index(
        "ix_creation_media_cleanup_due", "creation_media_cleanups", ["status", "next_attempt_at"]
    )


def downgrade() -> None:
    if "generation_jobs" not in _tables():
        return
    op.drop_index("ix_creation_media_cleanup_due", table_name="creation_media_cleanups")
    op.drop_index("ix_creation_media_cleanups_next_attempt_at", table_name="creation_media_cleanups")
    op.drop_index("ix_creation_media_cleanups_media_asset_id", table_name="creation_media_cleanups")
    op.drop_index("ix_creation_media_cleanups_project_id", table_name="creation_media_cleanups")
    op.drop_index(
        "ix_creation_media_cleanups_generation_job_id", table_name="creation_media_cleanups"
    )
    op.drop_table("creation_media_cleanups")
    op.drop_index("ix_generation_jobs_project_live", table_name="generation_jobs")
    op.drop_index("ix_generation_jobs_deleted_by", table_name="generation_jobs")
    op.drop_index("ix_generation_jobs_deleted_at", table_name="generation_jobs")
    op.drop_column("generation_jobs", "deleted_by")
    op.drop_column("generation_jobs", "deleted_at")
