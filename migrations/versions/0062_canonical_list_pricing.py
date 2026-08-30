"""Canonical pricing is the vendor's long-term list price, in the vendor's own currency.

The operator supplied a full audited price sheet and three rules that this
revision applies to the table:

1. **A promotion is never canonical.** Ark's 1080p Seedance row at 55.44 CNY was
   modelled correctly — end-dated rather than folded into the base — but it was
   still the row quoting today, and a quote taken from a discount under-charges
   the moment the discount lapses. It is deleted rather than re-dated. Nothing
   replaces it: 77.00 was already seeded beside it and simply becomes the answer.
2. **The original list price wins over whatever is charged today.** OpenRouter
   resells `openai/gpt-5.6-sol` at 2.00/10.00 and OpenAI's own current page shows
   4.00/20.00; both are promotional against an original launch list of
   5.00/30.00. The list figures are what a canonical price means here, and they
   are the only ones that cannot under-quote.
3. **A CNY price stays a CNY price.** `usd_per_currency` is an FX *snapshot*
   carrying its own source and date, and USD is derived from it at quote and
   settlement time. Converting the 20 CNY rows in place would destroy the one
   distinction that matters when the rate moves: a price the vendor publishes in
   dollars against a price we translated into dollars on a particular day.

Alongside those corrections it fills the scopes that had no published rate when
they were first seeded, all from the same sheet:

- **Ark video-input.** 0044 left `video_input` unseeded because Ark published it
  as a range, and `test_video_input_rates_are_deliberately_absent_rather_than_estimated`
  pinned that absence. The sheet supplies the list rates — 42.00 CNY per 1M
  tokens at 480p and 720p, 46.00 at 1080p — so the range is now a price and the
  scope is seeded. A continuation on Seedance no longer fails closed.
- **Wan 2.7 video-input, including the r2v snapshot.** 0051 left r2v unpriced on
  the reasoning that it also bills input video. That is still true and is
  recorded in the note, but it is an argument about what the estimate covers, not
  grounds for having no price at all: unpriced meant a Wan continuation was
  refused outright. Beijing list is 0.60/1.00 CNY per second and does not vary by
  t2v/i2v/r2v.
- **Veo 3.1 Fast at 4K**, 0.30 USD/s with audio — the one OpenRouter SKU 0047
  did not carry.
- **Cache-tier and modality-input SKUs** for Claude, Sol, Gemini Embedding,
  GPT Image 2 and Qwen. None of these are selectable by a quote: the engine
  resolves on `(input_mode, resolution)` scopes it builds itself, and no scope it
  builds is named here. They are recorded so the audit reads the whole published
  SKU table rather than the corner of it this platform currently sends.

Three canonical models still have **no** published price and are deliberately
left unseeded: `google_flow/flow-veo-3.1` and `google_flow/NARWHAL`, which Google
sells as subscription credits with no per-call rate, and `wan/wan3.0-video`,
which is invitation-only and unreachable from this account. They stay UNVERIFIED
and therefore refused a paid route, which is the honest state rather than a bug.

Revision ID: 0062_canonical_list_pricing
Revises: 0061_retire_wan_logical_name_pricing
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0062_canonical_list_pricing"
down_revision: str | None = "0061_retire_wan_logical_name_pricing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The day the operator's price sheet was compiled from the vendor pages.
CHECKED_AT = datetime(2026, 8, 29, tzinfo=UTC)

# Unchanged from 0044/0048/0051 on purpose. The CNY figures on the sheet are the
# same published Beijing rates those revisions read, so pairing them with a
# different day's parity would make the USD conversion answer a question nobody
# asked. This is a snapshot for deriving USD, never a rewrite of the CNY price.
USD_PER_CNY = 0.14743
CNY_FX_SOURCE = "PBOC/CFETS central parity 2026-08-26 (100 USD = 678.29 CNY)"

TOKENS = "1M_tokens"
SECOND = "second"

ARK_PRICING = "https://www.volcengine.com/docs/82379/1544106"
DASHSCOPE_PRICING = "https://help.aliyun.com/zh/model-studio/model-pricing"
OPENROUTER_VIDEO_SKUS = "https://openrouter.ai/api/v1/videos/models"


def _openrouter(model: str) -> str:
    return f"https://openrouter.ai/{model}"


# ---------------------------------------------------------------------------
# 1. The promotion that must stop quoting.
#
# Deleted by its own identity — provider, model, resolution, the 55.44 rate and
# the 2026-09-17 end date 0044 wrote — so a row someone has since corrected is
# left alone rather than assumed stale. The downgrade restores exactly what 0044
# inserted, including its original effective window.
# ---------------------------------------------------------------------------
ARK_MODEL = "doubao-seedance-2-5-260628"
PROMO_UNIT_PRICE = Decimal("55.44")
PROMO_ESTIMATE_PRICE = Decimal("2.69424")
PROMO_FROM = datetime(2026, 8, 14, tzinfo=UTC)
PROMO_UNTIL = datetime(2026, 9, 17, tzinfo=UTC)
PROMO_NOTE = (
    "1080p promotional rate, 72% of the 77.00 list. Dated so it expires by itself "
    "rather than becoming permanent by accident."
)

# ---------------------------------------------------------------------------
# 2. Rows whose price is corrected in place.
#
# A repricing to the vendor's original list, not a new dated rate: there is no
# window in which 2.00 was the canonical price, only a window in which it was
# the discounted one. Superseding it with a dated row would leave the discount
# quoting until the new row's start date, which is the defect.
# ---------------------------------------------------------------------------
_SOL_LIST = (
    "OpenAI's ORIGINAL published launch list for gpt-5.6-sol. Deliberately NOT the "
    "figure charged today: OpenAI's own page currently shows 4.00 input / 20.00 output "
    "and OpenRouter resells at 2.00 / 10.00, both promotional. Canonical pricing is the "
    "long-term list rate, because a promotion lapses without anyone acting and a quote "
    "taken from one under-charges from that moment. Rate for prompts of 272,000 tokens "
    "or fewer; above that the long-context tier applies. Reasoning tokens bill as output."
)

# (provider, model, input_mode, resolution, new unit price, note)
REPRICED: list[tuple[str, str, str, str, str, str]] = [
    ("openrouter", "openai/gpt-5.6-sol", "input_tokens", "", "5.00", _SOL_LIST),
    ("openrouter", "openai/gpt-5.6-sol", "output_tokens", "", "30.00", _SOL_LIST),
]

# ---------------------------------------------------------------------------
# 3. Scopes that had no published rate when they were first seeded.
# ---------------------------------------------------------------------------
_ARK_VIDEO_INPUT = (
    "Ark list rate for a request that carries video input, read from the operator's "
    "2026-08-29 price sheet. 0044 left this scope unseeded because the published figure "
    "was a range; it is now a rate, so the scope is priced and a continuation no longer "
    "fails closed. Cheaper per token than no-video-input (42.00/46.00 against "
    "70.00/77.00) because Ark settles on completion tokens, which input video does not "
    "add to. The per-second estimate is derived from this token rate at the same "
    "tokens-per-second the no-video-input row implies for that resolution — Ark's source "
    "billing unit is the token, never the second."
)
_WAN_VIDEO_INPUT = (
    "Beijing list, same 0.60/1.00 CNY per second as t2v and i2v: 0048 established that "
    "the Wan 2.7 price does not vary by mode. NOTE what a per-second estimate on this "
    "scope does and does not cover — r2v also bills min(input_seconds, 5) of INPUT video "
    "at the same rate, and the estimator has no input duration to work from, so the "
    "estimate covers output seconds only and is a floor for a reference-guided shot. "
    "0051 left r2v unpriced over exactly this, which made a Wan continuation refuse "
    "outright rather than under-quote; a floor plus the 1.20 service reserve is the "
    "better of the two, and the shortfall is bounded at five seconds."
)
_VEO_FAST_4K = (
    "OpenRouter SKU duration_seconds_with_audio_4k. The with-audio rate is seeded "
    "because OPENROUTER_VIDEO_GENERATE_AUDIO is true and that is what this platform "
    "sends; the silent 4K rate is 0.25. 0047 seeded 720p and 1080p and did not carry "
    "this SKU. The registry declares 720p/1080p for this model, so the row is not "
    "reachable today — it is here so that widening the declaration is a config change "
    "rather than a repricing."
)

# (provider, model, input_mode, resolution, currency, billing_unit, unit_price,
#  estimate_unit, estimate_unit_price, source, note)
Seeded = tuple[str, str, str, str, str, str, str, str, str, str, str]

# Ark bills on completion tokens and quotes a typical per-second figure beside
# it. Tokens per second is a property of the resolution, not of the input mode,
# so the video-input per-second estimate is the no-video-input one scaled by the
# ratio of the two token rates at that resolution. Written as the arithmetic
# rather than as a constant so the derivation is auditable.
_ARK_NO_VIDEO_CNY_PER_SECOND = {"480p": "0.672", "720p": "1.512", "1080p": "3.742"}
_ARK_NO_VIDEO_TOKEN_RATE = {"480p": "70.00", "720p": "70.00", "1080p": "77.00"}
_ARK_VIDEO_TOKEN_RATE = {"480p": "42.00", "720p": "42.00", "1080p": "46.00"}


def _ark_video_input_estimate(resolution: str) -> str:
    per_second = Decimal(_ARK_NO_VIDEO_CNY_PER_SECOND[resolution])
    ratio = Decimal(_ARK_VIDEO_TOKEN_RATE[resolution]) / Decimal(
        _ARK_NO_VIDEO_TOKEN_RATE[resolution]
    )
    return str((per_second * ratio).quantize(Decimal("0.00000001")))


SEEDED: list[Seeded] = [
    # -- Ark Seedance 2.5, video input --------------------------------------
    *[
        (
            "seedance",
            ARK_MODEL,
            "video_input",
            resolution,
            "CNY",
            "token",
            _ARK_VIDEO_TOKEN_RATE[resolution],
            SECOND,
            _ark_video_input_estimate(resolution),
            ARK_PRICING,
            _ARK_VIDEO_INPUT,
        )
        for resolution in ("480p", "720p", "1080p")
    ],
    # -- Wan 2.7 deployments, video input; and the r2v snapshot outright -----
    *[
        (
            "wan",
            model,
            input_mode,
            resolution,
            "CNY",
            SECOND,
            price,
            SECOND,
            price,
            DASHSCOPE_PRICING,
            _WAN_VIDEO_INPUT,
        )
        for model in (
            "wan2.7-t2v-2026-06-12",
            "wan2.7-i2v-2026-04-25",
            "wan2.7-r2v-2026-06-12",
        )
        for input_mode in ("no_video_input", "video_input")
        for resolution, price in (("720p", "0.60"), ("1080p", "1.00"))
        # t2v and i2v already carry no_video_input from 0051; only r2v needs it.
        if input_mode == "video_input" or model.endswith("r2v-2026-06-12")
    ],
    # -- Veo 3.1 Fast at 4K --------------------------------------------------
    *[
        (
            "openrouter",
            "google/veo-3.1-fast",
            input_mode,
            "4k",
            "USD",
            SECOND,
            "0.30",
            SECOND,
            "0.30",
            OPENROUTER_VIDEO_SKUS,
            _VEO_FAST_4K,
        )
        for input_mode in ("no_video_input", "video_input")
    ],
    # -- Cache tiers and modality inputs: recorded, never selectable ---------
    #
    # The engine resolves a video or image quote on (no_video_input | video_input
    # | default, resolution). None of the input modes below is one of those, so
    # none of these rows can be picked by a quote — they complete the published
    # SKU table for the audit without touching what anything is charged.
    (
        "openrouter",
        "anthropic/claude-opus-5",
        "cached_input_tokens",
        "",
        "USD",
        TOKENS,
        "0.50",
        TOKENS,
        "0.50",
        _openrouter("anthropic/claude-opus-5"),
        "Cache read, 10% of the input rate.",
    ),
    (
        "openrouter",
        "anthropic/claude-opus-5",
        "cache_write_5m_tokens",
        "",
        "USD",
        TOKENS,
        "6.25",
        TOKENS,
        "6.25",
        _openrouter("anthropic/claude-opus-5"),
        "Cache write with a 5-minute TTL: input x 1.25.",
    ),
    (
        "openrouter",
        "anthropic/claude-opus-5",
        "cache_write_1h_tokens",
        "",
        "USD",
        TOKENS,
        "10.00",
        TOKENS,
        "10.00",
        _openrouter("anthropic/claude-opus-5"),
        "Cache write with a 1-hour TTL: input x 2.",
    ),
    (
        "openrouter",
        "anthropic/claude-sonnet-5",
        "cached_input_tokens",
        "",
        "USD",
        TOKENS,
        "0.20",
        TOKENS,
        "0.20",
        _openrouter("anthropic/claude-sonnet-5"),
        "Cache read, 10% of the input rate.",
    ),
    (
        "openrouter",
        "anthropic/claude-sonnet-5",
        "cache_write_5m_tokens",
        "",
        "USD",
        TOKENS,
        "2.50",
        TOKENS,
        "2.50",
        _openrouter("anthropic/claude-sonnet-5"),
        "Cache write with a 5-minute TTL: input x 1.25.",
    ),
    (
        "openrouter",
        "anthropic/claude-sonnet-5",
        "cache_write_1h_tokens",
        "",
        "USD",
        TOKENS,
        "4.00",
        TOKENS,
        "4.00",
        _openrouter("anthropic/claude-sonnet-5"),
        "Cache write with a 1-hour TTL: input x 2.",
    ),
    (
        "openrouter",
        "openai/gpt-5.6-sol",
        "cached_input_tokens",
        "",
        "USD",
        TOKENS,
        "0.50",
        TOKENS,
        "0.50",
        _openrouter("openai/gpt-5.6-sol"),
        "Original list cache read: a 90% discount on the 5.00 list input rate. The "
        "0.40 quoted today is a discount on a discount and is not canonical.",
    ),
    (
        "openrouter",
        "openai/gpt-5.6-sol",
        "cache_write_tokens",
        "",
        "USD",
        TOKENS,
        "6.25",
        TOKENS,
        "6.25",
        _openrouter("openai/gpt-5.6-sol"),
        "Original list cache write: input x 1.25.",
    ),
    (
        "openrouter",
        "google/gemini-embedding-2",
        "file_input",
        "",
        "USD",
        TOKENS,
        "0.45",
        TOKENS,
        "0.45",
        _openrouter("google/gemini-embedding-2"),
        "Same rate as image input.",
    ),
    (
        "openrouter",
        "google/gemini-embedding-2",
        "audio_input",
        "",
        "USD",
        TOKENS,
        "6.50",
        TOKENS,
        "6.50",
        _openrouter("google/gemini-embedding-2"),
        "0051 recorded this SKU in a note without seeding it, because this platform "
        "sends no audio to an embedding model. Still true; now it is a row.",
    ),
    (
        "openrouter",
        "google/gemini-embedding-2",
        "video_input",
        "",
        "USD",
        TOKENS,
        "12.00",
        TOKENS,
        "12.00",
        _openrouter("google/gemini-embedding-2"),
        "As above. `video_input` here is an embedding input modality and shares a name "
        "with the video generation scope by coincidence — the two never meet, because a "
        "scope is resolved per provider and model id and this model generates nothing.",
    ),
    (
        "openrouter",
        "openai/gpt-image-2",
        "image_input_tokens",
        "",
        "USD",
        TOKENS,
        "8.00",
        TOKENS,
        "8.00",
        _openrouter("openai/gpt-image-2"),
        "Input image tokens. The OUTPUT image rate is not repeated here: it is the "
        "0.00003 USD/token on this model's `default` row, which is the same 30.00 per "
        "1M in the unit that row already uses, and stating one price twice in two units "
        "is how they drift apart.",
    ),
    (
        "openrouter",
        "openai/gpt-image-2",
        "image_cached_input_tokens",
        "",
        "USD",
        TOKENS,
        "2.00",
        TOKENS,
        "2.00",
        _openrouter("openai/gpt-image-2"),
        "Cached input image tokens.",
    ),
    (
        "openrouter",
        "openai/gpt-image-2",
        "text_input_tokens",
        "",
        "USD",
        TOKENS,
        "5.00",
        TOKENS,
        "5.00",
        _openrouter("openai/gpt-image-2"),
        "Prompt text bills on a separate axis from the image tokens. 0046 recorded this "
        "in the default row's note as 0.000005 USD/token; same rate, stated per 1M.",
    ),
    (
        "openrouter",
        "openai/gpt-image-2",
        "text_cached_input_tokens",
        "",
        "USD",
        TOKENS,
        "1.25",
        TOKENS,
        "1.25",
        _openrouter("openai/gpt-image-2"),
        "Cached prompt text tokens.",
    ),
    (
        "wan",
        "qwen3.8-max",
        "explicit_cache_write_tokens",
        "",
        "CNY",
        TOKENS,
        "15.00",
        TOKENS,
        "15.00",
        DASHSCOPE_PRICING,
        "Beijing list. DashScope charges explicit cache creation ABOVE the input rate "
        "(15.00 against 12.00) and reads back at 1.00. The 1.50 already seeded as "
        "`cached_input_tokens` is the IMPLICIT cache hit, which is a different tier.",
    ),
    (
        "wan",
        "qwen3.8-max",
        "explicit_cache_hit_tokens",
        "",
        "CNY",
        TOKENS,
        "1.00",
        TOKENS,
        "1.00",
        DASHSCOPE_PRICING,
        "Beijing list, explicit cache read. Cheaper than the 1.50 implicit hit.",
    ),
]


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


def _formula(unit: str, direction: str, price_column: str) -> str:
    """Match 0051's expressions exactly: one shape per billing unit."""

    if unit == SECOND:
        return f"{price_column} * duration_seconds * usd_per_currency"
    if unit == "token":
        return f"{price_column} * usage.completion_tokens / 1000000 * usd_per_currency"
    return f"{price_column} * {direction} / 1e6 * usd_per_currency"


def upgrade() -> None:
    connection = op.get_bind()
    if "model_pricing_profiles" not in set(sa.inspect(connection).get_table_names()):
        return
    now = datetime.now(UTC)

    # 1. The promotion stops quoting.
    connection.execute(
        sa.text(
            """
            delete from model_pricing_profiles
             where provider = 'seedance'
               and provider_model_id = :model
               and input_mode = 'no_video_input'
               and resolution = '1080p'
               and unit_price = :price
               and effective_until is not null
            """
        ).bindparams(
            sa.bindparam("model", ARK_MODEL),
            sa.bindparam("price", PROMO_UNIT_PRICE, type_=sa.Numeric(18, 8)),
        )
    )

    # 2. Corrections in place, to the vendor's original list.
    for provider, model, direction, resolution, price, note in REPRICED:
        connection.execute(
            sa.text(
                """
                update model_pricing_profiles
                   set unit_price = :price,
                       estimate_unit_price = :price,
                       notes = :note,
                       source_checked_at = :checked,
                       updated_at = :now
                 where provider = :provider
                   and provider_model_id = :model
                   and input_mode = :direction
                   and resolution = :resolution
                """
            ).bindparams(
                sa.bindparam("price", Decimal(price), type_=sa.Numeric(18, 8)),
                sa.bindparam("note", note),
                sa.bindparam("checked", CHECKED_AT),
                sa.bindparam("now", now),
                sa.bindparam("provider", provider),
                sa.bindparam("model", model),
                sa.bindparam("direction", direction),
                sa.bindparam("resolution", resolution),
            )
        )

    # 3. Scopes that had no published rate before.
    rows: list[dict[str, object]] = []
    for (
        provider,
        model,
        direction,
        resolution,
        currency,
        billing_unit,
        unit_price,
        estimate_unit,
        estimate_price,
        source,
        note,
    ) in SEEDED:
        already = connection.execute(
            sa.text(
                "select count(*) from model_pricing_profiles where provider = :p "
                "and provider_model_id = :m and input_mode = :i and resolution = :r"
            ),
            {"p": provider, "m": model, "i": direction, "r": resolution},
        ).scalar()
        if already:
            continue
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "provider": provider,
                "provider_model_id": model,
                "input_mode": direction,
                "resolution": resolution,
                "currency": currency,
                "billing_unit": billing_unit,
                "unit_price": unit_price,
                "estimate_unit": estimate_unit,
                "estimate_unit_price": estimate_price,
                # The FX snapshot, not a conversion: a CNY row stays a CNY row
                # and USD is derived from this at quote and settlement time.
                "usd_per_currency": 1.0 if currency == "USD" else USD_PER_CNY,
                "fx_source": "" if currency == "USD" else CNY_FX_SOURCE,
                "fx_checked_at": None if currency == "USD" else CHECKED_AT,
                "estimate_formula": _formula(estimate_unit, direction, "estimate_unit_price"),
                "settlement_formula": _formula(billing_unit, direction, "unit_price"),
                "effective_from": CHECKED_AT,
                "effective_until": None,
                "source_url": source,
                "source_checked_at": CHECKED_AT,
                "notes": note,
                "created_at": now,
                "updated_at": now,
            }
        )
    if rows:
        connection.execute(sa.text(_INSERT), rows)


def downgrade() -> None:
    connection = op.get_bind()
    if "model_pricing_profiles" not in set(sa.inspect(connection).get_table_names()):
        return
    now = datetime.now(UTC)

    for provider, model, direction, resolution, _price, _note in REPRICED:
        connection.execute(
            sa.text(
                """
                update model_pricing_profiles
                   set unit_price = :price, estimate_unit_price = :price, updated_at = :now
                 where provider = :provider
                   and provider_model_id = :model
                   and input_mode = :direction
                   and resolution = :resolution
                """
            ).bindparams(
                # What 0051 wrote, which is what a downgrade must restore.
                sa.bindparam(
                    "price",
                    Decimal("2.00") if direction == "input_tokens" else Decimal("10.00"),
                    type_=sa.Numeric(18, 8),
                ),
                sa.bindparam("now", now),
                sa.bindparam("provider", provider),
                sa.bindparam("model", model),
                sa.bindparam("direction", direction),
                sa.bindparam("resolution", resolution),
            )
        )

    for provider, model, direction, resolution, *_rest in SEEDED:
        connection.execute(
            sa.text(
                "delete from model_pricing_profiles where provider = :p "
                "and provider_model_id = :m and input_mode = :i and resolution = :r "
                "and effective_from = :e"
            ),
            {"p": provider, "m": model, "i": direction, "r": resolution, "e": CHECKED_AT},
        )

    # Restore 0044's promotional row exactly as it was inserted.
    already = connection.execute(
        sa.text(
            "select count(*) from model_pricing_profiles where provider = 'seedance' "
            "and provider_model_id = :m and resolution = '1080p' and unit_price = :p"
        ).bindparams(
            sa.bindparam("m", ARK_MODEL),
            # Typed explicitly: an untyped Decimal reaches sqlite3 as an object it
            # cannot adapt, and the whole downgrade rolls back on the last statement.
            sa.bindparam("p", PROMO_UNIT_PRICE, type_=sa.Numeric(18, 8)),
        )
    ).scalar()
    if not already:
        connection.execute(
            sa.text(_INSERT),
            [
                {
                    "id": str(uuid.uuid4()),
                    "provider": "seedance",
                    "provider_model_id": ARK_MODEL,
                    "input_mode": "no_video_input",
                    "resolution": "1080p",
                    "currency": "CNY",
                    "billing_unit": "token",
                    "unit_price": str(PROMO_UNIT_PRICE),
                    "estimate_unit": SECOND,
                    "estimate_unit_price": str(PROMO_ESTIMATE_PRICE),
                    "usd_per_currency": USD_PER_CNY,
                    "fx_source": CNY_FX_SOURCE,
                    "fx_checked_at": datetime(2026, 8, 26, tzinfo=UTC),
                    "estimate_formula": (
                        "estimate_unit_price * duration_seconds * usd_per_currency"
                    ),
                    "settlement_formula": (
                        "unit_price * usage.completion_tokens / 1000000 * usd_per_currency"
                    ),
                    "effective_from": PROMO_FROM,
                    "effective_until": PROMO_UNTIL,
                    "source_url": ARK_PRICING,
                    "source_checked_at": datetime(2026, 8, 26, tzinfo=UTC),
                    "notes": PROMO_NOTE,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )
