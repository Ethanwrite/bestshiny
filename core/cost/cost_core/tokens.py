from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from platform_database import Database
from production_domain.models import ModelPricingProfile, utcnow
from sqlalchemy import select

# The unit every per-token pricing row is stored in (migration 0051): the
# provider's own published per-1M-token rate, in the provider's own currency.
TOKEN_BILLING_UNIT = "1M_tokens"
_MILLION = Decimal(1_000_000)
_MONEY = Decimal("0.000001")

# An unquoted live reservation must hold *something* strictly positive: a zero
# hold would let one permit fan out into unlimited concurrent calls, which is
# the exact hazard the whole-budget hold existed to prevent.
_MINIMUM_HOLD_USD = Decimal("0.000100")

# Holds are planning figures, not bills. The character-based input bound and a
# fixed output budget can both undershoot a pathological call, so the whole
# estimate carries a margin; settlement replaces it with the exact figure.
_ESTIMATE_MARGIN = Decimal(2)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class TokenRates:
    """USD-per-token list rates for one provider model, from dated rows."""

    input_usd_per_token: Decimal
    output_usd_per_token: Decimal | None
    cached_input_usd_per_token: Decimal | None
    profile_ids: tuple[str, ...]


@dataclass(frozen=True)
class TokenSettlement:
    cost_usd: Decimal
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    detail: str


class TokenCostEngine:
    """Price chat/embedding calls from token counts at canonical list rates.

    Providers that bill per token report ``usage`` token counts, not a cost
    figure — Ark's chat responses carry ``prompt_tokens``/``completion_tokens``
    and nothing else. Until 2026-08-30 that meant a live chat canary usage
    could never settle and its whole-budget hold never came back. This engine
    turns the recorded token counts into a USD figure using the same dated,
    sourced ``model_pricing_profiles`` rows every quote uses, so a settlement
    is traceable to a published rate (``cost_source=TOKENS_LIST``) rather than
    invented. It prices nothing it does not have a row for.
    """

    version = "token-cost-v1"

    def __init__(self, database: Database):
        self.database = database

    def rates_for(self, provider: str, model: str) -> TokenRates | None:
        now = utcnow()
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(ModelPricingProfile).where(
                        ModelPricingProfile.provider == provider,
                        ModelPricingProfile.provider_model_id == model,
                        ModelPricingProfile.billing_unit == TOKEN_BILLING_UNIT,
                        ModelPricingProfile.effective_from <= now,
                    )
                )
            )
        live = [row for row in rows if row.effective_until is None or _aware(row.effective_until) > now]

        def current(direction: str) -> ModelPricingProfile | None:
            candidates = [row for row in live if row.input_mode == direction]
            if not candidates:
                return None
            # Promotion before list, exactly as CreditPricingEngine selects: a
            # dated row is narrower than an open-ended one, later reissues win.
            candidates.sort(
                key=lambda row: (
                    row.effective_until is None,
                    -_aware(row.effective_from).timestamp(),
                )
            )
            return candidates[0]

        def usd_per_token(row: ModelPricingProfile | None) -> Decimal | None:
            if row is None:
                return None
            return Decimal(row.unit_price) * Decimal(row.usd_per_currency) / _MILLION

        input_row = current("input_tokens")
        input_rate = usd_per_token(input_row)
        if input_row is None or input_rate is None:
            return None
        output_row = current("output_tokens")
        cached_row = current("cached_input_tokens")
        return TokenRates(
            input_usd_per_token=input_rate,
            output_usd_per_token=usd_per_token(output_row),
            cached_input_usd_per_token=usd_per_token(cached_row),
            profile_ids=tuple(row.id for row in (input_row, output_row, cached_row) if row is not None),
        )

    def estimate_call(
        self,
        provider: str,
        model: str,
        *,
        input_characters: int,
        max_output_tokens: int,
    ) -> Decimal | None:
        """A bounded planning hold for one call, or None when it cannot price.

        The input bound is one token per character — an overestimate for
        English and an upper bound for CJK — because a hold may overshoot and
        must not undershoot. None (no row, or output demanded with no output
        rate) keeps the caller's conservative behavior: hold everything.
        """

        if input_characters < 0 or max_output_tokens < 0:
            raise ValueError("token estimate bounds cannot be negative")
        rates = self.rates_for(provider, model)
        if rates is None:
            return None
        if max_output_tokens > 0 and rates.output_usd_per_token is None:
            return None
        output_rate = rates.output_usd_per_token or Decimal(0)
        estimate = (
            Decimal(input_characters) * rates.input_usd_per_token
            + Decimal(max_output_tokens) * output_rate
        ) * _ESTIMATE_MARGIN
        return max(_MINIMUM_HOLD_USD, estimate.quantize(_MONEY))

    def settle_from_usage(
        self,
        provider: str,
        model: str,
        usage: Mapping[str, Any],
    ) -> TokenSettlement | None:
        """The exact list-priced cost of a finished call, from its usage block.

        Returns None — leaving the usage UNCERTAIN for an operator — whenever
        the counts or the rates cannot support an honest figure: no input
        count, no pricing row, or output tokens with no output rate. Cached
        input tokens without a cached rate are billed at the full input rate;
        overstating a settlement is safe where understating is not.
        """

        input_tokens = _count(usage, "prompt_tokens", "input_tokens")
        if input_tokens is None:
            return None
        output_tokens = _count(usage, "completion_tokens", "output_tokens") or 0
        details = usage.get("prompt_tokens_details")
        cached = _count(details, "cached_tokens") or 0 if isinstance(details, Mapping) else 0
        cached = min(cached, input_tokens)
        rates = self.rates_for(provider, model)
        if rates is None:
            return None
        if output_tokens > 0 and rates.output_usd_per_token is None:
            return None
        cached_rate = rates.cached_input_usd_per_token
        if cached_rate is None:
            cached_rate = rates.input_usd_per_token
        cost = (
            Decimal(input_tokens - cached) * rates.input_usd_per_token
            + Decimal(cached) * cached_rate
            + Decimal(output_tokens) * (rates.output_usd_per_token or Decimal(0))
        ).quantize(_MONEY)
        return TokenSettlement(
            cost_usd=cost,
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            output_tokens=output_tokens,
            detail=(
                f"{input_tokens}in+{cached}cached+{output_tokens}out"
                f"@{provider}:{model}:{TOKEN_BILLING_UNIT}"
            ),
        )


def _count(payload: Mapping[str, Any] | None, *keys: str) -> int | None:
    if payload is None:
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
    return None


__all__ = [
    "TOKEN_BILLING_UNIT",
    "TokenCostEngine",
    "TokenRates",
    "TokenSettlement",
]
