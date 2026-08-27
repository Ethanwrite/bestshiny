"""Record the published token rates for the chat, embedding and Wan-snapshot models.

Twelve of the twenty-five registry models bill per token, and none of them
carried a price. `pricing_status` reported UNVERIFIED for all of them, which was
accurate and useless: nobody could say what a prompt-compilation call or a
memory embedding actually costs.

Every figure here was read from the provider's **own** documentation on
2026-08-26, one provider per query, and is stored in that provider's own
currency and billing unit with the URL and date it came from. Nothing is
converted, averaged, or inferred from a reseller.

**Why a script and not a migration.** `0050_router_evidence` is committed on
another branch and not yet on `main`, so a `0051` chained to it would dangle
here. This writes exactly the rows a migration would, is idempotent, and prints
what it would do unless told to write. Fold it into `0051` once `0050` lands.

    uv run python scripts/seed_token_pricing.py             # plan
    uv run python scripts/seed_token_pricing.py --confirm   # write

`input_mode` carries the billed direction — `input_tokens`, `output_tokens`,
`cached_input_tokens`, `image_input` — because each is a separately published
rate for the same model, and the table already keys on that column. Prices are
per **1M tokens**, the unit every one of these providers publishes, rather than
per token: DeepSeek's cache-hit rate is 0.007 USD/1M, which `numeric(18,8)`
cannot hold per-token without rounding it to zero.

Opens no socket. Prints no secret.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from platform_database import Database  # noqa: E402
from platform_shared import Settings  # noqa: E402
from production_domain.models import ModelPricingProfile, new_id  # noqa: E402
from sqlalchemy import select  # noqa: E402

CHECKED_AT = datetime(2026, 8, 26, tzinfo=UTC)

# Same rate and same source as migration 0048, deliberately: the CNY figures
# below were read on the same day, and pairing them with a different day's rate
# would make the USD conversion answer a question nobody asked.
USD_PER_CNY = Decimal("0.14743")
CNY_FX_SOURCE = "PBOC/CFETS central parity 2026-08-26 (100 USD = 678.29 CNY)"

TOKENS = "1M_tokens"
PIXELS = "1B_pixels"

ARK_PRICING = "https://www.volcengine.com/docs/82379/1544106"
DASHSCOPE_PRICING = "https://help.aliyun.com/zh/model-studio/model-pricing"
DEEPSEEK_PRICING = "https://api-docs.deepseek.com/quick_start/pricing/"


@dataclass(frozen=True)
class Rate:
    provider: str
    model: str
    direction: str
    price: str
    currency: str
    source_url: str
    unit: str = TOKENS
    resolution: str = ""
    note: str = ""
    formula: str = field(default="")


def _openrouter(model: str, *, input_price: str, output_price: str, note: str = "") -> list[Rate]:
    url = f"https://openrouter.ai/{model}"
    return [
        Rate("openrouter", model, "input_tokens", input_price, "USD", url, note=note),
        Rate("openrouter", model, "output_tokens", output_price, "USD", url, note=note),
    ]


RATES: list[Rate] = [
    # ---- OpenRouter chat ---------------------------------------------------
    *_openrouter("anthropic/claude-opus-5", input_price="5.00", output_price="25.00"),
    *_openrouter("anthropic/claude-sonnet-5", input_price="2.00", output_price="10.00"),
    *_openrouter(
        "openai/gpt-5.6-sol",
        input_price="2.00",
        output_price="10.00",
        note=(
            "Rate for prompts of 272,000 tokens or fewer. Above that OpenRouter charges "
            "4.00 input / 15.00 output per 1M. The profile keys on input mode and "
            "resolution, not prompt length, so the long-context tier is recorded here "
            "rather than seeded. Reasoning tokens are billed as output."
        ),
    ),
    # ---- OpenRouter embeddings --------------------------------------------
    Rate(
        "openrouter",
        "google/gemini-embedding-2",
        "input_tokens",
        "0.20",
        "USD",
        "https://openrouter.ai/google/gemini-embedding-2",
        note=(
            "Output is not billed: OpenRouter publishes completion \"0\" for this model. "
            "Context is 8,192 tokens per input. Audio (6.50) and video (12.00) input SKUs "
            "exist and are not seeded because this platform sends neither."
        ),
    ),
    Rate(
        "openrouter",
        "google/gemini-embedding-2",
        "image_input",
        "0.45",
        "USD",
        "https://openrouter.ai/google/gemini-embedding-2",
    ),
    Rate(
        "openrouter",
        "voyageai/voyage-multimodal-3.5",
        "input_tokens",
        "0.12",
        "USD",
        "https://openrouter.ai/voyageai/voyage-multimodal-3.5",
        note=(
            "Output is not billed. Context is 32,000 tokens. Voyage stays ADVISORY only "
            "on this platform, never identity, state, delta or commit authority."
        ),
    ),
    Rate(
        "openrouter",
        "voyageai/voyage-multimodal-3.5",
        "image_input",
        "0.60",
        "USD",
        "https://openrouter.ai/voyageai/voyage-multimodal-3.5",
        unit=PIXELS,
        note="Billed per billion pixels, not per token — a different unit, not a conversion.",
    ),
    # ---- Volcengine Ark chat ----------------------------------------------
    Rate(
        "seedance",
        "doubao-seed-2-0-lite-260428",
        "input_tokens",
        "0.60",
        "CNY",
        ARK_PRICING,
        note=(
            "Beijing list, 常规 online inference, [0,32]k input band. Ark tiers by the "
            "input length of the request: (32,128]k is 0.9/0.18/5.4 and (128,256]k is "
            "1.8/0.36/10.8. 低延迟 doubles the non-cache rates and 批量 halves input; this "
            "deployment selects neither. Audio input is a separate, much higher rate."
        ),
    ),
    Rate("seedance", "doubao-seed-2-0-lite-260428", "cached_input_tokens", "0.12", "CNY", ARK_PRICING),
    Rate("seedance", "doubao-seed-2-0-lite-260428", "output_tokens", "3.60", "CNY", ARK_PRICING),
    Rate(
        "seedance",
        "glm-5.2",
        "input_tokens",
        "8.00",
        "CNY",
        ARK_PRICING,
        note=(
            "Beijing list, flat — no input-length tiers. Priced under the name `glm-5.2`, "
            "which is what Ark's price table uses. Whether that dotted string is accepted "
            "in the pay-as-you-go `model` field is NOT confirmed: the versioned Model ID "
            "in Ark's own decommission notice is `glm-5-2-260617`, and Ark began "
            "decommissioning this model on 2026-08-17 with `glm-5.3` as the replacement. "
            "The price has a source; the contract does not. A canary settles which."
        ),
    ),
    Rate("seedance", "glm-5.2", "cached_input_tokens", "2.00", "CNY", ARK_PRICING),
    Rate("seedance", "glm-5.2", "output_tokens", "28.00", "CNY", ARK_PRICING),
    # ---- Alibaba DashScope chat -------------------------------------------
    Rate(
        "wan",
        "qwen3.8-max",
        "input_tokens",
        "12.00",
        "CNY",
        DASHSCOPE_PRICING,
        note=(
            "Beijing (华北2) list, single 0-1M band with no context-length tiers. Thinking "
            "is ON by default with reasoning_effort defaulting to xhigh, and chain-of-thought "
            "is billed as OUTPUT — a default that moves the bill and must be stated, not "
            "inherited. Free trial quota is 1M tokens for 90 days, Beijing only. Singapore "
            "(14.988/44.965) is a different region and is not seeded."
        ),
    ),
    Rate("wan", "qwen3.8-max", "cached_input_tokens", "1.50", "CNY", DASHSCOPE_PRICING),
    Rate("wan", "qwen3.8-max", "output_tokens", "36.00", "CNY", DASHSCOPE_PRICING),
    # ---- DeepSeek ----------------------------------------------------------
    Rate(
        "deepseek",
        "deepseek-v4-flash",
        "input_tokens",
        "0.44",
        "USD",
        DEEPSEEK_PRICING,
        note=(
            "PEAK rate, cache miss. Off-peak is exactly half (0.22/0.007/0.66). Peak is "
            "Mon-Fri 01:00-04:00 and 06:00-10:00 UTC; all other hours including weekends "
            "are off-peak. `effective_from`/`effective_until` are absolute instants, not a "
            "recurring window, so the schema cannot express this — the higher of the two "
            "published rates is seeded, because quoting it can never under-charge. Rates "
            "took effect 16:00 UTC 2026-08-16. Thinking is on by default "
            "(reasoning_effort=high). This model rejects images with HTTP 400."
        ),
    ),
    Rate("deepseek", "deepseek-v4-flash", "cached_input_tokens", "0.014", "USD", DEEPSEEK_PRICING),
    Rate("deepseek", "deepseek-v4-flash", "output_tokens", "1.32", "USD", DEEPSEEK_PRICING),
    # ---- RunAPI ------------------------------------------------------------
    Rate(
        "runapi",
        "gpt-5.6-luna",
        "input_tokens",
        "0.20",
        "USD",
        "https://runapi.ai/models/gpt/5.6-luna",
        note=(
            "Rate for prompts below 272,001 tokens; at or above it RunAPI charges "
            "0.40/0.04/1.80. RunAPI is an aggregator (LinkCode LLC), not the origin host — "
            "not to be confused with runapi.co / .host / .net. Failed generations are not "
            "charged. Its variant page also carries `billing_unit: 1K tokens` while every "
            "displayed price is per 1M; the displayed figures are used. RunAPI remains a "
            "low-trust edge provider on this platform."
        ),
    ),
    Rate("runapi", "gpt-5.6-luna", "cached_input_tokens", "0.02", "USD", "https://runapi.ai/models/gpt/5.6-luna"),
    Rate("runapi", "gpt-5.6-luna", "output_tokens", "1.20", "USD", "https://runapi.ai/models/gpt/5.6-luna"),
]

# Wan's price was keyed on the family key `wan-2.7`, but any deployment that sets
# WAN2_7_T2V_MODEL_ID moves the registry row onto a mode snapshot, and the lookup
# then finds nothing. 0048 established the rate does not vary by mode, so the same
# Beijing per-second figures are recorded against the snapshots the adapter posts.
# r2v is deliberately absent: it bills min(input_seconds, 5) + output_seconds,
# which a per-second estimate does not model — 0048 left it unpriced for that
# reason and this does not quietly change it.
WAN_SNAPSHOTS = ("wan2.7-t2v-2026-06-12", "wan2.7-i2v-2026-04-25")
WAN_CNY_PER_SECOND = {"720p": "0.60", "1080p": "1.00"}

for _snapshot in WAN_SNAPSHOTS:
    for _resolution, _price in WAN_CNY_PER_SECOND.items():
        RATES.append(
            Rate(
                "wan",
                _snapshot,
                "no_video_input",
                _price,
                "CNY",
                DASHSCOPE_PRICING,
                unit="second",
                resolution=_resolution,
                note=(
                    "Beijing list, same rate as the `wan-2.7` family key: 0048 established "
                    "the price does not vary by t2v/i2v/r2v. Seeded against the snapshot "
                    "because a deployment that declares WAN2_7_T2V_MODEL_ID moves the "
                    "registry row here. r2v is not seeded: it also bills "
                    "min(input_seconds, 5) of input video."
                ),
                formula="estimate_unit_price * duration_seconds * usd_per_currency",
            )
        )


def _estimate_formula(rate: Rate) -> str:
    if rate.formula:
        return rate.formula
    if rate.unit == PIXELS:
        return "estimate_unit_price * input_pixels / 1e9 * usd_per_currency"
    return f"estimate_unit_price * {rate.direction} / 1e6 * usd_per_currency"


def run(confirm: bool) -> int:
    settings = Settings()
    database = Database(settings.database_url)
    written = skipped = 0

    with database.session() as session:
        for rate in RATES:
            existing = session.scalar(
                select(ModelPricingProfile).where(
                    ModelPricingProfile.provider == rate.provider,
                    ModelPricingProfile.provider_model_id == rate.model,
                    ModelPricingProfile.input_mode == rate.direction,
                    ModelPricingProfile.resolution == rate.resolution,
                    ModelPricingProfile.effective_from == CHECKED_AT,
                )
            )
            if existing is not None:
                skipped += 1
                continue
            scope = f"{rate.provider}:{rate.model} {rate.direction}"
            if rate.resolution:
                scope += f" @{rate.resolution}"
            action = "write" if confirm else "would write"
            print(f"  {action:12} {scope:58} {rate.price} {rate.currency}/{rate.unit}")
            written += 1
            if not confirm:
                continue
            session.add(
                ModelPricingProfile(
                    id=new_id(),
                    provider=rate.provider,
                    provider_model_id=rate.model,
                    input_mode=rate.direction,
                    resolution=rate.resolution,
                    currency=rate.currency,
                    billing_unit=rate.unit,
                    unit_price=Decimal(rate.price),
                    estimate_unit=rate.unit,
                    estimate_unit_price=Decimal(rate.price),
                    usd_per_currency=Decimal("1") if rate.currency == "USD" else USD_PER_CNY,
                    fx_source="" if rate.currency == "USD" else CNY_FX_SOURCE,
                    fx_checked_at=None if rate.currency == "USD" else CHECKED_AT,
                    estimate_formula=_estimate_formula(rate),
                    settlement_formula=_estimate_formula(rate).replace(
                        "estimate_unit_price", "unit_price"
                    ),
                    effective_from=CHECKED_AT,
                    source_url=rate.source_url,
                    source_checked_at=CHECKED_AT,
                    notes=rate.note,
                )
            )

    print(f"\n  {written} row(s) {'written' if confirm else 'to write'}, {skipped} already present.")
    if not confirm and written:
        print("\n  Plan only. Re-run with --confirm to write.\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="write the rows; otherwise plan only")
    return run(parser.parse_args().confirm)


if __name__ == "__main__":
    raise SystemExit(main())
