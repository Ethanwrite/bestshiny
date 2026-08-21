"""Complete workspace credit reserve/settle/refund/reconcile lifecycle.

Revision ID: 0024_workspace_credit_lifecycle
Revises: 0023_workspace_credit_wallet
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_workspace_credit_lifecycle"
down_revision: str | None = "0023_workspace_credit_wallet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _duplicate_value(group_columns: str, *, require_non_null: str) -> object | None:
    return (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT 1 FROM workspace_credit_entries "
                f"WHERE {require_non_null} GROUP BY {group_columns} HAVING COUNT(*) > 1 LIMIT 1"
            )
        )
        .scalar()
    )


def upgrade() -> None:
    tables = _tables()
    if "workspaces" not in tables:
        return
    required = {"workspaces", "projects", "generation_jobs", "workspace_credit_entries"}
    missing = required.difference(tables)
    if missing:
        raise RuntimeError(f"workspace credit lifecycle requires missing tables: {sorted(missing)}")
    if _duplicate_value("generation_job_id", require_non_null="generation_job_id IS NOT NULL"):
        raise RuntimeError("workspace credit lifecycle found duplicate generation_job_id reservations")
    if _duplicate_value(
        "project_id, idempotency_key",
        require_non_null="project_id IS NOT NULL",
    ):
        raise RuntimeError("workspace credit lifecycle found duplicate project idempotency reservations")
    unsupported_legacy_status = (
        op.get_bind()
        .execute(sa.text("SELECT status FROM workspace_credit_entries WHERE status != 'CHARGED' LIMIT 1"))
        .scalar()
    )
    if unsupported_legacy_status is not None:
        raise RuntimeError(
            f"workspace credit lifecycle expected the 0023 CHARGED state, found: {unsupported_legacy_status}"
        )
    unreserved_active_free_job = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT job.id FROM generation_jobs AS job "
                "JOIN projects AS project ON project.id = job.project_id "
                "JOIN workspaces AS workspace ON workspace.id = project.workspace_id "
                "LEFT JOIN workspace_credit_entries AS credit "
                "ON credit.generation_job_id = job.id "
                "WHERE workspace.plan_tier = 'FREE' AND credit.id IS NULL "
                "AND job.status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED') LIMIT 1"
            )
        )
        .scalar()
    )
    if unreserved_active_free_job is not None:
        raise RuntimeError(
            "workspace credit lifecycle found an active FREE generation without a reservation: "
            f"{unreserved_active_free_job}; terminalize or explicitly reconcile it before upgrading"
        )

    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "workspace_credit_required",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "quoted_credits",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_generation_job_quoted_credits",
            "quoted_credits >= 0",
        )
    op.execute(
        sa.text(
            "UPDATE generation_jobs SET "
            "workspace_credit_required = CASE WHEN EXISTS ("
            "SELECT 1 FROM workspace_credit_entries AS credit "
            "WHERE credit.generation_job_id = generation_jobs.id"
            ") THEN TRUE ELSE FALSE END, "
            "quoted_credits = COALESCE(("
            "SELECT credit.credits FROM workspace_credit_entries AS credit "
            "WHERE credit.generation_job_id = generation_jobs.id"
            "), 0)"
        )
    )

    with op.batch_alter_table("workspace_credit_entries") as batch_op:
        batch_op.add_column(
            sa.Column("settled_credits", sa.Integer(), server_default=sa.text("0"), nullable=False)
        )
        batch_op.add_column(
            sa.Column("refunded_credits", sa.Integer(), server_default=sa.text("0"), nullable=False)
        )
        batch_op.add_column(sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False))
        batch_op.add_column(sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("reconciliation_required_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("reconciliation_reason", sa.String(length=240), nullable=True))

    op.execute(
        sa.text(
            "UPDATE workspace_credit_entries SET "
            "status = 'SETTLED', settled_credits = credits, refunded_credits = 0, "
            "reserved_at = created_at, settled_at = updated_at, version = 1, "
            "reason = 'LEGACY_CHARGE_MIGRATED' WHERE status = 'CHARGED'"
        )
    )
    op.execute(
        sa.text("UPDATE workspace_credit_entries SET reserved_at = created_at WHERE reserved_at IS NULL")
    )
    with op.batch_alter_table("workspace_credit_entries") as batch_op:
        batch_op.drop_constraint("uq_workspace_credit_entry_idempotency", type_="unique")
        batch_op.drop_index("ix_workspace_credit_entries_job")
        batch_op.alter_column(
            "reserved_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_credit_entry_project_key",
            ["project_id", "idempotency_key"],
        )
        batch_op.create_unique_constraint(
            "uq_credit_entry_generation_job",
            ["generation_job_id"],
        )
        batch_op.create_check_constraint(
            "ck_credit_entry_settled_nonnegative",
            "settled_credits >= 0",
        )
        batch_op.create_check_constraint(
            "ck_credit_entry_refunded_nonnegative",
            "refunded_credits >= 0",
        )
        batch_op.create_check_constraint(
            "ck_credit_entry_allocation",
            "settled_credits + refunded_credits <= credits",
        )
        batch_op.create_check_constraint(
            "ck_credit_entry_version_positive",
            "version > 0",
        )
        batch_op.create_check_constraint(
            "ck_credit_entry_status",
            "status IN ('RESERVED', 'SETTLED', 'REFUNDED', 'RECONCILIATION_REQUIRED')",
        )
        batch_op.create_check_constraint(
            "ck_credit_entry_state_allocation",
            "(status = 'RESERVED' AND settled_credits = 0 AND refunded_credits = 0 "
            "AND settled_at IS NULL AND refunded_at IS NULL "
            "AND reconciliation_required_at IS NULL) OR "
            "(status = 'RECONCILIATION_REQUIRED' AND settled_credits = 0 "
            "AND refunded_credits = 0 AND settled_at IS NULL AND refunded_at IS NULL "
            "AND reconciliation_required_at IS NOT NULL "
            "AND reconciliation_reason IS NOT NULL) OR "
            "(status = 'SETTLED' AND settled_credits = credits AND refunded_credits = 0 "
            "AND settled_at IS NOT NULL AND refunded_at IS NULL) OR "
            "(status = 'REFUNDED' AND settled_credits = 0 AND refunded_credits = credits "
            "AND refunded_at IS NOT NULL AND settled_at IS NULL)",
        )
    op.create_index(
        "ix_workspace_credit_entries_status",
        "workspace_credit_entries",
        ["status"],
    )

    op.create_table(
        "workspace_credit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("credit_entry_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("generation_job_id", sa.String(length=36), nullable=True),
        sa.Column("event_key", sa.String(length=160), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("credits", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("balance_delta", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=240), nullable=False),
        sa.Column("actor_type", sa.String(length=80), server_default="SYSTEM", nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("credits >= 0", name="ck_credit_event_credits_nonnegative"),
        sa.CheckConstraint("balance_after >= 0", name="ck_credit_event_balance_nonnegative"),
        sa.ForeignKeyConstraint(
            ["credit_entry_id"],
            ["workspace_credit_entries.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["generation_job_id"],
            ["generation_jobs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credit_entry_id", "event_key", name="uq_credit_event_entry_key"),
    )
    op.create_index(
        "ix_workspace_credit_events_credit_entry_id",
        "workspace_credit_events",
        ["credit_entry_id"],
    )
    op.create_index(
        "ix_workspace_credit_events_workspace_id",
        "workspace_credit_events",
        ["workspace_id"],
    )
    op.create_index(
        "ix_workspace_credit_events_project_id",
        "workspace_credit_events",
        ["project_id"],
    )
    op.create_index(
        "ix_workspace_credit_events_generation_job_id",
        "workspace_credit_events",
        ["generation_job_id"],
    )
    op.create_index(
        "ix_workspace_credit_events_event_type",
        "workspace_credit_events",
        ["event_type"],
    )
    op.create_index(
        "ix_workspace_credit_events_created_at",
        "workspace_credit_events",
        ["created_at"],
    )

    entry_source = sa.table(
        "workspace_credit_entries",
        sa.column("id", sa.String()),
        sa.column("workspace_id", sa.String()),
        sa.column("project_id", sa.String()),
        sa.column("generation_job_id", sa.String()),
        sa.column("credits", sa.Integer()),
        sa.column("balance_after", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    entries = list(op.get_bind().execute(sa.select(entry_source)).mappings())
    event_table = sa.table(
        "workspace_credit_events",
        sa.column("id", sa.String()),
        sa.column("credit_entry_id", sa.String()),
        sa.column("workspace_id", sa.String()),
        sa.column("project_id", sa.String()),
        sa.column("generation_job_id", sa.String()),
        sa.column("event_key", sa.String()),
        sa.column("event_type", sa.String()),
        sa.column("credits", sa.Integer()),
        sa.column("balance_delta", sa.Integer()),
        sa.column("balance_after", sa.Integer()),
        sa.column("reason", sa.String()),
        sa.column("actor_type", sa.String()),
        sa.column("metadata_json", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    for entry in entries:
        op.bulk_insert(
            event_table,
            [
                {
                    "id": str(uuid.uuid4()),
                    "credit_entry_id": entry["id"],
                    "workspace_id": entry["workspace_id"],
                    "project_id": entry["project_id"],
                    "generation_job_id": entry["generation_job_id"],
                    "event_key": "migration:settled",
                    "event_type": "LEGACY_SETTLED",
                    "credits": entry["credits"],
                    "balance_delta": -entry["credits"],
                    "balance_after": entry["balance_after"],
                    "reason": "LEGACY_CHARGE_MIGRATED",
                    "actor_type": "MIGRATION",
                    "metadata_json": {"revision": revision},
                    "created_at": entry["created_at"],
                }
            ],
        )


def downgrade() -> None:
    tables = _tables()
    if "workspace_credit_entries" not in tables:
        return
    unsupported = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT status FROM workspace_credit_entries "
                "WHERE status != 'SETTLED' OR reason != 'LEGACY_CHARGE_MIGRATED' "
                "OR version != 1 OR settled_credits != credits OR refunded_credits != 0 LIMIT 1"
            )
        )
        .scalar()
    )
    if unsupported is not None:
        raise RuntimeError("workspace credit lifecycle downgrade would lose non-settled wallet state")
    if "workspace_credit_events" in tables:
        non_legacy_event = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT event_type FROM workspace_credit_events "
                    "WHERE event_key != 'migration:settled' "
                    "OR event_type != 'LEGACY_SETTLED' LIMIT 1"
                )
            )
            .scalar()
        )
        if non_legacy_event is not None:
            raise RuntimeError("workspace credit lifecycle downgrade would lose v2 audit events")
    if _duplicate_value(
        "workspace_id, idempotency_key",
        require_non_null="workspace_id IS NOT NULL",
    ):
        raise RuntimeError("workspace credit lifecycle downgrade cannot restore workspace-scoped idempotency")
    if "workspace_credit_events" in tables:
        op.drop_index("ix_workspace_credit_events_created_at", table_name="workspace_credit_events")
        op.drop_index("ix_workspace_credit_events_event_type", table_name="workspace_credit_events")
        op.drop_index(
            "ix_workspace_credit_events_generation_job_id",
            table_name="workspace_credit_events",
        )
        op.drop_index("ix_workspace_credit_events_project_id", table_name="workspace_credit_events")
        op.drop_index("ix_workspace_credit_events_workspace_id", table_name="workspace_credit_events")
        op.drop_index(
            "ix_workspace_credit_events_credit_entry_id",
            table_name="workspace_credit_events",
        )
        op.drop_table("workspace_credit_events")

    op.drop_index(
        "ix_workspace_credit_entries_status",
        table_name="workspace_credit_entries",
    )

    with op.batch_alter_table("workspace_credit_entries") as batch_op:
        batch_op.drop_constraint("ck_credit_entry_state_allocation", type_="check")
        batch_op.drop_constraint("ck_credit_entry_status", type_="check")

    op.execute(
        sa.text("UPDATE workspace_credit_entries SET status = 'CHARGED', reason = 'GENERATION_SUBMISSION'")
    )
    with op.batch_alter_table("workspace_credit_entries") as batch_op:
        batch_op.drop_constraint("ck_credit_entry_version_positive", type_="check")
        batch_op.drop_constraint("ck_credit_entry_allocation", type_="check")
        batch_op.drop_constraint("ck_credit_entry_refunded_nonnegative", type_="check")
        batch_op.drop_constraint("ck_credit_entry_settled_nonnegative", type_="check")
        batch_op.drop_constraint("uq_credit_entry_generation_job", type_="unique")
        batch_op.drop_constraint("uq_credit_entry_project_key", type_="unique")
        batch_op.create_unique_constraint(
            "uq_workspace_credit_entry_idempotency",
            ["workspace_id", "idempotency_key"],
        )
        batch_op.create_index(
            "ix_workspace_credit_entries_job",
            ["generation_job_id"],
            unique=False,
        )
        batch_op.drop_column("reconciliation_reason")
        batch_op.drop_column("reconciled_at")
        batch_op.drop_column("reconciliation_required_at")
        batch_op.drop_column("refunded_at")
        batch_op.drop_column("settled_at")
        batch_op.drop_column("reserved_at")
        batch_op.drop_column("version")
        batch_op.drop_column("refunded_credits")
        batch_op.drop_column("settled_credits")
    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.drop_constraint("ck_generation_job_quoted_credits", type_="check")
        batch_op.drop_column("quoted_credits")
        batch_op.drop_column("workspace_credit_required")
