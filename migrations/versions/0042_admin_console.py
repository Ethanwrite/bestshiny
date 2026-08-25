"""Add platform RBAC and append-only Admin Console operations.

Revision ID: 0042_admin_console
Revises: 0041_embedding_space
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_admin_console"
down_revision: str | None = "0041_embedding_space"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = _tables()
    # Recovery snapshots stamped at an older revision may intentionally contain
    # only one product domain.  Later migrations can create the model registry in
    # those snapshots even though the commercial account domain is absent, so use
    # the account-domain pair as the completeness sentinel.
    if "users" not in tables and "workspaces" not in tables:
        return
    required = {"users", "workspaces", "model_definitions", "model_capability_profiles"}
    missing = required.difference(tables)
    if missing:
        raise RuntimeError(f"admin console migration requires missing tables: {sorted(missing)}")

    if "platform_role" not in _columns("users"):
        op.add_column(
            "users",
            sa.Column("platform_role", sa.String(40), nullable=False, server_default="USER"),
        )

    model_columns = _columns("model_definitions")
    additions = (
        sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("user_visible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("router_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("lifecycle_status", sa.String(40), nullable=False, server_default="CONFIGURED"),
        sa.Column("pricing_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("last_live_test_at", sa.DateTime(timezone=True)),
    )
    for column in additions:
        if column.name not in model_columns:
            op.add_column("model_definitions", column)
    op.execute(
        "UPDATE model_definitions SET lifecycle_status = CASE "
        "WHEN enabled = false THEN 'DISABLED' "
        "WHEN live_enabled = true THEN 'LIVE' ELSE 'CONFIGURED' END"
    )
    op.create_index(
        "ix_model_definitions_lifecycle_status",
        "model_definitions",
        ["lifecycle_status"],
        if_not_exists=True,
    )

    if "admin_credit_adjustments" not in tables:
        op.create_table(
            "admin_credit_adjustments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("workspace_id", sa.String(36), nullable=False),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("operator_user_id", sa.String(36), nullable=False),
            sa.Column("idempotency_key", sa.String(200), nullable=False),
            sa.Column("delta", sa.Integer(), nullable=False),
            sa.Column("before_balance", sa.Integer(), nullable=False),
            sa.Column("after_balance", sa.Integer(), nullable=False),
            sa.Column("reason", sa.String(500), nullable=False),
            sa.Column("reference", sa.String(240)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("delta != 0", name="ck_admin_credit_adjustment_delta_nonzero"),
            sa.CheckConstraint("before_balance >= 0", name="ck_admin_credit_adjustment_before_nonnegative"),
            sa.CheckConstraint("after_balance >= 0", name="ck_admin_credit_adjustment_after_nonnegative"),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["operator_user_id"], ["users.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint("idempotency_key", name="uq_admin_credit_adjustment_idempotency"),
        )
        for column in ("workspace_id", "user_id", "operator_user_id", "created_at"):
            op.create_index(f"ix_admin_credit_adjustments_{column}", "admin_credit_adjustments", [column])
        op.create_index(
            "ix_admin_credit_adjustments_workspace_created",
            "admin_credit_adjustments",
            ["workspace_id", "created_at"],
        )

    if "admin_audit_logs" not in tables:
        op.create_table(
            "admin_audit_logs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("actor_user_id", sa.String(36), nullable=False),
            sa.Column("actor_role", sa.String(40), nullable=False),
            sa.Column("action", sa.String(100), nullable=False),
            sa.Column("entity_type", sa.String(80), nullable=False),
            sa.Column("entity_id", sa.String(160), nullable=False),
            sa.Column("before_json", sa.JSON(), nullable=False),
            sa.Column("after_json", sa.JSON(), nullable=False),
            sa.Column("reason", sa.String(500)),
            sa.Column("request_id", sa.String(160), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        )
        for column in ("actor_user_id", "action", "entity_type", "entity_id", "request_id", "created_at"):
            op.create_index(f"ix_admin_audit_logs_{column}", "admin_audit_logs", [column])
        op.create_index(
            "ix_admin_audit_entity_created",
            "admin_audit_logs",
            ["entity_type", "entity_id", "created_at"],
        )
        op.create_index(
            "ix_admin_audit_actor_created",
            "admin_audit_logs",
            ["actor_user_id", "created_at"],
        )

    if "model_verifications" not in tables:
        op.create_table(
            "model_verifications",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("model_definition_id", sa.String(36), nullable=False),
            sa.Column("operator_user_id", sa.String(36), nullable=False),
            sa.Column("idempotency_key", sa.String(200), nullable=False),
            sa.Column("protocol_version", sa.String(120), nullable=False),
            sa.Column("result", sa.String(40), nullable=False),
            sa.Column("evidence_reference", sa.String(500), nullable=False),
            sa.Column("billable", sa.Boolean(), nullable=False),
            sa.Column("latency_ms", sa.Float()),
            sa.Column("detail", sa.String(500)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["model_definition_id"], ["model_definitions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["operator_user_id"], ["users.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint("model_definition_id", "idempotency_key", name="uq_model_verification_key"),
        )
        for column in ("model_definition_id", "operator_user_id", "result", "created_at"):
            op.create_index(f"ix_model_verifications_{column}", "model_verifications", [column])
        op.create_index(
            "ix_model_verification_created",
            "model_verifications",
            ["model_definition_id", "created_at"],
        )

    if "provider_controls" not in tables:
        op.create_table(
            "provider_controls",
            sa.Column("provider", sa.String(80), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("disabled_reason", sa.String(500)),
            sa.Column("changed_by_user_id", sa.String(36)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["changed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        )
        op.create_index(
            "ix_provider_controls_changed_by_user_id",
            "provider_controls",
            ["changed_by_user_id"],
        )

    _install_append_only_triggers()


def _install_append_only_triggers() -> None:
    tables = ("admin_credit_adjustments", "admin_audit_logs", "model_verifications")
    if op.get_bind().dialect.name == "sqlite":
        for table in tables:
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER IF NOT EXISTS trg_{table}_append_only_{operation.lower()} "
                    f"BEFORE {operation} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
                )
        return
    op.execute(
        "CREATE OR REPLACE FUNCTION enforce_admin_append_only() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION 'admin audit table is append-only' USING ERRCODE = '23000'; "
        "RETURN OLD; END; $$"
    )
    for table in tables:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION enforce_admin_append_only()"
        )


def downgrade() -> None:
    tables = _tables()
    for table in ("provider_controls", "model_verifications", "admin_audit_logs", "admin_credit_adjustments"):
        if table in tables:
            op.drop_table(table)
    if "model_definitions" in tables:
        op.drop_index(
            "ix_model_definitions_lifecycle_status",
            table_name="model_definitions",
            if_exists=True,
        )
        for name in (
            "last_live_test_at",
            "last_verified_at",
            "pricing_metadata",
            "lifecycle_status",
            "router_enabled",
            "user_visible",
            "display_name",
        ):
            if name in _columns("model_definitions"):
                op.drop_column("model_definitions", name)
    if "users" in tables and "platform_role" in _columns("users"):
        op.drop_column("users", "platform_role")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS enforce_admin_append_only()")
