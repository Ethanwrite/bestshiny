"""Price the twelve per-token models, and close the Wan snapshot gap.

Twelve of the twenty-five registry models bill per token and none of them carried
a price. `pricing_status` reported UNVERIFIED for all of them, which was accurate
and useless: nobody could say what a prompt compilation or a memory embedding
costs, and in live mode an unpriced model is refused rather than estimated.

Every figure was read from the provider's **own** documentation on 2026-08-26,
one provider per query, and is stored in that provider's own currency and billing
unit with the URL and the date it came from. Nothing here is converted from a
reseller's listing, averaged, or inferred from a sibling model.

**Unit.** Prices are per 1M tokens, which is what all five providers publish.
Per-token would not survive the column: DeepSeek's cache-hit rate is 0.007
USD/1M, and `numeric(18,8)` rounds that to zero.

**`input_mode` carries the billed direction** — `input_tokens`, `output_tokens`,
`cached_input_tokens`, `image_input` — because each is a separately published
rate for the same model and the table already keys on that column.

**The Wan gap.** 0048 keyed Wan's price on the family key `wan-2.7`, but any
deployment that sets `WAN2_7_T2V_MODEL_ID` moves the registry row onto a mode
snapshot, and the lookup then finds nothing — VERIFIED in the report, refused at
the till. The t2v and i2v snapshots now carry the same Beijing per-second rate;
0048 established it does not vary by mode. **r2v is deliberately absent**: it
also bills `min(input_seconds, 5)` of input video, which a per-second estimate
does not model, and 0048 left it unpriced for exactly that reason.

Recorded in `notes` rather than seeded, because the profile keys on input mode
and resolution and cannot express them:

* **Prompt-length tiers.** `openai/gpt-5.6-sol` and `gpt-5.6-luna` both double
  above roughly 272K prompt tokens. Every request this platform builds is far
  below it.
* **DeepSeek's off-peak window.** Half price outside Mon-Fri 01:00-04:00 and
  06:00-10:00 UTC. `effective_from`/`effective_until` are absolute instants, not
  a recurring window. The **peak** rate is seeded, because quoting the higher of
  two published rates can never under-charge.
* **Ark's input-length bands.** `doubao-seed-2.0-lite` has two further bands
  above 32K. The [0,32]k band is seeded.
* **`glm-5.2`.** Priced under the name Ark's own price table uses, while the
  callable Model ID is *not* confirmed — Ark's decommission notice names
  `glm-5-2-260617`, and Ark began retiring this model on 2026-08-17. The price
  has a source; the contract does not. A canary settles which.

The five models this migration does not price — `grok-imagine-video` on the
transport-less `grok` provider, `veo-3.1-generate-preview` on `veo_official`,
`wan3.0-video`, and both Google Flow models — are left refused on purpose. Flow
publishes no third-party API and no per-call or per-credit price at all, so
there is nothing to record; the others have no confirmed rate for the account
this platform holds. A price for a string nobody can call is a number waiting to
be believed.

Revision ID: 0051_token_pricing
Revises: 0050_router_evidence
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0051_token_pricing"
down_revision: str | None = "0050_router_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKED_AT = datetime(2026, 8, 26, tzinfo=UTC)

# Same rate and same source as 0048, deliberately: the CNY figures below were read
# on the same day, and pairing them with a different day's rate would make the USD
# conversion answer a question nobody asked.
USD_PER_CNY = 0.14743
CNY_FX_SOURCE = "PBOC/CFETS central parity 2026-08-26 (100 USD = 678.29 CNY)"

TOKENS = "1M_tokens"
PIXELS = "1B_pixels"

ARK_PRICING = "https://www.volcengine.com/docs/82379/1544106"
DASHSCOPE_PRICING = "https://help.aliyun.com/zh/model-studio/model-pricing"
DEEPSEEK_PRICING = "https://api-docs.deepseek.com/quick_start/pricing/"
RUNAPI_PRICING = "https://runapi.ai/models/gpt/5.6-luna"

_SOL_TIER = (
    "Rate for prompts of 272,000 tokens or fewer. Above that OpenRouter charges "
    "4.00 input / 15.00 output per 1M. The profile keys on input mode and resolution, "
    "not prompt length, so the long-context tier is recorded rather than seeded. "
    "Reasoning tokens are billed as output."
)
_GEMINI_EMBED = (
    'Output is not billed: OpenRouter publishes completion "0" for this model. Context '
    "is 8,192 tokens per input. Audio (6.50) and video (12.00) input SKUs exist and are "
    "not seeded because this platform sends neither."
)
_VOYAGE = (
    "Output is not billed. Context is 32,000 tokens. Voyage stays ADVISORY only on this "
    "platform, never identity, state, delta or commit authority."
)
_DOUBAO = (
    "Beijing list, 常规 online inference, [0,32]k input band. Ark tiers by the input "
    "length of the request: (32,128]k is 0.9/0.18/5.4 and (128,256]k is 1.8/0.36/10.8. "
    "低延迟 doubles the non-cache rates and 批量 halves input; this deployment selects "
    "neither. Audio input is a separate, much higher rate."
)
_GLM = (
    "Beijing list, flat — no input-length tiers. Priced under the name `glm-5.2`, which "
    "is what Ark's price table uses. Whether that dotted string is accepted in the "
    "pay-as-you-go `model` field is NOT confirmed: the versioned Model ID in Ark's own "
    "decommission notice is `glm-5-2-260617`, and Ark began decommissioning this model "
    "on 2026-08-17 with `glm-5.3` as the replacement. The price has a source; the "
    "contract does not."
)
_QWEN = (
    "Beijing (华北2) list, single 0-1M band with no context-length tiers. Thinking is ON "
    "by default with reasoning_effort defaulting to xhigh, and chain-of-thought is billed "
    "as OUTPUT — a default that moves the bill and must be stated, not inherited. Free "
    "trial quota is 1M tokens for 90 days, Beijing only. Singapore (14.988/44.965) is a "
    "different region and is not seeded."
)
_DEEPSEEK = (
    "PEAK rate, cache miss. Off-peak is exactly half (0.22/0.007/0.66). Peak is Mon-Fri "
    "01:00-04:00 and 06:00-10:00 UTC; all other hours including weekends are off-peak. "
    "`effective_from`/`effective_until` are absolute instants, not a recurring window, so "
    "the schema cannot express this — the higher of the two published rates is seeded, "
    "because quoting it can never under-charge. Rates took effect 16:00 UTC 2026-08-16. "
    "Thinking is on by default (reasoning_effort=high). This model rejects images with "
    "HTTP 400; deepseek-v4-flash-vision-exp is the vision id."
)
_RUNAPI = (
    "Rate for prompts below 272,001 tokens; at or above it RunAPI charges 0.40/0.04/1.80. "
    "RunAPI is an aggregator (LinkCode LLC), not the origin host — not to be confused "
    "with runapi.co / .host / .net. Failed generations are not charged. Its variant page "
    "also carries `billing_unit: 1K tokens` while every displayed price is per 1M; the "
    "displayed figures are used. RunAPI remains a low-trust edge provider here."
)
_WAN_SNAPSHOT = (
    "Beijing list, same rate as the `wan-2.7` family key: 0048 established the price does "
    "not vary by t2v/i2v/r2v. Seeded against the snapshot because a deployment that "
    "declares WAN2_7_T2V_MODEL_ID moves the registry row here. r2v is not seeded: it also "
    "bills min(input_seconds, 5) of input video."
)


def _openrouter(model: str) -> str:
    return f"https://openrouter.ai/{model}"


# One row per published rate. `input_mode` carries the billed direction; `unit`
# is the provider's own billing unit, never a conversion of it.
Rate = tuple[str, str, str, str, str, str, str, str, str]


def _token(
    provider: str, model: str, direction: str, price: str, currency: str, source: str, note: str = ""
) -> Rate:
    return (provider, model, direction, price, currency, TOKENS, "", source, note)


def _chat(
    provider: str,
    model: str,
    source: str,
    *,
    inp: str,
    out: str,
    cached: str = "",
    note: str = "",
) -> list[Rate]:
    rows = [
        _token(provider, model, "input_tokens", inp, _CURRENCY[provider], source, note),
        _token(provider, model, "output_tokens", out, _CURRENCY[provider], source),
    ]
    if cached:
        rows.insert(
            1, _token(provider, model, "cached_input_tokens", cached, _CURRENCY[provider], source)
        )
    return rows


_CURRENCY = {
    "openrouter": "USD",
    "seedance": "CNY",
    "wan": "CNY",
    "deepseek": "USD",
    "runapi": "USD",
}

RATES: list[Rate] = [
    *_chat(
        "openrouter",
        "anthropic/claude-opus-5",
        _openrouter("anthropic/claude-opus-5"),
        inp="5.00",
        out="25.00",
    ),
    *_chat(
        "openrouter",
        "anthropic/claude-sonnet-5",
        _openrouter("anthropic/claude-sonnet-5"),
        inp="2.00",
        out="10.00",
    ),
    *_chat(
        "openrouter",
        "openai/gpt-5.6-sol",
        _openrouter("openai/gpt-5.6-sol"),
        inp="2.00",
        out="10.00",
        note=_SOL_TIER,
    ),
    _token(
        "openrouter",
        "google/gemini-embedding-2",
        "input_tokens",
        "0.20",
        "USD",
        _openrouter("google/gemini-embedding-2"),
        _GEMINI_EMBED,
    ),
    _token(
        "openrouter",
        "google/gemini-embedding-2",
        "image_input",
        "0.45",
        "USD",
        _openrouter("google/gemini-embedding-2"),
    ),
    _token(
        "openrouter",
        "voyageai/voyage-multimodal-3.5",
        "input_tokens",
        "0.12",
        "USD",
        _openrouter("voyageai/voyage-multimodal-3.5"),
        _VOYAGE,
    ),
    (
        "openrouter",
        "voyageai/voyage-multimodal-3.5",
        "image_input",
        "0.60",
        "USD",
        PIXELS,
        "",
        _openrouter("voyageai/voyage-multimodal-3.5"),
        "Billed per billion pixels, not per token — a different unit, not a conversion.",
    ),
    *_chat(
        "seedance",
        "doubao-seed-2-0-lite-260428",
        ARK_PRICING,
        inp="0.60",
        out="3.60",
        cached="0.12",
        note=_DOUBAO,
    ),
    *_chat("seedance", "glm-5.2", ARK_PRICING, inp="8.00", out="28.00", cached="2.00", note=_GLM),
    *_chat("wan", "qwen3.8-max", DASHSCOPE_PRICING, inp="12.00", out="36.00", cached="1.50", note=_QWEN),
    *_chat(
        "deepseek",
        "deepseek-v4-flash",
        DEEPSEEK_PRICING,
        inp="0.44",
        out="1.32",
        cached="0.014",
        note=_DEEPSEEK,
    ),
    *_chat("runapi", "gpt-5.6-luna", RUNAPI_PRICING, inp="0.20", out="1.20", cached="0.02", note=_RUNAPI),
]

# Wan's two mode snapshots, at the family key's Beijing per-second rate.
for _snapshot in ("wan2.7-t2v-2026-06-12", "wan2.7-i2v-2026-04-25"):
    for _resolution, _price in (("720p", "0.60"), ("1080p", "1.00")):
        RATES.append(
            (
                "wan",
                _snapshot,
                "no_video_input",
                _price,
                "CNY",
                "second",
                _resolution,
                DASHSCOPE_PRICING,
                _WAN_SNAPSHOT,
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


def _formula(unit: str, direction: str, price_column: str) -> str:
    if unit == PIXELS:
        return f"{price_column} * input_pixels / 1e9 * usd_per_currency"
    if unit == "second":
        return f"{price_column} * duration_seconds * usd_per_currency"
    return f"{price_column} * {direction} / 1e6 * usd_per_currency"


def upgrade() -> None:
    connection = op.get_bind()
    if "model_pricing_profiles" not in set(sa.inspect(connection).get_table_names()):
        return
    now = datetime.now(UTC)
    rows: list[dict[str, object]] = []
    for provider, model, direction, price, currency, unit, resolution, source, note in RATES:
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
                "billing_unit": unit,
                "unit_price": price,
                "estimate_unit": unit,
                "estimate_unit_price": price,
                "usd_per_currency": 1.0 if currency == "USD" else USD_PER_CNY,
                "fx_source": "" if currency == "USD" else CNY_FX_SOURCE,
                "fx_checked_at": None if currency == "USD" else CHECKED_AT,
                "estimate_formula": _formula(unit, direction, "estimate_unit_price"),
                "settlement_formula": _formula(unit, direction, "unit_price"),
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
    for provider, model, direction, _price, _currency, _unit, resolution, _source, _note in RATES:
        connection.execute(
            sa.text(
                "delete from model_pricing_profiles where provider = :p "
                "and provider_model_id = :m and input_mode = :i and resolution = :r "
                "and effective_from = :e"
            ),
            {"p": provider, "m": model, "i": direction, "r": resolution, "e": CHECKED_AT},
        )
