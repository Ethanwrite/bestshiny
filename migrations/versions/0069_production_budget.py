"""Add the automatic production budget: spend windows and per-operation authorizations.

Revision ID: 0069_production_budget
Revises: 0068_xunhupay
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0069_production_budget"
down_revision: str | None = "0068_xunhupay"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLES = ("production_budget_ledgers", "generation_spend_authorizations")


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    present = set(NEW_TABLES).intersection(tables)
    if "generation_jobs" not in tables and not present:
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return
    required = {"workspaces", "projects", "generation_jobs"}
    missing = required.difference(tables)
    if missing:
        raise RuntimeError(f"production budget migration requires missing tables: {sorted(missing)}")
    if present:
        raise RuntimeError(
            f"production budget migration found partial pre-existing tables: {sorted(present)}"
        )

    op.create_table(
        "production_budget_ledgers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("scope_key", sa.String(length=80), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("limit_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("actual_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("limit_usd >= 0", name="ck_production_budget_limit_nonnegative"),
        sa.CheckConstraint("reserved_usd >= 0", name="ck_production_budget_reserved_nonnegative"),
        sa.CheckConstraint("actual_usd >= 0", name="ck_production_budget_actual_nonnegative"),
        sa.CheckConstraint("window_seconds > 0", name="ck_production_budget_window_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "scope_key", "window_start", name="uq_production_budget_window"),
    )

    op.create_table(
        "generation_spend_authorizations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("operation_key", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("generation_job_id", sa.String(length=36), nullable=True),
        sa.Column("model_role", sa.String(length=80), nullable=True),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("max_cost_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("reserved_cost_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("actual_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("quoted_credits", sa.Integer(), nullable=False),
        sa.Column("pricing_version", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("fence", sa.String(length=20), nullable=False),
        sa.Column("settlement_source", sa.String(length=40), nullable=True),
        sa.Column("evidence_reference", sa.String(length=500), nullable=True),
        sa.Column("platform_ledger_id", sa.String(length=36), nullable=False),
        sa.Column("provider_ledger_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("max_cost_usd > 0", name="ck_spend_authorization_max_positive"),
        sa.CheckConstraint("reserved_cost_usd >= 0", name="ck_spend_authorization_reserved_nonnegative"),
        sa.CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_spend_authorization_actual_nonnegative",
        ),
        sa.CheckConstraint("quoted_credits >= 0", name="ck_spend_authorization_credits_nonnegative"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["generation_job_id"], ["generation_jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["platform_ledger_id"], ["production_budget_ledgers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["provider_ledger_id"], ["production_budget_ledgers.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operation_key", name="uq_spend_authorization_operation"),
        sa.UniqueConstraint("generation_job_id", name="uq_spend_authorization_job"),
    )
    op.create_index(
        "ix_generation_spend_authorizations_workspace_id",
        "generation_spend_authorizations",
        ["workspace_id"],
    )
    op.create_index(
        "ix_generation_spend_authorizations_project_id",
        "generation_spend_authorizations",
        ["project_id"],
    )
    op.create_index(
        "ix_generation_spend_authorizations_status",
        "generation_spend_authorizations",
        ["status"],
    )
    op.create_index(
        "ix_spend_authorization_lookup",
        "generation_spend_authorizations",
        ["provider", "model", "status"],
    )
    op.create_index(
        "ix_spend_authorization_workspace",
        "generation_spend_authorizations",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    tables = _tables()
    if "generation_spend_authorizations" not in tables:
        return
    count = (
        op.get_bind().execute(sa.text("SELECT COUNT(*) FROM generation_spend_authorizations")).scalar_one()
    )
    if count:
        raise RuntimeError("downgrade would discard production spend authorizations")
    op.drop_table("generation_spend_authorizations")
    op.drop_table("production_budget_ledgers")
