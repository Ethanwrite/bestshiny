"""Add fixed-offer DePay payment intents and atomic Pro activation.

Revision ID: 0033_fixed_depay_pro_offer
Revises: 0032_depay_payment_links
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_fixed_depay_pro_offer"
down_revision: str | None = "0032_depay_payment_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "depay_checkout_sessions" not in tables and "onchain_payment_intents" not in tables:
        return
    required = {"depay_checkout_sessions", "onchain_payment_intents"}
    missing = required.difference(tables)
    if missing:
        raise RuntimeError(f"fixed DePay offer migration requires missing tables: {sorted(missing)}")

    with op.batch_alter_table("onchain_payment_intents") as batch:
        batch.alter_column(
            "wallet_binding_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
        batch.alter_column(
            "from_address",
            existing_type=sa.String(length=42),
            nullable=True,
        )

    with op.batch_alter_table("depay_checkout_sessions") as batch:
        batch.add_column(sa.Column("payment_intent_id", sa.String(length=36)))
        batch.create_foreign_key(
            "fk_depay_checkout_payment_intent",
            "onchain_payment_intents",
            ["payment_intent_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_depay_checkout_payment_intent",
            ["payment_intent_id"],
        )
        batch.create_index(
            "ix_depay_checkout_sessions_payment_intent_id",
            ["payment_intent_id"],
        )


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "onchain_payment_intents" not in tables:
        return
    unbound = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM onchain_payment_intents WHERE wallet_binding_id IS NULL")
    ).scalar_one()
    if unbound:
        raise RuntimeError("downgrade would discard DePay payment-intent ownership")

    if "depay_checkout_sessions" in tables:
        with op.batch_alter_table("depay_checkout_sessions") as batch:
            batch.drop_index("ix_depay_checkout_sessions_payment_intent_id")
            batch.drop_constraint("uq_depay_checkout_payment_intent", type_="unique")
            batch.drop_constraint("fk_depay_checkout_payment_intent", type_="foreignkey")
            batch.drop_column("payment_intent_id")

    with op.batch_alter_table("onchain_payment_intents") as batch:
        batch.alter_column(
            "from_address",
            existing_type=sa.String(length=42),
            nullable=False,
        )
        batch.alter_column(
            "wallet_binding_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
