"""Add generation job ownership tokens and expiring leases.

Revision ID: 0009_generation_job_claim_lease
Revises: 0008_asset_registry_invariants
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_generation_job_claim_lease"
down_revision: str | None = "0008_asset_registry_invariants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if "generation_jobs" not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns("generation_jobs")}


def _indexes() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if "generation_jobs" not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes("generation_jobs") if index.get("name")}


def upgrade() -> None:
    columns = _columns()
    if not columns:
        return
    if "claim_token" not in columns:
        op.add_column("generation_jobs", sa.Column("claim_token", sa.String(length=64), nullable=True))
    if "claim_expires_at" not in columns:
        op.add_column(
            "generation_jobs",
            sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        )
    indexes = _indexes()
    if "ix_generation_jobs_claim_token" not in indexes:
        op.create_index(
            "ix_generation_jobs_claim_token",
            "generation_jobs",
            ["claim_token"],
        )
    if "ix_generation_jobs_claim_expires_at" not in indexes:
        op.create_index(
            "ix_generation_jobs_claim_expires_at",
            "generation_jobs",
            ["claim_expires_at"],
        )


def downgrade() -> None:
    columns = _columns()
    if not columns:
        return
    indexes = _indexes()
    if "ix_generation_jobs_claim_expires_at" in indexes:
        op.drop_index("ix_generation_jobs_claim_expires_at", table_name="generation_jobs")
    if "ix_generation_jobs_claim_token" in indexes:
        op.drop_index("ix_generation_jobs_claim_token", table_name="generation_jobs")
    if "claim_expires_at" in columns:
        op.drop_column("generation_jobs", "claim_expires_at")
    if "claim_token" in columns:
        op.drop_column("generation_jobs", "claim_token")
