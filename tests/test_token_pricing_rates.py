"""The recorded token rates, checked for the ways a price table goes wrong.

These are the twelve per-token models plus the two Wan snapshots. The rates
themselves came from each provider's own documentation and cannot be verified
from inside the repository — what can be verified is that none of them lost the
properties that make a recorded price trustworthy: a source, a date, its own
currency, and an FX rate that is not silently 1.

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
    _estimate_formula,
)

# Every registry model that bills per token. Absent on purpose: the five models
# whose provider IDs are wrong or unverifiable (grok-video, veo-3.1-quality,
# wan-3.0, NARWHAL, flow-veo-3.1) — a price for a string no provider publishes
# would be a number waiting to be believed.
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


def test_every_per_token_registry_model_is_covered() -> None:
    covered = {(rate.provider, rate.model) for rate in RATES if rate.unit in {TOKENS, PIXELS}}
    assert EXPECTED_TOKEN_MODELS <= covered


@pytest.mark.parametrize("rate", RATES, ids=lambda r: f"{r.provider}:{r.model}:{r.direction}")
def test_a_rate_carries_the_source_that_makes_it_a_price(rate) -> None:  # type: ignore[no-untyped-def]
    """A price without a source is not a price — the standing rule, as a test."""

    assert rate.source_url.startswith("https://")
    assert rate.price
    assert Decimal(rate.price) > 0
    assert rate.currency in {"USD", "CNY"}


def test_no_rate_is_recorded_against_a_reseller_of_another_providers_model() -> None:
    """Each provider's rows must name that provider's own domain.

    Mixing Ark, BytePlus, OpenRouter and DashScope figures is the specific
    failure this audit exists to prevent, and a source URL is where it would
    first be visible.
    """

    domains = {
        "openrouter": "openrouter.ai",
        "seedance": "volcengine.com",
        "wan": "aliyun.com",
        "deepseek": "deepseek.com",
        "runapi": "runapi.ai",
    }
    for rate in RATES:
        assert domains[rate.provider] in rate.source_url, (
            f"{rate.provider}:{rate.model} is sourced from {rate.source_url}, "
            f"which is not {domains[rate.provider]}"
        )


def test_a_cny_rate_is_never_treated_as_dollars() -> None:
    """`usd_per_currency` of 1 on a CNY row would understate the bill ~6.8x."""

    assert USD_PER_CNY < Decimal("0.2")
    assert CNY_FX_SOURCE
    assert "2026-08-26" in CNY_FX_SOURCE
    assert any(rate.currency == "CNY" for rate in RATES)


def test_the_two_directions_of_a_chat_model_are_priced_separately() -> None:
    """Output costs several times input everywhere here; one number cannot serve."""

    chat = {
        ("openrouter", "anthropic/claude-opus-5"),
        ("openrouter", "anthropic/claude-sonnet-5"),
        ("openrouter", "openai/gpt-5.6-sol"),
        ("seedance", "doubao-seed-2-0-lite-260428"),
        ("seedance", "glm-5.2"),
        ("wan", "qwen3.8-max"),
        ("deepseek", "deepseek-v4-flash"),
        ("runapi", "gpt-5.6-luna"),
    }
    for provider, model in chat:
        directions = {
            rate.direction for rate in RATES if (rate.provider, rate.model) == (provider, model)
        }
        assert {"input_tokens", "output_tokens"} <= directions, f"{model} is missing a direction"
        prices = {
            rate.direction: Decimal(rate.price)
            for rate in RATES
            if (rate.provider, rate.model) == (provider, model)
        }
        assert prices["output_tokens"] > prices["input_tokens"], (
            f"{model} prices output at or below input, which none of these providers do"
        )


def test_a_cached_input_rate_is_cheaper_than_an_uncached_one() -> None:
    """A cache rate that is not a discount is a transcription error."""

    for provider, model in EXPECTED_TOKEN_MODELS:
        prices = {
            rate.direction: Decimal(rate.price)
            for rate in RATES
            if (rate.provider, rate.model) == (provider, model)
        }
        if "cached_input_tokens" in prices:
            assert prices["cached_input_tokens"] < prices["input_tokens"], model


def test_wan_snapshots_carry_the_family_rate_and_r2v_stays_unpriced() -> None:
    """r2v also bills input video, which a per-second estimate cannot model.

    0048 left it unpriced for that reason. Seeding the two snapshots that a
    deployment's settings actually move the registry row onto must not quietly
    reverse that decision.
    """

    wan_seconds = {rate.model for rate in RATES if rate.provider == "wan" and rate.unit == "second"}
    assert wan_seconds == {"wan2.7-t2v-2026-06-12", "wan2.7-i2v-2026-04-25"}
    assert not any("r2v" in model for model in wan_seconds)
    for rate in RATES:
        if rate.provider == "wan" and rate.unit == "second":
            assert rate.resolution in {"720p", "1080p"}
            assert Decimal(rate.price) == (
                Decimal("0.60") if rate.resolution == "720p" else Decimal("1.00")
            )


def test_a_pixel_rate_is_not_described_as_a_token_rate() -> None:
    """Voyage bills images per billion pixels — a different unit, not a conversion."""

    pixel_rates = [rate for rate in RATES if rate.unit == PIXELS]
    assert pixel_rates
    for rate in pixel_rates:
        assert "pixels" in _estimate_formula(rate)
        assert "1e6" not in _estimate_formula(rate)


def test_every_rate_is_dated_so_it_can_go_stale_visibly() -> None:
    assert CHECKED_AT.year == 2026
    assert CHECKED_AT.tzinfo is not None
