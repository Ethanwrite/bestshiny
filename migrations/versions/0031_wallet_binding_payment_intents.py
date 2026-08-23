"""Add wallet ownership challenges and Base USDC payment intents.

Revision ID: 0031_wallet_binding_payment_intents
Revises: 0030_alchemy_usdc_credit_ledger
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_wallet_binding_payment_intents"
down_revision: str | None = "0030_alchemy_usdc_credit_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "workspaces" not in tables and "users" not in tables:
        return
    required = {"workspaces", "users", "workspace_wallet_bindings", "onchain_payments"}
    missing = required.difference(tables)
    if missing:
        raise RuntimeError(f"wallet payment migration requires missing tables: {sorted(missing)}")

    op.create_table(
        "wallet_binding_challenges",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("address", sa.String(length=42), nullable=False),
        sa.Column("nonce_hash", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("message_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("chain_id > 0", name="ck_wallet_binding_challenge_chain_positive"),
        sa.CheckConstraint(
            "length(address) = 42",
            name="ck_wallet_binding_challenge_address_length",
        ),
        sa.CheckConstraint(
            "address = lower(address)",
            name="ck_wallet_binding_challenge_address_lowercase",
        ),
        sa.CheckConstraint(
            "length(message_hash) = 64",
            name="ck_wallet_binding_challenge_message_hash",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("nonce_hash", name="uq_wallet_binding_challenge_nonce_hash"),
    )
    for column in (
        "workspace_id",
        "user_id",
        "chain_id",
        "address",
        "expires_at",
        "consumed_at",
    ):
        op.create_index(
            f"ix_wallet_binding_challenges_{column}",
            "wallet_binding_challenges",
            [column],
        )

    op.create_table(
        "onchain_payment_intents",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("wallet_binding_id", sa.String(length=36), nullable=False),
        sa.Column("network", sa.String(length=80), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("from_address", sa.String(length=42), nullable=False),
        sa.Column("to_address", sa.String(length=42), nullable=False),
        sa.Column("token_address", sa.String(length=42), nullable=False),
        sa.Column("raw_amount_microunits", sa.BigInteger(), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("transaction_hash", sa.String(length=66)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("chain_id > 0", name="ck_payment_intent_chain_positive"),
        sa.CheckConstraint(
            "raw_amount_microunits > 0",
            name="ck_payment_intent_amount_positive",
        ),
        sa.CheckConstraint("credits > 0", name="ck_payment_intent_credits_positive"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SUBMITTED', 'PAID', 'EXPIRED', 'CANCELLED', "
            "'RECONCILIATION_REQUIRED')",
            name="ck_payment_intent_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["wallet_binding_id"],
            ["workspace_wallet_bindings.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "transaction_hash",
            name="uq_onchain_payment_intent_transaction_hash",
        ),
    )
    for column in (
        "workspace_id",
        "wallet_binding_id",
        "network",
        "chain_id",
        "from_address",
        "to_address",
        "token_address",
        "status",
        "transaction_hash",
        "expires_at",
    ):
        op.create_index(
            f"ix_onchain_payment_intents_{column}",
            "onchain_payment_intents",
            [column],
        )

    with op.batch_alter_table("onchain_payments") as batch:
        batch.add_column(sa.Column("payment_intent_id", sa.String(length=36)))
        batch.create_foreign_key(
            "fk_onchain_payments_payment_intent_id_onchain_payment_intents",
            "onchain_payment_intents",
            ["payment_intent_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_onchain_payments_payment_intent_id", ["payment_intent_id"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "onchain_payments" in tables:
        with op.batch_alter_table("onchain_payments") as batch:
            batch.drop_index("ix_onchain_payments_payment_intent_id")
            batch.drop_constraint(
                "fk_onchain_payments_payment_intent_id_onchain_payment_intents",
                type_="foreignkey",
            )
            batch.drop_column("payment_intent_id")
    if "onchain_payment_intents" in tables:
        op.drop_table("onchain_payment_intents")
    if "wallet_binding_challenges" in tables:
        op.drop_table("wallet_binding_challenges")
