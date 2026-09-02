"""Add XunHuPay CNY checkout and unified credit-ledger settlement evidence.

Revision ID: 0068_xunhupay
Revises: 0067_eip3009_relayer
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068_xunhupay"
down_revision: str | None = "0067_eip3009_relayer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _drop_append_only_trigger(table_name: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        for operation in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only_{operation}")
        return
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only ON {table_name}")


def _create_append_only_trigger(table_name: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER trg_{table_name}_append_only_{operation.lower()} "
                f"BEFORE {operation} ON {table_name} "
                f"BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END"
            )
        return
    op.execute(
        f"CREATE TRIGGER trg_{table_name}_append_only BEFORE UPDATE OR DELETE ON {table_name} "
        "FOR EACH ROW EXECUTE FUNCTION enforce_payment_ledger_append_only()"
    )


def upgrade() -> None:
    tables = _tables()
    new_tables = {"xunhupay_checkout_sessions", "xunhupay_settlements"}
    present = new_tables.intersection(tables)
    if "workspaces" not in tables and not present:
        return
    required = {
        "users",
        "workspaces",
        "onchain_payment_intents",
        "workspace_credit_ledger_entries",
        "onchain_payments",
    }
    missing = required.difference(tables)
    if missing:
        raise RuntimeError(f"XunHuPay migration requires missing tables: {sorted(missing)}")
    if present:
        raise RuntimeError(f"XunHuPay migration found partial pre-existing tables: {sorted(present)}")

    with op.batch_alter_table("onchain_payment_intents") as batch:
        batch.drop_constraint("ck_payment_intent_chain_positive", type_="check")
        batch.alter_column("chain_id", existing_type=sa.BigInteger(), nullable=True)
        batch.alter_column("to_address", existing_type=sa.String(length=42), nullable=True)
        batch.alter_column("token_address", existing_type=sa.String(length=42), nullable=True)
        batch.create_check_constraint(
            "ck_payment_intent_chain_positive", "chain_id IS NULL OR chain_id > 0"
        )

    op.create_table(
        "xunhupay_checkout_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("payment_order_id", sa.String(length=36), nullable=False),
        sa.Column("trade_order_id", sa.String(length=32), nullable=False),
        sa.Column("gateway_order_id", sa.String(length=64)),
        sa.Column("checkout_url", sa.Text()),
        sa.Column("qrcode_url", sa.Text()),
        sa.Column("credits_granted", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "credits_granted >= 0", name="ck_xunhupay_checkout_credits_nonnegative"
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PAID', 'EXPIRED', 'CANCELLED', 'RECONCILIATION_REQUIRED')",
            name="ck_xunhupay_checkout_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["payment_order_id"], ["onchain_payment_intents.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("payment_order_id", name="uq_xunhupay_checkout_payment_order"),
        sa.UniqueConstraint("trade_order_id", name="uq_xunhupay_checkout_trade_order"),
    )
    for column in (
        "workspace_id",
        "user_id",
        "payment_order_id",
        "trade_order_id",
        "gateway_order_id",
        "status",
        "expires_at",
        "paid_at",
    ):
        op.create_index(
            f"ix_xunhupay_checkout_sessions_{column}",
            "xunhupay_checkout_sessions",
            [column],
        )

    op.create_table(
        "xunhupay_settlements",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("checkout_session_id", sa.String(length=36), nullable=False),
        sa.Column("payment_order_id", sa.String(length=36), nullable=False),
        sa.Column("transaction_id", sa.String(length=64), nullable=False),
        sa.Column("open_order_id", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(length=20), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("credits_granted", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_xunhupay_settlement_amount_positive"),
        sa.CheckConstraint(
            "credits_granted >= 0", name="ck_xunhupay_settlement_credits_nonnegative"
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64", name="ck_xunhupay_settlement_payload_hash"
        ),
        sa.CheckConstraint(
            "status IN ('CREDITED', 'RECONCILIATION_REQUIRED')",
            name="ck_xunhupay_settlement_status",
        ),
        sa.ForeignKeyConstraint(
            ["checkout_session_id"], ["xunhupay_checkout_sessions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["payment_order_id"], ["onchain_payment_intents.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("payment_order_id", name="uq_xunhupay_settlement_payment_order"),
        sa.UniqueConstraint("transaction_id", name="uq_xunhupay_settlement_transaction"),
        sa.UniqueConstraint("open_order_id", name="uq_xunhupay_settlement_open_order"),
    )
    for column in (
        "checkout_session_id",
        "payment_order_id",
        "transaction_id",
        "open_order_id",
        "status",
        "created_at",
    ):
        op.create_index(
            f"ix_xunhupay_settlements_{column}", "xunhupay_settlements", [column]
        )

    _drop_append_only_trigger("workspace_credit_ledger_entries")
    with op.batch_alter_table("workspace_credit_ledger_entries") as batch:
        batch.drop_constraint("ck_workspace_credit_ledger_entry_type", type_="check")
        batch.alter_column("payment_id", existing_type=sa.String(length=36), nullable=True)
        batch.alter_column("chain_id", existing_type=sa.BigInteger(), nullable=True)
        batch.add_column(sa.Column("xunhupay_settlement_id", sa.String(length=36)))
        batch.create_foreign_key(
            "fk_credit_ledger_xunhupay_settlement",
            "xunhupay_settlements",
            ["xunhupay_settlement_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_workspace_credit_ledger_entry_type",
            "entry_type IN ('USDC_PURCHASE', 'USDC_REORG_REVERSAL', 'CNY_PURCHASE')",
        )
        batch.create_check_constraint(
            "ck_workspace_credit_ledger_payment_source",
            "(payment_id IS NOT NULL AND xunhupay_settlement_id IS NULL) OR "
            "(payment_id IS NULL AND xunhupay_settlement_id IS NOT NULL)",
        )
    op.create_index(
        "ix_workspace_credit_ledger_entries_xunhupay_settlement_id",
        "workspace_credit_ledger_entries",
        ["xunhupay_settlement_id"],
    )
    _create_append_only_trigger("workspace_credit_ledger_entries")
    _create_append_only_trigger("xunhupay_settlements")


def downgrade() -> None:
    tables = _tables()
    if "xunhupay_checkout_sessions" not in tables:
        return
    sold = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM onchain_payment_intents WHERE provider = 'XUNHUPAY'")
    ).scalar_one()
    if sold:
        raise RuntimeError("downgrade would discard XunHuPay payment orders")

    _drop_append_only_trigger("xunhupay_settlements")
    _drop_append_only_trigger("workspace_credit_ledger_entries")
    op.drop_index(
        "ix_workspace_credit_ledger_entries_xunhupay_settlement_id",
        table_name="workspace_credit_ledger_entries",
    )
    with op.batch_alter_table("workspace_credit_ledger_entries") as batch:
        batch.drop_constraint("ck_workspace_credit_ledger_payment_source", type_="check")
        batch.drop_constraint("ck_workspace_credit_ledger_entry_type", type_="check")
        batch.drop_constraint("fk_credit_ledger_xunhupay_settlement", type_="foreignkey")
        batch.drop_column("xunhupay_settlement_id")
        batch.alter_column("payment_id", existing_type=sa.String(length=36), nullable=False)
        batch.alter_column("chain_id", existing_type=sa.BigInteger(), nullable=False)
        batch.create_check_constraint(
            "ck_workspace_credit_ledger_entry_type",
            "entry_type IN ('USDC_PURCHASE', 'USDC_REORG_REVERSAL')",
        )
    _create_append_only_trigger("workspace_credit_ledger_entries")
    op.drop_table("xunhupay_settlements")
    op.drop_table("xunhupay_checkout_sessions")

    with op.batch_alter_table("onchain_payment_intents") as batch:
        batch.drop_constraint("ck_payment_intent_chain_positive", type_="check")
        batch.alter_column("chain_id", existing_type=sa.BigInteger(), nullable=False)
        batch.alter_column("to_address", existing_type=sa.String(length=42), nullable=False)
        batch.alter_column("token_address", existing_type=sa.String(length=42), nullable=False)
        batch.create_check_constraint("ck_payment_intent_chain_positive", "chain_id > 0")
