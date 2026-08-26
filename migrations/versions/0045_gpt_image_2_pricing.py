"""Price openai/gpt-image-2 from OpenRouter's own published token rate.

The registry carried `estimated_per_image = 0.1248` USD with no source. The real
rate, read from OpenRouter's endpoint descriptor for this model
(`GET /api/v1/images/models/openai/gpt-image-2/endpoints`, which is the
authoritative billing source rather than the model page's generic "$8 / $8 per
1M" display), is:

    output_image  0.00003  USD/token
    input_text    0.000005 USD/token
    input_image   0.000008 USD/token

Tokens per generated image at 1024x1024 come from OpenAI's official
gpt-image-2 calculator: low 196, medium 1756, high 7024. Two independent sources
agree on the product: OpenAI's own rounded per-image estimate table prints
$0.006 / $0.053 / $0.211, which is exactly those counts at $30/1M.

    low     196 tok = 0.00588 USD
    medium 1756 tok = 0.05268 USD
    high   7024 tok = 0.21072 USD

**Why the reservation is taken at the `high` figure.** Nothing in this platform
sends an explicit `quality`: `PassengerGenerationCommand` has no field for it and
the adapter only forwards one if a caller supplies it, so every request bills at
the provider's `auto` default. `auto` may resolve to any of the three, a 36x
spread. A reservation must never be smaller than the bill, so it is taken at the
ceiling. That is deliberately conservative and it is not free: an `auto` request
that resolves to `low` reserves ~35x what it costs, and this platform settles the
reservation rather than the actual usage, so the workspace is charged the
ceiling. The fix is to send `quality` explicitly and price the three modes
separately; until that decision is made, over-reserving is the safe direction and
this note is the record of the trade.

The old 0.1248 sat between `medium` and `high` and matched neither — close enough
to look considered, wrong in the direction that under-reserves a `high` image.

Sources, recorded on the row:
  https://openrouter.ai/api/v1/images/models/openai/gpt-image-2/endpoints
  https://developers.openai.com/api/docs/guides/image-generation#calculating-costs

Revision ID: 0045_gpt_image_2_pricing
Revises: 0044_model_pricing
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0045_gpt_image_2_pricing"
down_revision: str | None = "0044_model_pricing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROVIDER = "openrouter"
MODEL_ID = "openai/gpt-image-2"
LOGICAL_NAME = "gpt-image-2-openrouter"
CHECKED_AT = datetime(2026, 8, 26, tzinfo=UTC)

# USD per output_image token, from the OpenRouter endpoint descriptor.
USD_PER_OUTPUT_TOKEN = 0.00003
# Ceiling: `high` at 1024x1024. See the module docstring for why the ceiling.
USD_PER_IMAGE_CEILING = 7024 * USD_PER_OUTPUT_TOKEN

NOTES = (
    "auto-quality ceiling. 1024x1024 output_image tokens: low 196 = 0.00588 USD, "
    "medium 1756 = 0.05268 USD, high 7024 = 0.21072 USD. No explicit quality is sent, "
    "so auto may bill any of the three; the reservation takes the ceiling so it can "
    "never be smaller than the bill. Prompt text bills separately at 0.000005 USD/token."
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


def upgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    if "model_pricing_profiles" not in tables:
        return
    existing = connection.execute(
        sa.text("select count(*) from model_pricing_profiles where provider_model_id = :model"),
        {"model": MODEL_ID},
    ).scalar()
    if existing:
        return
    now = datetime.now(UTC)
    connection.execute(
        sa.text(_INSERT),
        [
            {
                "id": str(uuid.uuid4()),
                "provider": PROVIDER,
                "provider_model_id": MODEL_ID,
                # Images have no video-input axis and this model prices per
                # image rather than per resolution tier, so one default row
                # covers every request the platform can currently build.
                "input_mode": "default",
                "resolution": "",
                "currency": "USD",
                "billing_unit": "token",
                "unit_price": USD_PER_OUTPUT_TOKEN,
                "estimate_unit": "image",
                "estimate_unit_price": USD_PER_IMAGE_CEILING,
                "usd_per_currency": 1.0,
                "fx_source": "none — provider bills in USD",
                "fx_checked_at": CHECKED_AT,
                "estimate_formula": "estimate_unit_price * image_count",
                "settlement_formula": (
                    "unit_price * usage.output_image_tokens "
                    "+ 0.000005 * usage.input_text_tokens "
                    "+ 0.000008 * usage.input_image_tokens"
                ),
                "effective_from": CHECKED_AT,
                "effective_until": None,
                "source_url": (
                    "https://openrouter.ai/api/v1/images/models/openai/gpt-image-2/endpoints"
                ),
                "source_checked_at": CHECKED_AT,
                "notes": NOTES,
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    # Belt and braces: `reconcile_pricing_status()` derives this column from the
    # profiles at boot. Some histories reach this revision with the registry
    # tables deliberately absent, and there is no claim to record there.
    if "model_definitions" in tables:
        connection.execute(
            sa.text(
                "update model_definitions set pricing_status = 'VERIFIED' "
                "where logical_name = :name"
            ),
            {"name": LOGICAL_NAME},
        )


def downgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    if "model_pricing_profiles" not in tables:
        return
    connection.execute(
        sa.text("delete from model_pricing_profiles where provider_model_id = :model"),
        {"model": MODEL_ID},
    )
    if "model_definitions" not in tables:
        return
    connection.execute(
        sa.text(
            "update model_definitions set pricing_status = 'UNVERIFIED' where logical_name = :name"
        ),
        {"name": LOGICAL_NAME},
    )
