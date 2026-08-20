"""Scope generation idempotency keys to their project.

Revision ID: 0012_project_scoped_idempotency
Revises: 0011_legacy_workspace_backfill
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_project_scoped_idempotency"
down_revision: str | None = "0011_legacy_workspace_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _resize_alembic_version(length: int) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or "alembic_version" not in _tables():
        return
    bind.execute(sa.text(f"ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR({length})"))


def upgrade() -> None:
    # Alembic's default is VARCHAR(32), while immutable revision IDs from 0013
    # onward are longer. Widen it before Alembic attempts to record 0013.
    _resize_alembic_version(255)
    tables = _tables()
    if "generation_idempotency" not in tables:
        return
    required = {"generation_jobs", "projects"}
    if not required.issubset(tables):
        raise RuntimeError("project-scoped idempotency requires generation_jobs and projects")

    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("generation_idempotency")}
    if "project_id" not in columns:
        op.add_column(
            "generation_idempotency",
            sa.Column("project_id", sa.String(length=36), nullable=True),
        )
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """UPDATE generation_idempotency
            SET project_id = (
                SELECT generation_jobs.project_id
                FROM generation_jobs
                WHERE generation_jobs.id = generation_idempotency.generation_job_id
            )
            WHERE project_id IS NULL"""
        )
    )
    missing = bind.scalar(sa.text("SELECT COUNT(*) FROM generation_idempotency WHERE project_id IS NULL"))
    if missing:
        raise RuntimeError("generation idempotency rows without a valid project cannot be migrated safely")

    constraints = {
        item.get("name") for item in sa.inspect(bind).get_unique_constraints("generation_idempotency")
    }
    indexes = {item.get("name") for item in sa.inspect(bind).get_indexes("generation_idempotency")}
    with op.batch_alter_table("generation_idempotency") as batch_op:
        batch_op.alter_column(
            "project_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        if "uq_generation_idempotency_key" in constraints:
            batch_op.drop_constraint("uq_generation_idempotency_key", type_="unique")
        if "uq_generation_idempotency_project_key" not in constraints:
            batch_op.create_unique_constraint(
                "uq_generation_idempotency_project_key",
                ["project_id", "key"],
            )
        batch_op.create_foreign_key(
            "fk_generation_idempotency_project_id_projects",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="CASCADE",
        )
        if "ix_generation_idempotency_project_id" not in indexes:
            batch_op.create_index(
                "ix_generation_idempotency_project_id",
                ["project_id"],
                unique=False,
            )


def downgrade() -> None:
    if "generation_idempotency" not in _tables():
        _resize_alembic_version(32)
        return
    constraints = {
        item.get("name")
        for item in sa.inspect(op.get_bind()).get_unique_constraints("generation_idempotency")
    }
    indexes = {item.get("name") for item in sa.inspect(op.get_bind()).get_indexes("generation_idempotency")}
    with op.batch_alter_table("generation_idempotency") as batch_op:
        if "ix_generation_idempotency_project_id" in indexes:
            batch_op.drop_index("ix_generation_idempotency_project_id")
        batch_op.drop_constraint(
            "fk_generation_idempotency_project_id_projects",
            type_="foreignkey",
        )
        if "uq_generation_idempotency_project_key" in constraints:
            batch_op.drop_constraint(
                "uq_generation_idempotency_project_key",
                type_="unique",
            )
        if "uq_generation_idempotency_key" not in constraints:
            batch_op.create_unique_constraint("uq_generation_idempotency_key", ["key"])
        batch_op.drop_column("project_id")
    _resize_alembic_version(32)
