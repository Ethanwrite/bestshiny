"""Add durable EIP-3009 authorizations and the relayer nonce lock.

Revision ID: 0067_eip3009_relayer
Revises: 0066_dynamic_depay_packages
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067_eip3009_relayer"
down_revision: str | None = "0066_dynamic_depay_packages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    present = {"eip3009_authorizations", "relayer_account_states"}.intersection(tables)
    if "workspaces" not in tables and not present:
        return
    required = {"workspaces", "users", "onchain_payment_intents"}
    missing = required.difference(tables)
    if missing:
        raise RuntimeError(f"EIP-3009 migration requires missing tables: {sorted(missing)}")
    if present:
        raise RuntimeError(f"EIP-3009 migration found partial tables: {sorted(present)}")

    op.create_table(
        "eip3009_authorizations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("payment_intent_id", sa.String(length=36), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("token_address", sa.String(length=42), nullable=False),
        sa.Column("from_address", sa.String(length=42), nullable=False),
        sa.Column("to_address", sa.String(length=42), nullable=False),
        sa.Column("value_microunits", sa.BigInteger(), nullable=False),
        sa.Column("valid_after", sa.BigInteger(), nullable=False),
        sa.Column("valid_before", sa.BigInteger(), nullable=False),
        sa.Column("nonce", sa.String(length=66), nullable=False),
        sa.Column("typed_data_hash", sa.String(length=64), nullable=False),
        sa.Column("signature_hash", sa.String(length=64)),
        sa.Column("raw_transaction", sa.Text()),
        sa.Column("relayer_address", sa.String(length=42), nullable=False),
        sa.Column("relayer_nonce", sa.BigInteger()),
        sa.Column("transaction_hash", sa.String(length=66)),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(length=120)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("chain_id > 0", name="ck_eip3009_authorization_chain_positive"),
        sa.CheckConstraint("value_microunits > 0", name="ck_eip3009_authorization_value_positive"),
        sa.CheckConstraint("valid_after >= 0", name="ck_eip3009_authorization_valid_after"),
        sa.CheckConstraint("valid_before > valid_after", name="ck_eip3009_authorization_window"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_eip3009_authorization_attempts"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SUBMITTING', 'SUBMITTED', 'CONFIRMED', 'FAILED', "
            "'EXPIRED', 'CANCELLED', "
            "'RECONCILIATION_REQUIRED')",
            name="ck_eip3009_authorization_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_intent_id"], ["onchain_payment_intents.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("payment_intent_id", name="uq_eip3009_authorization_payment_intent"),
        sa.UniqueConstraint("nonce", name="uq_eip3009_authorization_nonce"),
        sa.UniqueConstraint("transaction_hash", name="uq_eip3009_authorization_transaction_hash"),
    )
    for column in (
        "workspace_id",
        "user_id",
        "payment_intent_id",
        "from_address",
        "to_address",
        "valid_before",
        "transaction_hash",
        "status",
        "submitted_at",
        "confirmed_at",
    ):
        op.create_index(f"ix_eip3009_authorizations_{column}", "eip3009_authorizations", [column])

    op.create_table(
        "relayer_account_states",
        sa.Column("address", sa.String(length=42), primary_key=True),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("last_submitted_nonce", sa.BigInteger()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("chain_id > 0", name="ck_relayer_account_chain_positive"),
        sa.CheckConstraint(
            "last_submitted_nonce IS NULL OR last_submitted_nonce >= 0",
            name="ck_relayer_account_nonce_nonnegative",
        ),
    )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "eip3009_authorizations" not in tables:
        return
    count = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM eip3009_authorizations")).scalar_one()
    if count:
        raise RuntimeError("downgrade would discard EIP-3009 payment evidence")
    op.drop_table("relayer_account_states")
    op.drop_table("eip3009_authorizations")
