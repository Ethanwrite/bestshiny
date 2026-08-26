"""Price the six OpenRouter video models from OpenRouter's own SKU table.

Every rate here was read from `GET https://openrouter.ai/api/v1/videos/models` on
2026-08-26 — the field the model pages render — and checked against the raw JSON
rather than a summary of it. Verbatim examples:

    x-ai/grok-imagine-video   cents_per_video_output_second_480p  "5"
    kwaivgi/kling-v3.0-std    duration_seconds_with_audio         "0.126"
    google/veo-3.1            duration_seconds_without_audio      "0.20"

All six bill per generated second. None had a source recorded, and the
placeholders were wrong by up to 34%: `kling-v3.0-pro` carried 0.14 against a
real 0.168, `kling-v3.0-std` 0.10 against 0.126.

**These are the audio-on rates**, because that is what this platform is billed.
OpenRouter defaults `generate_audio` to true, and the adapter now states it
explicitly (`OPENROUTER_VIDEO_GENERATE_AUDIO`, default true) rather than
inheriting it. Setting it false roughly halves `google/veo-3.1`, and repricing
these rows is part of making that change, not a consequence of it.

The 1080p rows are the reason the platform-wide resolution multiplier is gone
rather than retuned. Three Veo models, one vendor, one reseller, one day:

    google/veo-3.1        1080p / 720p = 1.0
    google/veo-3.1-fast   1080p / 720p = 1.2
    google/veo-3.1-lite   1080p / 720p = 1.6

The multiplier claimed 1.30 for all of them, and for every other provider too.

Two capability corrections travel with the prices, from the same SKU table:
`kling-v3.0-pro` and `-std` are listed at **720p only** with a **3s** minimum,
where the registry allowed 1080p and 1s — it would have priced and submitted a
resolution OpenRouter does not serve. The matching fix for fresh deployments is
in `config/model-registry/defaults.json`; this migration only reaches databases
that already hold the rows.

Recorded but not fixed here: `veo-3.1`, `-fast` and `-lite` accept durations of
**4, 6 or 8 seconds only**, a discrete set the capability profile cannot express
with min and max, so 5 and 7 stay quotable and would be refused by the provider.
4K is deliberately unpriced — supported upstream on `veo-3.1` and `-fast`, not
enabled here, and an unpriced resolution fails closed.

Revision ID: 0047_openrouter_video
Revises: 0046_gpt_image_2_exact
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0047_openrouter_video"
down_revision: str | None = "0046_gpt_image_2_exact"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROVIDER = "openrouter"
CHECKED_AT = datetime(2026, 8, 26, tzinfo=UTC)
SKU_SOURCE = "https://openrouter.ai/api/v1/videos/models"

AUDIO_NOTE = (
    "audio-on rate: OpenRouter defaults generate_audio=true and this platform now "
    "states it explicitly, so this is the rate actually billed."
)

# model_id -> ({resolution: usd_per_second}, extra settlement terms, note)
MODELS: dict[str, tuple[dict[str, float], str, str]] = {
    "google/veo-3.1": (
        {"720p": 0.40, "1080p": 0.40},
        "",
        AUDIO_NOTE + " 4K is 0.60 USD/s upstream and is not enabled here.",
    ),
    "google/veo-3.1-fast": (
        {"720p": 0.10, "1080p": 0.12},
        "",
        AUDIO_NOTE + " 4K is 0.30 USD/s upstream and is not enabled here.",
    ),
    "google/veo-3.1-lite": (
        {"720p": 0.05, "1080p": 0.08},
        "",
        AUDIO_NOTE + " No 4K upstream.",
    ),
    "kwaivgi/kling-v3.0-pro": (
        {"720p": 0.168},
        "",
        AUDIO_NOTE + " 720p is the only resolution OpenRouter lists for this model.",
    ),
    "kwaivgi/kling-v3.0-std": (
        {"720p": 0.126},
        "",
        AUDIO_NOTE + " 720p is the only resolution OpenRouter lists for this model.",
    ),
    "x-ai/grok-imagine-video": (
        {"480p": 0.05, "720p": 0.07},
        " + 0.002 * input_image_count",
        "No audio SKU on this model. Input images bill 0.002 USD each on top of the seconds.",
    ),
}

# An image reference does not move the per-second rate on any of these, so both
# modes carry the same number rather than one of them falling through to nothing.
INPUT_MODES = ("no_video_input", "video_input")

# logical_name -> (supported_resolutions, min_duration, max_duration)
CAPABILITY_FIXES = {
    "kling-3-pro-openrouter": (["720p"], 3.0, 15.0),
    "kling-3-standard-openrouter": (["720p"], 3.0, 15.0),
}

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

    now = datetime.now(UTC)
    rows = []
    for model_id, (by_resolution, extra_settlement, note) in MODELS.items():
        already = connection.execute(
            sa.text(
                "select count(*) from model_pricing_profiles where provider_model_id = :model"
            ),
            {"model": model_id},
        ).scalar()
        if already:
            continue
        for resolution, usd_per_second in by_resolution.items():
            for input_mode in INPUT_MODES:
                rows.append(
                    {
                        "id": str(uuid.uuid4()),
                        "provider": PROVIDER,
                        "provider_model_id": model_id,
                        "input_mode": input_mode,
                        "resolution": resolution,
                        "currency": "USD",
                        "billing_unit": "second",
                        "unit_price": usd_per_second,
                        "estimate_unit": "second",
                        "estimate_unit_price": usd_per_second,
                        "usd_per_currency": 1.0,
                        "fx_source": "none — provider bills in USD",
                        "fx_checked_at": CHECKED_AT,
                        "estimate_formula": "estimate_unit_price * duration_seconds",
                        "settlement_formula": (
                            "unit_price * generated_seconds" + extra_settlement
                        ),
                        "effective_from": CHECKED_AT,
                        "effective_until": None,
                        "source_url": SKU_SOURCE,
                        "source_checked_at": CHECKED_AT,
                        "notes": note,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
    if rows:
        connection.execute(sa.text(_INSERT), rows)

    if "model_definitions" not in tables or "model_capability_profiles" not in tables:
        return
    for logical_name, (resolutions, min_duration, max_duration) in CAPABILITY_FIXES.items():
        definition_id = connection.execute(
            sa.text("select id from model_definitions where logical_name = :name"),
            {"name": logical_name},
        ).scalar()
        if definition_id is None:
            continue
        connection.execute(
            sa.text(
                "update model_capability_profiles set supported_resolutions = :resolutions, "
                "min_duration = :min_duration, max_duration = :max_duration "
                "where model_definition_id = :row"
            ).bindparams(sa.bindparam("resolutions", type_=sa.JSON())),
            {
                "resolutions": resolutions,
                "min_duration": min_duration,
                "max_duration": max_duration,
                "row": definition_id,
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    tables = set(sa.inspect(connection).get_table_names())
    if "model_pricing_profiles" not in tables:
        return
    connection.execute(
        sa.text(
            "delete from model_pricing_profiles where provider_model_id in :models"
        ).bindparams(sa.bindparam("models", expanding=True)),
        {"models": list(MODELS)},
    )
