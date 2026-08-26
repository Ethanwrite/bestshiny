"""Price per provider, model, mode and resolution — with a source and a date.

The registry priced a model with one `estimated_per_second` and then scaled it
by a resolution multiplier shared by every provider on the platform:

    {"720p": 1.0, "1080p": 1.30, "2k": 1.65, "4k": 2.4}

That table is one vendor's price curve treated as physics. Against Volcengine
Ark's own published rates for Seedance 2.5 it is wrong in both directions at
once: Ark charges 18.71 CNY for a 5s 1080p clip against 7.56 CNY at 720p — a
factor of **2.47**, not 1.30 — and 480p, which Ark sells at 0.44x of 720p, has no
entry at all, so it was quoted as though it were 720p. Twelve billable models
shared that curve; exactly one of them had any pricing provenance recorded.

This replaces it with a row per (provider, model, input mode, resolution),
carrying the price in the provider's own currency and billing unit, the FX rate
used to reach USD, and the URL and date it was read from. Two prices, not one:
providers bill on things you cannot know before the job exists — Ark settles on
`usage.completion_tokens` — and publish a per-second typical price so a
reservation can be taken up front. Conflating them is how a reservation and a
debit quietly stop agreeing.

`pricing_status` on `model_definitions` defaults to UNVERIFIED, which is the
honest state of every model here except the one whose rates were checked against
the vendor page. A billable model without a confirmed price is refused a paid
route rather than quoted from a placeholder.

Seeded here: Volcengine Ark `doubao-seedance-2-5-260628`, no-video-input, at
480p / 720p / 1080p, from the Ark pricing page read 2026-08-26, converted at the
PBOC/CFETS central parity for that day. The **video-input** rates are published
as a range that depends on input length, so they are deliberately *not* seeded —
an unseeded mode fails closed, which is the correct answer to "we do not know".
The 1080p promotional rate is seeded as its own dated row rather than folded into
the base price, so it expires by itself.

Revision ID: 0044_model_pricing
Revises: 0043_seedance_25_ark
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0044_model_pricing"
down_revision: str | None = "0043_seedance_25_ark"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ARK_MODEL_ID = "doubao-seedance-2-5-260628"
ARK_PRICING_URL = "https://www.volcengine.com/docs/82379/1544106"
USD_PER_CNY = "0.14743"
FX_SOURCE = "PBOC/CFETS central parity 2026-08-26 (100 USD = 678.29 CNY)"
CHECKED_AT = datetime(2026, 8, 26, tzinfo=UTC)

ESTIMATE_FORMULA = "estimate_unit_price * duration_seconds * usd_per_currency"
SETTLEMENT_FORMULA = "unit_price * usage.completion_tokens / 1000000 * usd_per_currency"

# (resolution, CNY per 1M completion tokens, CNY per second typical, notes)
ARK_NO_VIDEO_INPUT = [
    ("480p", "70.00", "0.672", "typical 3.36 CNY per 5s, 16:9, no video input"),
    ("720p", "70.00", "1.512", "typical 7.56 CNY per 5s, 16:9, no video input"),
    ("1080p", "77.00", "3.742", "list rate; typical 18.71 CNY per 5s, 16:9, no video input"),
]


def upgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())

    if "model_definitions" in tables:
        columns = {item["name"] for item in sa.inspect(connection).get_columns("model_definitions")}
        if "pricing_status" not in columns:
            op.add_column(
                "model_definitions",
                sa.Column(
                    "pricing_status",
                    sa.String(24),
                    nullable=False,
                    server_default="UNVERIFIED",
                ),
            )
            op.create_index(
                "ix_model_definitions_pricing_status", "model_definitions", ["pricing_status"]
            )

    if "model_pricing_profiles" not in tables:
        op.create_table(
            "model_pricing_profiles",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("provider", sa.String(80), nullable=False),
            sa.Column("provider_model_id", sa.String(255), nullable=False),
            sa.Column("input_mode", sa.String(40), nullable=False, server_default="default"),
            sa.Column("resolution", sa.String(24), nullable=False, server_default=""),
            sa.Column("currency", sa.String(8), nullable=False),
            sa.Column("billing_unit", sa.String(32), nullable=False),
            sa.Column("unit_price", sa.Numeric(18, 8), nullable=False),
            sa.Column("estimate_unit", sa.String(32), nullable=False),
            sa.Column("estimate_unit_price", sa.Numeric(18, 8), nullable=False),
            sa.Column("usd_per_currency", sa.Numeric(18, 8), nullable=False),
            sa.Column("fx_source", sa.String(500), nullable=False, server_default=""),
            sa.Column("fx_checked_at", sa.DateTime(timezone=True)),
            sa.Column("estimate_formula", sa.String(500), nullable=False, server_default=""),
            sa.Column("settlement_formula", sa.String(500), nullable=False, server_default=""),
            sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
            sa.Column("effective_until", sa.DateTime(timezone=True)),
            sa.Column("source_url", sa.String(1000), nullable=False),
            sa.Column("source_checked_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("notes", sa.String(1000), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "provider",
                "provider_model_id",
                "input_mode",
                "resolution",
                "effective_from",
                name="uq_model_pricing_profile_scope",
            ),
            sa.CheckConstraint("unit_price >= 0", name="ck_model_pricing_unit_price_nonnegative"),
            sa.CheckConstraint("estimate_unit_price >= 0", name="ck_model_pricing_estimate_nonnegative"),
            sa.CheckConstraint("usd_per_currency > 0", name="ck_model_pricing_fx_positive"),
            sa.CheckConstraint(
                "effective_until IS NULL OR effective_until > effective_from",
                name="ck_model_pricing_effective_window",
            ),
        )
        op.create_index(
            "ix_model_pricing_profiles_lookup",
            "model_pricing_profiles",
            ["provider", "provider_model_id", "input_mode"],
        )

    # Re-inspect: the pricing table may have just been created above. Revisions
    # replay onto partial schemas — a test upgrades a character-state-only
    # database through this chain — so the seed asks for the tables it touches
    # rather than assuming the DDL above ran.
    _seed_ark_seedance(connection, set(sa.inspect(connection).get_table_names()))


def _seed_ark_seedance(connection, tables: set[str]) -> None:  # type: ignore[no-untyped-def]
    if "model_pricing_profiles" not in tables:
        return
    now = datetime.now(UTC)
    rows = []
    for resolution, cny_per_million_tokens, cny_per_second, note in ARK_NO_VIDEO_INPUT:
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "provider": "seedance",
                "provider_model_id": ARK_MODEL_ID,
                "input_mode": "no_video_input",
                "resolution": resolution,
                "currency": "CNY",
                "billing_unit": "token",
                "unit_price": float(cny_per_million_tokens),
                "estimate_unit": "second",
                "estimate_unit_price": float(cny_per_second),
                "usd_per_currency": float(USD_PER_CNY),
                "fx_source": FX_SOURCE,
                "fx_checked_at": CHECKED_AT,
                "estimate_formula": ESTIMATE_FORMULA,
                "settlement_formula": SETTLEMENT_FORMULA,
                "effective_from": CHECKED_AT,
                "effective_until": None,
                "source_url": ARK_PRICING_URL,
                "source_checked_at": CHECKED_AT,
                "notes": note,
                "created_at": now,
                "updated_at": now,
            }
        )
    # The 1080p promotion is a separate, dated row. Folding 72% into the base
    # price would make a discount that ends on 2026-09-17 permanent.
    rows.append(
        {
            "id": str(uuid.uuid4()),
            "provider": "seedance",
            "provider_model_id": ARK_MODEL_ID,
            "input_mode": "no_video_input",
            "resolution": "1080p",
            "currency": "CNY",
            "billing_unit": "token",
            "unit_price": 77.00 * 0.72,
            "estimate_unit": "second",
            "estimate_unit_price": 3.742 * 0.72,
            "usd_per_currency": float(USD_PER_CNY),
            "fx_source": FX_SOURCE,
            "fx_checked_at": CHECKED_AT,
            "estimate_formula": ESTIMATE_FORMULA,
            "settlement_formula": SETTLEMENT_FORMULA,
            "effective_from": datetime(2026, 8, 14, 6, 0, tzinfo=UTC),
            "effective_until": datetime(2026, 9, 17, 6, 0, tzinfo=UTC),
            "source_url": ARK_PRICING_URL,
            "source_checked_at": CHECKED_AT,
            "notes": (
                "limited-time 72% of list, 2026-08-14 14:00 to 2026-09-17 14:00 Beijing. "
                "The page prints the list rate and the discount, not the product; "
                "these two numbers are 0.72 x list."
            ),
            "created_at": now,
            "updated_at": now,
        }
    )
    existing = connection.execute(
        sa.text(
            "select count(*) from model_pricing_profiles where provider_model_id = :model"
        ),
        {"model": ARK_MODEL_ID},
    ).scalar()
    if existing:
        return
    connection.execute(sa.text(_INSERT), rows)
    if "model_definitions" not in tables:
        return
    # The one model whose rates were read off the vendor page. Everything else
    # keeps the UNVERIFIED default, which is what it honestly is.
    connection.execute(
        sa.text(
            "update model_definitions set pricing_status = 'VERIFIED' "
            "where logical_name = 'seedance-2.5-official'"
        )
    )


_INSERT = """
insert into model_pricing_profiles (
    id, provider, provider_model_id, input_mode, resolution, currency, billing_unit,
    unit_price, estimate_unit, estimate_unit_price, usd_per_currency, fx_source,
    fx_checked_at, estimate_formula, settlement_formula, effective_from, effective_until,
    source_url, source_checked_at, notes, created_at, updated_at
) values (
    :id, :provider, :provider_model_id, :input_mode, :resolution, :currency, :billing_unit,
    :unit_price, :estimate_unit, :estimate_unit_price, :usd_per_currency, :fx_source,
    :fx_checked_at, :estimate_formula, :settlement_formula, :effective_from, :effective_until,
    :source_url, :source_checked_at, :notes, :created_at, :updated_at
)
"""


def downgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    if "model_pricing_profiles" in tables:
        op.drop_index("ix_model_pricing_profiles_lookup", table_name="model_pricing_profiles")
        op.drop_table("model_pricing_profiles")
    if "model_definitions" in tables:
        columns = {item["name"] for item in sa.inspect(connection).get_columns("model_definitions")}
        if "pricing_status" in columns:
            op.drop_index("ix_model_definitions_pricing_status", table_name="model_definitions")
            op.drop_column("model_definitions", "pricing_status")
