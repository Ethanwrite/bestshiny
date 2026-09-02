"""Snapshot dynamic DePay package pricing on payment orders.

`onchain_payment_intents` is the PaymentOrder table. Until now it carried only
the derived `raw_amount_microunits` and `credits`, so nothing recorded *which*
offer an order was sold under. With three DePay packages that can be repriced,
settlement has to compare a paid amount against the terms frozen at checkout,
not against whatever the catalogue says today.

Five columns freeze that snapshot: `sku`, `amount`, `currency`,
`pricing_version` and `provider`. All arrive with server defaults so existing
rows stay valid, then get backfilled to describe what they actually were: a
DePay fixed-offer order (no wallet binding) or a direct wallet payment.

Revision ID: 0066_dynamic_depay_packages
Revises: 0065_rebind_pro_prompt_refiner
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0066_dynamic_depay_packages"
down_revision: str | None = "0065_rebind_pro_prompt_refiner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "onchain_payment_intents"
_LIVE_SKUS = ("starter_20", "creator_50", "pro_100")


def upgrade() -> None:
    if _TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(
            sa.Column(
                "sku",
                sa.String(length=80),
                nullable=False,
                server_default=sa.text("'legacy_direct'"),
            )
        )
        batch.add_column(
            sa.Column(
                "amount",
                sa.Numeric(18, 6),
                nullable=False,
                server_default=sa.text("0.01"),
            )
        )
        batch.add_column(
            sa.Column(
                "currency",
                sa.String(length=20),
                nullable=False,
                server_default=sa.text("'USDC'"),
            )
        )
        batch.add_column(
            sa.Column(
                "pricing_version",
                sa.String(length=80),
                nullable=False,
                server_default=sa.text("'legacy'"),
            )
        )
        batch.add_column(
            sa.Column(
                "provider",
                sa.String(length=40),
                nullable=False,
                server_default=sa.text("'ALCHEMY'"),
            )
        )
        # Inside the batch: SQLite cannot ALTER a constraint onto an existing
        # table, and the copy-and-move that adds the columns is the only place
        # this can be attached. The 0.01 server default satisfies it already.
        batch.create_check_constraint(
            "ck_payment_intent_snapshot_amount_positive", "amount > 0"
        )
    # A DePay order never had a wallet binding; a direct wallet payment always
    # did. That is the only signal historical rows carry, and it is enough.
    op.get_bind().execute(
        sa.text(
            f"UPDATE {_TABLE} SET"
            " amount = raw_amount_microunits / 1000000.0,"
            " sku = CASE WHEN wallet_binding_id IS NULL"
            " THEN 'legacy_depay_fixed' ELSE 'legacy_direct' END,"
            " pricing_version = CASE WHEN wallet_binding_id IS NULL"
            " THEN 'legacy_depay_v1' ELSE 'legacy_microunits_per_credit' END,"
            " provider = CASE WHEN wallet_binding_id IS NULL"
            " THEN 'DEPAY' ELSE 'ALCHEMY' END"
        )
    )
    op.create_index("ix_onchain_payment_intents_provider", _TABLE, ["provider"])


def downgrade() -> None:
    if _TABLE not in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    skus = ", ".join(f"'{sku}'" for sku in _LIVE_SKUS)
    sold = op.get_bind().execute(
        sa.text(f"SELECT COUNT(*) FROM {_TABLE} WHERE sku IN ({skus})")
    ).scalar_one()
    if sold:
        raise RuntimeError("downgrade would discard dynamic DePay order snapshots")
    op.drop_index("ix_onchain_payment_intents_provider", table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(
            "ck_payment_intent_snapshot_amount_positive", type_="check"
        )
        batch.drop_column("provider")
        batch.drop_column("pricing_version")
        batch.drop_column("currency")
        batch.drop_column("amount")
        batch.drop_column("sku")
