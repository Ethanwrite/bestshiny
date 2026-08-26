"""Price gpt-image-2 for the request we now actually send.

0045 reserved at the `high` ceiling, 0.21072 USD, because nothing in this
platform stated a `quality` and the provider's `auto` default could resolve
anywhere across a 36x range. A ceiling is the safe answer to "we do not know
what we asked for", but it is still an answer to the wrong question: an `auto`
request that resolved to `low` reserved about 35x what it cost, and this platform
settles the reservation rather than actual usage.

The adapter now names the parameter. `OPENROUTER_IMAGE_QUALITY` defaults to `low`
and is sent on every image request, so the token count is no longer a range:
196 output tokens at OpenRouter's 0.00003 USD/token is **0.00588 USD**, exact.

This is a repricing of our own request, not a change in OpenRouter's rate, so the
row is corrected in place rather than superseded with a dated one. The vendor's
per-token price is unchanged and still carries its original source and date.

Changing `OPENROUTER_IMAGE_QUALITY` without repricing this row would put the
quote and the wire back out of step, which is the whole defect. A test ties the
configured quality to the seeded price so that cannot happen quietly.

Revision ID: 0046_gpt_image_2_exact
Revises: 0045_gpt_image_2_pricing
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_gpt_image_2_exact"
down_revision: str | None = "0045_gpt_image_2_pricing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MODEL_ID = "openai/gpt-image-2"
USD_PER_OUTPUT_TOKEN = 0.00003
TOKENS_PER_IMAGE = {"low": 196, "medium": 1756, "high": 7024}

LOW_USD_PER_IMAGE = TOKENS_PER_IMAGE["low"] * USD_PER_OUTPUT_TOKEN
HIGH_USD_PER_IMAGE = TOKENS_PER_IMAGE["high"] * USD_PER_OUTPUT_TOKEN

NOTES_EXACT = (
    "exact for quality=low, which the adapter now sends explicitly "
    "(OPENROUTER_IMAGE_QUALITY). 1024x1024 output_image tokens: low 196, "
    "medium 1756, high 7024, at 0.00003 USD/token. Reprice this row if the "
    "configured quality changes. Prompt text bills separately at 0.000005 USD/token."
)

NOTES_CEILING = (
    "auto-quality ceiling. 1024x1024 output_image tokens: low 196 = 0.00588 USD, "
    "medium 1756 = 0.05268 USD, high 7024 = 0.21072 USD. No explicit quality is sent, "
    "so auto may bill any of the three; the reservation takes the ceiling so it can "
    "never be smaller than the bill. Prompt text bills separately at 0.000005 USD/token."
)


def _reprice(price: float, notes: str) -> None:
    connection = op.get_bind()
    if "model_pricing_profiles" not in set(sa.inspect(connection).get_table_names()):
        return
    connection.execute(
        sa.text(
            "update model_pricing_profiles set estimate_unit_price = :price, notes = :notes "
            "where provider_model_id = :model"
        ),
        {"price": price, "notes": notes, "model": MODEL_ID},
    )


def upgrade() -> None:
    _reprice(LOW_USD_PER_IMAGE, NOTES_EXACT)


def downgrade() -> None:
    _reprice(HIGH_USD_PER_IMAGE, NOTES_CEILING)
