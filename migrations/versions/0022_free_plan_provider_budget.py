"""Add workspace plan scope and durable provider budget accounting.

Revision ID: 0022_free_plan_provider_budget
Revises: 0021_unified_model_registry
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0022_free_plan_provider_budget"
down_revision: str | None = "0021_unified_model_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNAPI_BUDGET_ID = "780b0b49-f0c8-54b6-8c3c-288107583648"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "workspaces" in tables:
        workspace_columns = {
            str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns("workspaces")
        }
        if "plan_tier" not in workspace_columns:
            with op.batch_alter_table("workspaces") as batch_op:
                batch_op.add_column(
                    sa.Column(
                        "plan_tier",
                        sa.String(length=40),
                        server_default="FREE",
                        nullable=False,
                    )
                )

    if "provider_budgets" not in tables:
        op.create_table(
            "provider_budgets",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=80), nullable=False),
            sa.Column("credit_budget_usd", sa.Numeric(14, 6), nullable=False),
            sa.Column("actual_cost_usd", sa.Numeric(14, 6), nullable=False),
            sa.Column("reserved_cost_usd", sa.Numeric(14, 6), nullable=False),
            sa.Column("routing_enabled", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_provider_budgets_provider",
            "provider_budgets",
            ["provider"],
            unique=True,
        )

    tables = _tables()
    if "provider_budget_usages" not in tables:
        op.create_table(
            "provider_budget_usages",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("budget_id", sa.String(length=36), nullable=False),
            sa.Column("provider", sa.String(length=80), nullable=False),
            sa.Column("task_id", sa.String(length=200), nullable=False),
            sa.Column("task_role", sa.String(length=100), nullable=False),
            sa.Column("estimated_cost_usd", sa.Numeric(14, 6), nullable=False),
            sa.Column("actual_cost_usd", sa.Numeric(14, 6), nullable=True),
            sa.Column("remaining_budget_usd", sa.Numeric(14, 6), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["budget_id"],
                ["provider_budgets.id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "provider",
                "task_id",
                name="uq_provider_budget_usage_task",
            ),
        )
        op.create_index(
            "ix_provider_budget_usages_budget_id",
            "provider_budget_usages",
            ["budget_id"],
            unique=False,
        )
        op.create_index(
            "ix_provider_budget_usage_lookup",
            "provider_budget_usages",
            ["provider", "status", "created_at"],
            unique=False,
        )

    budget = sa.table(
        "provider_budgets",
        sa.column("id", sa.String),
        sa.column("provider", sa.String),
        sa.column("credit_budget_usd", sa.Numeric),
        sa.column("actual_cost_usd", sa.Numeric),
        sa.column("reserved_cost_usd", sa.Numeric),
        sa.column("routing_enabled", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    exists = op.get_bind().execute(sa.select(budget.c.id).where(budget.c.provider == "runapi")).first()
    if not exists:
        now = datetime.now(UTC)
        op.bulk_insert(
            budget,
            [
                {
                    "id": RUNAPI_BUDGET_ID,
                    "provider": "runapi",
                    "credit_budget_usd": 10,
                    "actual_cost_usd": 0,
                    "reserved_cost_usd": 0,
                    "routing_enabled": True,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )


def downgrade() -> None:
    tables = _tables()
    if "provider_budget_usages" in tables:
        op.drop_index(
            "ix_provider_budget_usage_lookup",
            table_name="provider_budget_usages",
        )
        op.drop_index(
            "ix_provider_budget_usages_budget_id",
            table_name="provider_budget_usages",
        )
        op.drop_table("provider_budget_usages")
    if "provider_budgets" in tables:
        op.drop_index("ix_provider_budgets_provider", table_name="provider_budgets")
        op.drop_table("provider_budgets")
    if "workspaces" in tables:
        workspace_columns = {
            str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns("workspaces")
        }
        if "plan_tier" in workspace_columns:
            with op.batch_alter_table("workspaces") as batch_op:
                batch_op.drop_column("plan_tier")
