"""Bind browser command claims to the worker connection that won them.

Revision ID: 0016_worker_command_claim_binding
Revises: 0015_cost_record_job_idempotency
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_worker_command_claim_binding"
down_revision: str | None = "0015_cost_record_job_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if "worker_commands" not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns("worker_commands")}


def upgrade() -> None:
    columns = _columns()
    if not columns:
        return
    if "claim_connection_id" not in columns:
        op.add_column(
            "worker_commands",
            sa.Column("claim_connection_id", sa.String(length=100), nullable=True),
        )
    indexes = {item.get("name") for item in sa.inspect(op.get_bind()).get_indexes("worker_commands")}
    if "ix_worker_commands_claim_connection_id" not in indexes:
        op.create_index(
            "ix_worker_commands_claim_connection_id",
            "worker_commands",
            ["claim_connection_id"],
            unique=False,
        )


def downgrade() -> None:
    columns = _columns()
    if "claim_connection_id" not in columns:
        return
    indexes = {item.get("name") for item in sa.inspect(op.get_bind()).get_indexes("worker_commands")}
    if "ix_worker_commands_claim_connection_id" in indexes:
        op.drop_index("ix_worker_commands_claim_connection_id", table_name="worker_commands")
    op.drop_column("worker_commands", "claim_connection_id")
