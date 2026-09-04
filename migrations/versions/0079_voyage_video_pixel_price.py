"""Price the video-pixel side of voyage-multimodal-3.5.

0071 recorded two of the three official list prices for
``voyage / voyage-multimodal-3.5``: text input at USD 0.12 per 1M tokens and
image input at USD 0.60 per 1B pixels. It recorded no video row, and the
settlement path looks one up by ``input_mode = "video_input"``. With no such
row, ``settle_from_usage`` refuses to settle any call whose usage block
reports non-zero ``video_pixels`` - correctly, because guessing a price is
worse than admitting the cost is UNCERTAIN - and the authorization then closes
at its quote ceiling rather than at what was actually spent.

The vendor prices video by the frame, at the image rate:

    "For pricing purposes, each video frame is considered an image."
    -- https://docs.voyageai.com/docs/pricing

So the video row is the image row's rate, recorded as its own row rather than
by teaching the settlement code that a missing video price may fall back to
the image one. A fallback would silently price *any* model's video input off
its image price; a row states this vendor's rule for this model and leaves
every other model's missing price exactly as UNCERTAIN as it is now.

This changes no behaviour for the frame path the platform actually uses:
``BoundedVideoFrameSampler`` extracts stills and sends them as image inputs,
so the provider reports those pixels as image pixels and they already settle.
It closes the case where a usage block reports video pixels directly.

Revision ID: 0079_voyage_video_pixel_price
Revises: 0078_creative_session_create_idempotency
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0079_voyage_video_pixel_price"
down_revision: str | None = "0078_creative_session_create_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROVIDER = "voyage"
MODEL = "voyage-multimodal-3.5"
INPUT_MODE = "video_input"
BILLING_UNIT = "1B_pixels"
UNIT_PRICE = "0.60"
FORMULA = "unit_price * input_pixels / 1e9 * usd_per_currency"
SOURCE_URL = "https://docs.voyageai.com/docs/pricing"
#: The date this price was read off the vendor's own page, not the date this
#: migration runs. `effective_from` is a fact about the price list.
CHECKED_AT = datetime(2026, 9, 4, tzinfo=UTC)


def _pricing() -> sa.Table | None:
    connection = op.get_bind()
    if "model_pricing_profiles" not in set(sa.inspect(connection).get_table_names()):
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return None
    return sa.Table("model_pricing_profiles", sa.MetaData(), autoload_with=connection)


def upgrade() -> None:
    pricing = _pricing()
    if pricing is None:
        return
    connection = op.get_bind()
    existing = connection.execute(
        sa.select(pricing.c.id).where(
            pricing.c.provider == PROVIDER,
            pricing.c.provider_model_id == MODEL,
            pricing.c.input_mode == INPUT_MODE,
            pricing.c.resolution == "",
        )
    ).scalar_one_or_none()
    if existing:
        return
    now = datetime.now(UTC)
    connection.execute(
        pricing.insert().values(
            id=str(uuid.uuid4()),
            provider=PROVIDER,
            provider_model_id=MODEL,
            input_mode=INPUT_MODE,
            resolution="",
            currency="USD",
            billing_unit=BILLING_UNIT,
            unit_price=UNIT_PRICE,
            estimate_unit=BILLING_UNIT,
            estimate_unit_price=UNIT_PRICE,
            usd_per_currency="1.0",
            fx_source="",
            fx_checked_at=None,
            estimate_formula=FORMULA.replace("unit_price", "estimate_unit_price"),
            settlement_formula=FORMULA,
            effective_from=CHECKED_AT,
            effective_until=None,
            source_url=SOURCE_URL,
            source_checked_at=CHECKED_AT,
            notes=(
                "Official voyage-multimodal-3.5 list price. Video input is billed per "
                "billion pixels at the image rate: the vendor counts each video frame "
                "as an image. The platform uses this model for advisory retrieval only, "
                "and sends videos as extracted stills rather than as video."
            ),
            created_at=now,
            updated_at=now,
        )
    )


def downgrade() -> None:
    pricing = _pricing()
    if pricing is None:
        return
    op.get_bind().execute(
        pricing.delete().where(
            pricing.c.provider == PROVIDER,
            pricing.c.provider_model_id == MODEL,
            pricing.c.input_mode == INPUT_MODE,
            pricing.c.resolution == "",
        )
    )
