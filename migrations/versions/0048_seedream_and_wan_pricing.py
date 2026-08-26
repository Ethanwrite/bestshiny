"""Correct Seedream's model ID, and price it and Wan 2.7 from published rates.

Two more models audited against their vendors' own pages, read 2026-08-26.

**Seedream 5.0 — the same defect as Seedance 2.5.** The registry held
`seedream-5-0`, which is not a model ID Volcengine Ark publishes: it is the
BytePlus-style stem, with no `doubao-` prefix and no dated snapshot. Ark sells
this as two SKUs, `doubao-seedream-5-0-260128` (lite) and
`doubao-seedream-5-0-pro-260628` (pro). The lite SKU is adopted here because its
published rate, 0.22 CNY per output image, is what the unsourced placeholder
(0.03 USD) was approximating — choosing pro would be a product change wearing an
audit's clothes. Input reference images are free on lite. Images that fail
moderation are not billed. The endpoint, `POST /images/generations`, already
matches what the adapter sends.

**Wan 2.7 — the IDs were right, the price was not.** All three dated snapshots
this adapter posts are current and undeprecated on Model Studio:
`wan2.7-t2v-2026-06-12`, `wan2.7-i2v-2026-04-25`, `wan2.7-r2v-2026-06-12`.
`wan-2.7` stays the registry's `provider_model_id` because it is a family key the
adapter resolves per mode, not a logical name leaking onto the wire — a test
already pins that mapping. The price carried 0.07 USD/s with no source against a
published Beijing rate of 0.6 CNY/s at 720P, about 21% under.

Pricing is per output second and varies by **region**, not by t2v/i2v/r2v. This
deployment posts to `https://dashscope.aliyuncs.com/api/v1`, the mainland
endpoint, so the Beijing column applies: 0.6 CNY/s at 720P and 1.0 CNY/s at
1080P. Singapore's catalogue rates differ (0.733924 / 1.100886) and are not
seeded, because seeding a region we do not call would be a number waiting to be
believed.

Deliberately not seeded, and therefore fail-closed:

* **Tokyo.** The per-model cards quote figures that are the USD conversion of the
  Beijing CNY price presented as CNY; the pricing catalogue lists no Japan row at
  all. That is a contradiction, not a rate.
* **r2v.** It bills input video as well as output —
  `min(input_seconds, 5) + output_seconds` — which the per-second estimate does
  not model. Recorded in the settlement formula; the mode stays unpriced.

Revision ID: 0048_seedream_wan
Revises: 0047_openrouter_video
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0048_seedream_wan"
down_revision: str | None = "0047_openrouter_video"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKED_AT = datetime(2026, 8, 26, tzinfo=UTC)
USD_PER_CNY = 0.14743
FX_SOURCE = "PBOC/CFETS central parity 2026-08-26 (100 USD = 678.29 CNY)"

SEEDREAM_PLACEHOLDER = "seedream-5-0"
SEEDREAM_MODEL_ID = "doubao-seedream-5-0-260128"
SEEDREAM_LOGICAL = "seedream-5.0-ark"
ARK_PRICING_URL = "https://www.volcengine.com/docs/82379/1544106"
ARK_MODEL_LIST_URL = "https://www.volcengine.com/docs/82379/1330310"
SEEDREAM_CNY_PER_IMAGE = 0.22

WAN_MODEL_KEY = "wan-2.7"
WAN_PRICING_URL = "https://help.aliyun.com/zh/model-studio/model-pricing"
# Beijing list, CNY per output second.
WAN_CNY_PER_SECOND = {"720p": 0.6, "1080p": 1.0}

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


def _row(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "input_mode": "no_video_input",
        "resolution": "",
        "currency": "CNY",
        "usd_per_currency": USD_PER_CNY,
        "fx_source": FX_SOURCE,
        "fx_checked_at": CHECKED_AT,
        "effective_from": CHECKED_AT,
        "effective_until": None,
        "source_checked_at": CHECKED_AT,
        "notes": "",
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return base


def upgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    if "model_pricing_profiles" not in tables:
        return

    rows: list[dict[str, object]] = []
    if not connection.execute(
        sa.text("select count(*) from model_pricing_profiles where provider_model_id = :m"),
        {"m": SEEDREAM_MODEL_ID},
    ).scalar():
        rows.append(
            _row(
                provider="seedance",
                provider_model_id=SEEDREAM_MODEL_ID,
                input_mode="default",
                billing_unit="image",
                unit_price=SEEDREAM_CNY_PER_IMAGE,
                estimate_unit="image",
                estimate_unit_price=SEEDREAM_CNY_PER_IMAGE,
                estimate_formula="estimate_unit_price * image_count * usd_per_currency",
                settlement_formula=(
                    "unit_price * successfully_generated_images * usd_per_currency"
                ),
                source_url=ARK_PRICING_URL,
                notes=(
                    "Seedream 5.0 lite, flat 0.22 CNY per output image at every named size "
                    "tier. Input reference images are free on lite. Moderation failures are "
                    f"not billed. Model list: {ARK_MODEL_LIST_URL}"
                ),
            )
        )
    if not connection.execute(
        sa.text("select count(*) from model_pricing_profiles where provider_model_id = :m"),
        {"m": WAN_MODEL_KEY},
    ).scalar():
        for resolution, cny_per_second in WAN_CNY_PER_SECOND.items():
            rows.append(
                _row(
                    provider="wan",
                    provider_model_id=WAN_MODEL_KEY,
                    resolution=resolution,
                    billing_unit="second",
                    unit_price=cny_per_second,
                    estimate_unit="second",
                    estimate_unit_price=cny_per_second,
                    estimate_formula="estimate_unit_price * duration_seconds * usd_per_currency",
                    settlement_formula=(
                        "unit_price * output_seconds * usd_per_currency "
                        "(r2v additionally bills min(input_seconds, 5) and is not priced here)"
                    ),
                    source_url=WAN_PRICING_URL,
                    notes=(
                        "Beijing list rate; this deployment posts to "
                        "dashscope.aliyuncs.com/api/v1. Singapore lists 0.733924 / 1.100886 "
                        "CNY/s and is not seeded. Price does not vary by t2v/i2v/r2v."
                    ),
                )
            )
    if rows:
        connection.execute(sa.text(_INSERT), rows)

    if "model_definitions" not in tables:
        return
    # Only replace the placeholder: an operator who has pointed this at a
    # specific Ark endpoint ID has made a deployment decision.
    connection.execute(
        sa.text(
            "update model_definitions set provider_model_id = :new "
            "where logical_name = :name and provider_model_id = :old"
        ),
        {"new": SEEDREAM_MODEL_ID, "name": SEEDREAM_LOGICAL, "old": SEEDREAM_PLACEHOLDER},
    )


def downgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    if "model_pricing_profiles" in tables:
        connection.execute(
            sa.text(
                "delete from model_pricing_profiles where provider_model_id in :models"
            ).bindparams(sa.bindparam("models", expanding=True)),
            {"models": [SEEDREAM_MODEL_ID, WAN_MODEL_KEY]},
        )
    if "model_definitions" in tables:
        connection.execute(
            sa.text(
                "update model_definitions set provider_model_id = :old "
                "where logical_name = :name and provider_model_id = :new"
            ),
            {"old": SEEDREAM_PLACEHOLDER, "name": SEEDREAM_LOGICAL, "new": SEEDREAM_MODEL_ID},
        )
