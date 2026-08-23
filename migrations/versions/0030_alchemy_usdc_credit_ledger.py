"""Add authenticated Alchemy receipts and a Base USDC credit ledger.

Revision ID: 0030_alchemy_usdc_credit_ledger
Revises: 0029_project_style_lock
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_alchemy_usdc_credit_ledger"
down_revision: str | None = "0029_project_style_lock"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAYMENT_TABLES = (
    "alchemy_webhook_deliveries",
    "workspace_wallet_bindings",
    "onchain_payments",
    "workspace_credit_ledger_entries",
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _skip_recovery_or_require_complete_core() -> bool:
    tables = _tables()
    present = set(PAYMENT_TABLES).intersection(tables)
    if "workspaces" not in tables and not present:
        return True
    missing = {"workspaces", "users"}.difference(tables)
    if missing:
        raise RuntimeError(f"Alchemy payment migration requires missing tables: {sorted(missing)}")
    if present:
        raise RuntimeError(f"Alchemy payment migration found partial pre-existing tables: {sorted(present)}")
    return False


def _timestamps() -> tuple[sa.Column, sa.Column]:  # type: ignore[type-arg]
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    if _skip_recovery_or_require_complete_core():
        return

    op.create_table(
        "alchemy_webhook_deliveries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("provider_event_id", sa.String(length=160), nullable=False),
        sa.Column("webhook_id", sa.String(length=160), nullable=False),
        sa.Column("webhook_type", sa.String(length=80), nullable=False),
        sa.Column("network", sa.String(length=80), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("activity_count", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("credited_count", sa.Integer(), nullable=False),
        sa.Column("ignored_count", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(length=40), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("activity_count >= 0", name="ck_alchemy_delivery_activity_count"),
        sa.CheckConstraint("accepted_count >= 0", name="ck_alchemy_delivery_accepted_count"),
        sa.CheckConstraint("credited_count >= 0", name="ck_alchemy_delivery_credited_count"),
        sa.CheckConstraint("ignored_count >= 0", name="ck_alchemy_delivery_ignored_count"),
        sa.CheckConstraint("length(payload_hash) = 64", name="ck_alchemy_delivery_payload_hash"),
        sa.UniqueConstraint("provider_event_id", name="uq_alchemy_delivery_provider_event"),
    )
    for column in ("provider_event_id", "webhook_id", "network", "created_at"):
        op.create_index(
            f"ix_alchemy_webhook_deliveries_{column}",
            "alchemy_webhook_deliveries",
            [column],
        )

    op.create_table(
        "workspace_wallet_bindings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("address", sa.String(length=42), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("verified_by_user_id", sa.String(length=36)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("chain_id > 0", name="ck_workspace_wallet_chain_positive"),
        sa.CheckConstraint("length(address) = 42", name="ck_workspace_wallet_address_length"),
        sa.CheckConstraint("address = lower(address)", name="ck_workspace_wallet_address_lowercase"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'VERIFIED', 'REVOKED')",
            name="ck_workspace_wallet_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["verified_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("chain_id", "address", name="uq_workspace_wallet_chain_address"),
    )
    for column in ("workspace_id", "chain_id", "address", "status", "verified_by_user_id"):
        op.create_index(
            f"ix_workspace_wallet_bindings_{column}",
            "workspace_wallet_bindings",
            [column],
        )

    op.create_table(
        "onchain_payments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("network", sa.String(length=80), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("transaction_hash", sa.String(length=66), nullable=False),
        sa.Column("log_index", sa.String(length=66), nullable=False),
        sa.Column("block_number", sa.String(length=66), nullable=False),
        sa.Column("from_address", sa.String(length=42), nullable=False),
        sa.Column("to_address", sa.String(length=42), nullable=False),
        sa.Column("token_address", sa.String(length=42), nullable=False),
        sa.Column("token_decimals", sa.Integer(), nullable=False),
        sa.Column("raw_amount_microunits", sa.BigInteger(), nullable=False),
        sa.Column("workspace_id", sa.String(length=36)),
        sa.Column("wallet_binding_id", sa.String(length=36)),
        sa.Column("provider_event_id", sa.String(length=160), nullable=False),
        sa.Column("credits_granted", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("chain_id > 0", name="ck_onchain_payment_chain_positive"),
        sa.CheckConstraint("token_decimals = 6", name="ck_onchain_payment_usdc_decimals"),
        sa.CheckConstraint("raw_amount_microunits > 0", name="ck_onchain_payment_amount_positive"),
        sa.CheckConstraint("credits_granted >= 0", name="ck_onchain_payment_credits_nonnegative"),
        sa.CheckConstraint(
            "status IN ('RECEIVED', 'UNMATCHED', 'CREDITED', 'REMOVED', 'RECONCILIATION_REQUIRED')",
            name="ck_onchain_payment_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["wallet_binding_id"], ["workspace_wallet_bindings.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "network",
            "transaction_hash",
            "log_index",
            name="uq_onchain_payment_log",
        ),
    )
    for column in (
        "network",
        "chain_id",
        "transaction_hash",
        "from_address",
        "to_address",
        "token_address",
        "workspace_id",
        "wallet_binding_id",
        "provider_event_id",
        "status",
    ):
        op.create_index(f"ix_onchain_payments_{column}", "onchain_payments", [column])

    op.create_table(
        "workspace_credit_ledger_entries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("payment_id", sa.String(length=36), nullable=False),
        sa.Column("related_entry_id", sa.String(length=36)),
        sa.Column("external_reference", sa.String(length=240), nullable=False),
        sa.Column("entry_type", sa.String(length=60), nullable=False),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("balance_before", sa.Integer(), nullable=False),
        sa.Column("balance_after", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=20), nullable=False),
        sa.Column("raw_amount_microunits", sa.BigInteger(), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("credits > 0", name="ck_workspace_credit_ledger_credits_positive"),
        sa.CheckConstraint("balance_before >= 0", name="ck_workspace_credit_ledger_before_nonnegative"),
        sa.CheckConstraint("balance_after >= 0", name="ck_workspace_credit_ledger_after_nonnegative"),
        sa.CheckConstraint(
            "direction IN ('CREDIT', 'DEBIT')",
            name="ck_workspace_credit_ledger_direction",
        ),
        sa.CheckConstraint(
            "entry_type IN ('USDC_PURCHASE', 'USDC_REORG_REVERSAL')",
            name="ck_workspace_credit_ledger_entry_type",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_id"], ["onchain_payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["related_entry_id"], ["workspace_credit_ledger_entries.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("external_reference", name="uq_workspace_credit_ledger_external_reference"),
    )
    for column in (
        "workspace_id",
        "payment_id",
        "related_entry_id",
        "entry_type",
        "created_at",
    ):
        op.create_index(
            f"ix_workspace_credit_ledger_entries_{column}",
            "workspace_credit_ledger_entries",
            [column],
        )

    _install_append_only_triggers()


def _install_append_only_triggers() -> None:
    if op.get_bind().dialect.name == "sqlite":
        for table_name in ("alchemy_webhook_deliveries", "workspace_credit_ledger_entries"):
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER trg_{table_name}_append_only_{operation.lower()} "
                    f"BEFORE {operation} ON {table_name} "
                    f"BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END"
                )
        return
    op.execute(
        "CREATE OR REPLACE FUNCTION enforce_payment_ledger_append_only() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '23000'; "
        "RETURN OLD; END; $$"
    )
    for table_name in ("alchemy_webhook_deliveries", "workspace_credit_ledger_entries"):
        op.execute(
            f"CREATE TRIGGER trg_{table_name}_append_only BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION enforce_payment_ledger_append_only()"
        )


def downgrade() -> None:
    tables = _tables()
    if "workspace_credit_ledger_entries" not in tables:
        return
    for table_name in reversed(PAYMENT_TABLES):
        op.drop_table(table_name)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS enforce_payment_ledger_append_only()")
