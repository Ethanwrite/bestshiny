"""The rates migration `0051_token_pricing` writes, checked for how price tables go wrong.

The figures came from each provider's own documentation and cannot be verified
from inside the repository. What can be verified is that none of them lost the
properties that make a recorded price trustworthy: a source on that provider's
own domain, a date, its own currency, and an FX rate that is not silently 1.

The table is read out of the migrations, not out of the script — `0051` owns the
rows, `0062` owns the corrections to them, and the script imports both and
composes them, so a drift between the three is impossible rather than merely
tested for.

Pure data assertions. No database, no socket.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.seed_token_pricing import (
    CHECKED_AT,
    CNY_FX_SOURCE,
    PIXELS,
    RATES,
    TOKENS,
    USD_PER_CNY,
    formula_for,
)

# Field order of a rate tuple in the migration.
PROVIDER, MODEL, DIRECTION, PRICE, CURRENCY, UNIT, RESOLUTION, SOURCE, NOTE = range(9)

# Every registry model that bills per token. Absent on purpose: the five models
# whose provider IDs are unroutable or whose price is unpublished — a price for a
# string nobody can call is a number waiting to be believed.
EXPECTED_TOKEN_MODELS = {
    ("openrouter", "anthropic/claude-opus-5"),
    ("openrouter", "anthropic/claude-sonnet-5"),
    ("openrouter", "openai/gpt-5.6-sol"),
    ("openrouter", "google/gemini-embedding-2"),
    ("openrouter", "voyageai/voyage-multimodal-3.5"),
    ("seedance", "doubao-seed-2-0-lite-260428"),
    ("seedance", "glm-5.2"),
    ("wan", "qwen3.8-max"),
    ("deepseek", "deepseek-v4-flash"),
    ("runapi", "gpt-5.6-luna"),
}

PROVIDER_DOMAINS = {
    "openrouter": "openrouter.ai",
    "seedance": "volcengine.com",
    "wan": "aliyun.com",
    "deepseek": "deepseek.com",
    "runapi": "runapi.ai",
}


def _prices(provider: str, model: str) -> dict[str, Decimal]:
    return {
        rate[DIRECTION]: Decimal(rate[PRICE])
        for rate in RATES
        if (rate[PROVIDER], rate[MODEL]) == (provider, model)
    }


def test_every_per_token_registry_model_is_covered() -> None:
    covered = {
        (rate[PROVIDER], rate[MODEL]) for rate in RATES if rate[UNIT] in {TOKENS, PIXELS}
    }
    assert EXPECTED_TOKEN_MODELS <= covered


@pytest.mark.parametrize(
    "rate", RATES, ids=lambda r: f"{r[PROVIDER]}:{r[MODEL]}:{r[DIRECTION]}"
)
def test_a_rate_carries_the_source_that_makes_it_a_price(rate: tuple) -> None:
    """A price without a source is not a price — the standing rule, as a test."""

    assert rate[SOURCE].startswith("https://")
    assert Decimal(rate[PRICE]) > 0
    assert rate[CURRENCY] in {"USD", "CNY"}


def test_no_rate_is_sourced_from_a_reseller_of_another_providers_model() -> None:
    """Each provider's rows must name that provider's own domain.

    Mixing Ark, BytePlus, OpenRouter and DashScope figures is the specific
    failure this audit exists to prevent, and the source URL is where it would
    first be visible.
    """

    for rate in RATES:
        expected = PROVIDER_DOMAINS[rate[PROVIDER]]
        assert expected in rate[SOURCE], (
            f"{rate[PROVIDER]}:{rate[MODEL]} is sourced from {rate[SOURCE]}, not {expected}"
        )


def test_a_cny_rate_is_never_treated_as_dollars() -> None:
    """`usd_per_currency` of 1 on a CNY row would understate the bill ~6.8x."""

    assert USD_PER_CNY < Decimal("0.2")
    assert "2026-08-26" in CNY_FX_SOURCE
    assert any(rate[CURRENCY] == "CNY" for rate in RATES)


def test_the_two_directions_of_a_chat_model_are_priced_separately() -> None:
    """Output costs several times input everywhere here; one number cannot serve."""

    chat = EXPECTED_TOKEN_MODELS - {
        ("openrouter", "google/gemini-embedding-2"),
        ("openrouter", "voyageai/voyage-multimodal-3.5"),
    }
    for provider, model in chat:
        prices = _prices(provider, model)
        assert {"input_tokens", "output_tokens"} <= set(prices), f"{model} is missing a direction"
        assert prices["output_tokens"] > prices["input_tokens"], (
            f"{model} prices output at or below input, which none of these providers do"
        )


def test_an_embedding_model_is_not_given_an_output_rate() -> None:
    """Both publish completion "0"; inventing an output price would be a fabrication."""

    for model in ("google/gemini-embedding-2", "voyageai/voyage-multimodal-3.5"):
        assert "output_tokens" not in _prices("openrouter", model)


def test_a_cached_input_rate_is_cheaper_than_an_uncached_one() -> None:
    """A cache rate that is not a discount is a transcription error."""

    for provider, model in EXPECTED_TOKEN_MODELS:
        prices = _prices(provider, model)
        if "cached_input_tokens" in prices:
            assert prices["cached_input_tokens"] < prices["input_tokens"], model


def test_wan_snapshots_carry_one_family_rate_across_every_deployment() -> None:
    """0048 established that the Wan 2.7 price does not vary by t2v/i2v/r2v.

    This is the half of the rule 0051 seeded: the two snapshots a deployment's
    settings actually move the registry row onto, both at the Beijing family
    rate. r2v is absent *here* on purpose — 0051 left it out because it also
    bills min(input_seconds, 5) of input video, which a per-second output
    estimate does not model.

    0062 has since priced r2v anyway, at the same family rate, because unpriced
    turned out to mean a Wan continuation was refused outright rather than
    under-quoted; the shortfall is bounded at five seconds and the 1.20 service
    reserve covers it. That decision lives in 0062 and is asserted there. What
    this test still holds is that 0051's own table did not quietly acquire it,
    and that every rate it does carry is the one family figure.
    """

    seconds = {rate[MODEL] for rate in RATES if rate[PROVIDER] == "wan" and rate[UNIT] == "second"}
    assert seconds == {"wan2.7-t2v-2026-06-12", "wan2.7-i2v-2026-04-25"}
    for rate in RATES:
        if rate[PROVIDER] == "wan" and rate[UNIT] == "second":
            assert rate[RESOLUTION] in {"720p", "1080p"}
            expected = Decimal("0.60") if rate[RESOLUTION] == "720p" else Decimal("1.00")
            assert Decimal(rate[PRICE]) == expected


def test_a_pixel_rate_is_not_described_as_a_token_rate() -> None:
    """Voyage bills images per billion pixels — a different unit, not a conversion."""

    pixel_rates = [rate for rate in RATES if rate[UNIT] == PIXELS]
    assert pixel_rates
    for rate in pixel_rates:
        expression = formula_for(rate[UNIT], rate[DIRECTION], "estimate_unit_price")
        assert "pixels" in expression
        assert "1e6" not in expression


def test_a_rate_the_schema_cannot_fully_express_says_so_in_its_note() -> None:
    """Tiers and time windows are recorded, not silently dropped."""

    notes = {(rate[PROVIDER], rate[MODEL]): rate[NOTE] for rate in RATES if rate[NOTE]}
    assert "272,000" in notes[("openrouter", "openai/gpt-5.6-sol")]
    assert "272,001" in notes[("runapi", "gpt-5.6-luna")]
    assert "off-peak" in notes[("deepseek", "deepseek-v4-flash")].lower()
    assert "NOT confirmed" in notes[("seedance", "glm-5.2")]


def test_every_rate_is_dated_so_it_can_go_stale_visibly() -> None:
    assert CHECKED_AT.year == 2026
    assert CHECKED_AT.tzinfo is not None
