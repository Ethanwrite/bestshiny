"""Retire the Wan pricing rows filed under a logical name.

Migration `0048` priced Wan 2.7 under `WAN_MODEL_KEY = "wan-2.7"`, which is the
logical name rather than a deployment id. The registry holds the deployments —
`wan2.7-t2v-2026-06-12` and `wan2.7-i2v-2026-04-25` — and both were later priced
correctly, at the same published rates, without the original rows being removed.

Nothing reads them. The cost engine resolves a profile on
`provider == provider and provider_model_id == model`, with no fallback to a
logical name, and no model is registered under `wan-2.7`: a quote for
`wan / wan-2.7` on production answers `HTTP 400 — selected video model is not
registered for this provider`. So these two rows are unreachable, and their only
effect is to make the ledger read as though a third Wan model were priced.

They are deleted by their own identity — provider, model id, resolution and the
0.60/1.00 CNY rates `0048` wrote — so a row that someone has since corrected is
left alone rather than assumed stale. The downgrade restores exactly what `0048`
inserted.

Revision ID: 0061_retire_wan_logical_name_pricing
Revises: 0060_flow_remote_owner_index
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0061_retire_wan_logical_name_pricing"
down_revision = "0060_flow_remote_owner_index"
branch_labels = None
depends_on = None

PROVIDER = "wan"
LOGICAL_NAME = "wan-2.7"
#: What 0048 wrote: the Beijing list, CNY per output second.
CNY_PER_SECOND = {"720p": 0.6, "1080p": 1.0}
PRICING_URL = "https://help.aliyun.com/zh/model-studio/model-pricing"
FX_SOURCE = "PBOC/CFETS central parity 2026-08-26 (100 USD = 678.29 CNY)"
USD_PER_CNY = 0.14743


def upgrade() -> None:
    for resolution, rate in CNY_PER_SECOND.items():
        op.execute(
            sa.text(
                """
                delete from model_pricing_profiles
                 where provider = :provider
                   and provider_model_id = :model
                   and resolution = :resolution
                   and unit_price = :rate
                """
            ).bindparams(provider=PROVIDER, model=LOGICAL_NAME, resolution=resolution, rate=rate)
        )


def downgrade() -> None:
    now = datetime.now(UTC)
    checked = datetime(2026, 8, 26, tzinfo=UTC)
    for resolution, rate in CNY_PER_SECOND.items():
        op.execute(
            sa.text(
                """
                insert into model_pricing_profiles (
                    id, provider, provider_model_id, input_mode, resolution, currency,
                    billing_unit, unit_price, estimate_unit, estimate_unit_price,
                    usd_per_currency, fx_source, fx_checked_at, estimate_formula,
                    settlement_formula, effective_from, effective_until, source_url,
                    source_checked_at, notes, created_at, updated_at
                ) values (
                    :id, :provider, :model, 'no_video_input', :resolution, 'CNY',
                    'second', :rate, 'second', :rate,
                    :usd_per_cny, :fx_source, :checked,
                    'unit_price * duration_seconds * usd_per_currency',
                    'unit_price * duration_seconds * usd_per_currency',
                    :checked, null, :source_url, :checked, '', :now, :now
                )
                """
            ).bindparams(
                id=str(uuid.uuid4()),
                provider=PROVIDER,
                model=LOGICAL_NAME,
                resolution=resolution,
                rate=rate,
                usd_per_cny=USD_PER_CNY,
                fx_source=FX_SOURCE,
                checked=checked,
                source_url=PRICING_URL,
                now=now,
            )
        )
