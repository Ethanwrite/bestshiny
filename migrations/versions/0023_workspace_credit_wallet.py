"""Add the Free-plan starter-credit wallet and append-only charge ledger.

Revision ID: 0023_workspace_credit_wallet
Revises: 0022_free_plan_provider_budget
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_workspace_credit_wallet"
down_revision: str | None = "0022_free_plan_provider_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    # Supported recovery snapshots can be stamped at an older runtime revision
    # without containing the commercial workspace schema.  There is no wallet
    # owner to migrate in that case, so keep the recovery path assetless instead
    # of manufacturing a partial commercial schema.  If a workspace exists, its
    # referenced project/job tables must exist as a complete wallet dependency
    # set; silently proceeding with a partial set would weaken the ledger FKs.
    if "workspaces" not in tables:
        return
    required = {"workspaces", "projects", "generation_jobs"}
    missing = required.difference(tables)
    if missing:
        raise RuntimeError(f"workspace credit migration requires missing tables: {sorted(missing)}")

    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.add_column(
            sa.Column(
                "credit_balance",
                sa.Integer(),
                server_default=sa.text("50"),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_workspace_credit_balance",
            "credit_balance >= 0",
        )

    op.create_table(
        "workspace_credit_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("generation_job_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=250), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.String(length=120), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("balance_after >= 0", name="ck_workspace_credit_entry_balance"),
        sa.CheckConstraint("credits > 0", name="ck_workspace_credit_entry_positive"),
        sa.ForeignKeyConstraint(
            ["generation_job_id"],
            ["generation_jobs.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_workspace_credit_entry_idempotency",
        ),
    )
    op.create_index(
        "ix_workspace_credit_entries_workspace_id",
        "workspace_credit_entries",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_workspace_credit_entries_project_id",
        "workspace_credit_entries",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_workspace_credit_entries_job",
        "workspace_credit_entries",
        ["generation_job_id"],
        unique=False,
    )


def downgrade() -> None:
    tables = _tables()
    if "workspace_credit_entries" in tables:
        op.drop_index("ix_workspace_credit_entries_job", table_name="workspace_credit_entries")
        op.drop_index("ix_workspace_credit_entries_project_id", table_name="workspace_credit_entries")
        op.drop_index("ix_workspace_credit_entries_workspace_id", table_name="workspace_credit_entries")
        op.drop_table("workspace_credit_entries")
    if "workspaces" not in tables:
        return
    workspace_columns = {
        str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns("workspaces")
    }
    if "credit_balance" in workspace_columns:
        with op.batch_alter_table("workspaces") as batch_op:
            batch_op.drop_constraint("ck_workspace_credit_balance", type_="check")
            batch_op.drop_column("credit_balance")
