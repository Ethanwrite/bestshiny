"""Add DePay checkout sessions and authenticated callback receipts.

Revision ID: 0032_depay_payment_links
Revises: 0031_wallet_binding_payment_intents
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_depay_payment_links"
down_revision: str | None = "0031_wallet_binding_payment_intents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    present = {"depay_checkout_sessions", "depay_webhook_deliveries"}.intersection(tables)
    if "workspaces" not in tables and not present:
        return
    required = {"workspaces", "users", "onchain_payments"}
    missing = required.difference(tables)
    if missing:
        raise RuntimeError(f"DePay payment migration requires missing tables: {sorted(missing)}")
    if present:
        raise RuntimeError(f"DePay payment migration found partial tables: {sorted(present)}")

    op.create_table(
        "depay_checkout_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_quantity", sa.Integer(), nullable=False),
        sa.Column("credits_granted", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payment_id", sa.String(length=36)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("requested_quantity > 0", name="ck_depay_checkout_quantity_positive"),
        sa.CheckConstraint("credits_granted >= 0", name="ck_depay_checkout_credits_nonnegative"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PAID', 'EXPIRED', 'CANCELLED', 'RECONCILIATION_REQUIRED')",
            name="ck_depay_checkout_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_id"], ["onchain_payments.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("token_hash", name="uq_depay_checkout_token_hash"),
    )
    for column in (
        "workspace_id",
        "user_id",
        "status",
        "payment_id",
        "expires_at",
        "paid_at",
    ):
        op.create_index(f"ix_depay_checkout_sessions_{column}", "depay_checkout_sessions", [column])

    op.create_table(
        "depay_webhook_deliveries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("event_key", sa.String(length=240), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("link_id", sa.String(length=160), nullable=False),
        sa.Column("checkout_session_id", sa.String(length=36)),
        sa.Column("payment_id", sa.String(length=36)),
        sa.Column("result", sa.String(length=60), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(payload_hash) = 64", name="ck_depay_delivery_payload_hash"),
        sa.ForeignKeyConstraint(["checkout_session_id"], ["depay_checkout_sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_id"], ["onchain_payments.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("event_key", name="uq_depay_delivery_event_key"),
    )
    for column in (
        "event_key",
        "link_id",
        "checkout_session_id",
        "payment_id",
        "result",
        "created_at",
    ):
        op.create_index(f"ix_depay_webhook_deliveries_{column}", "depay_webhook_deliveries", [column])

    if op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_depay_deliveries_append_only_{operation.lower()} "
                f"BEFORE {operation} ON depay_webhook_deliveries "
                "BEGIN SELECT RAISE(ABORT, 'depay_webhook_deliveries is append-only'); END"
            )
    else:
        op.execute(
            "CREATE TRIGGER trg_depay_webhook_deliveries_append_only "
            "BEFORE UPDATE OR DELETE ON depay_webhook_deliveries "
            "FOR EACH ROW EXECUTE FUNCTION enforce_payment_ledger_append_only()"
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "depay_checkout_sessions" not in tables:
        return
    count = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM depay_checkout_sessions")).scalar_one()
    if count:
        raise RuntimeError("downgrade would discard DePay checkout/payment evidence")
    op.drop_table("depay_webhook_deliveries")
    op.drop_table("depay_checkout_sessions")
