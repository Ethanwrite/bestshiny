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
# Multimodal input is not always billed per token. Voyage bills two axes at
# once (migration 0071): text per 1M tokens and image input per 1B pixels. A
# settlement that reads only the token row prices a call that was mostly
# pixels at nearly nothing, which is how an embedding cost silently vanishes.
PIXEL_BILLING_UNIT = "1B_pixels"
SETTLEABLE_BILLING_UNITS = (TOKEN_BILLING_UNIT, PIXEL_BILLING_UNIT)
_MILLION = Decimal(1_000_000)
_BILLION = Decimal(1_000_000_000)
_UNIT_DIVISORS = {TOKEN_BILLING_UNIT: _MILLION, PIXEL_BILLING_UNIT: _BILLION}
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
    """USD-per-unit list rates for one provider model, from dated rows.

    Per-token and per-pixel rates live on one object because a single
    multimodal call is billed on both at once, and a settlement is only
    traceable if it can name every row it used.
    """

    input_usd_per_token: Decimal
    output_usd_per_token: Decimal | None
    cached_input_usd_per_token: Decimal | None
    #: USD for one image pixel — the ``1B_pixels`` row divided by 1e9.
    image_usd_per_pixel: Decimal | None
    #: USD for one video pixel. No shipped migration prices one; a provider
    #: that starts billing video pixels gets a dated row, not a code change.
    video_usd_per_pixel: Decimal | None
    profile_ids: tuple[str, ...]


@dataclass(frozen=True)
class TokenSettlement:
    cost_usd: Decimal
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    detail: str
    #: The provider's own reported pixel counts, carried out so the execution
    #: record and the budget evidence can show what was actually billed.
    image_pixels: int = 0
    video_pixels: int = 0


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
                        ModelPricingProfile.billing_unit.in_(SETTLEABLE_BILLING_UNITS),
                        ModelPricingProfile.effective_from <= now,
                    )
                )
            )
        live = [row for row in rows if row.effective_until is None or _aware(row.effective_until) > now]

        def current(direction: str, billing_unit: str) -> ModelPricingProfile | None:
            # The unit is part of the identity of a rate, not a detail of it:
            # an `image_input` row is per-pixel and an `input_tokens` row is
            # per-token, and reading either through the other's divisor would
            # be wrong by a factor of a thousand.
            candidates = [
                row for row in live if row.input_mode == direction and row.billing_unit == billing_unit
            ]
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

        def usd_per_unit(row: ModelPricingProfile | None) -> Decimal | None:
            if row is None:
                return None
            divisor = _UNIT_DIVISORS.get(row.billing_unit)
            if divisor is None:  # pragma: no cover - the query filters on the units
                return None
            return Decimal(row.unit_price) * Decimal(row.usd_per_currency) / divisor

        input_row = current("input_tokens", TOKEN_BILLING_UNIT)
        input_rate = usd_per_unit(input_row)
        if input_row is None or input_rate is None:
            return None
        output_row = current("output_tokens", TOKEN_BILLING_UNIT)
        cached_row = current("cached_input_tokens", TOKEN_BILLING_UNIT)
        image_row = current("image_input", PIXEL_BILLING_UNIT)
        video_row = current("video_input", PIXEL_BILLING_UNIT)
        return TokenRates(
            input_usd_per_token=input_rate,
            output_usd_per_token=usd_per_unit(output_row),
            cached_input_usd_per_token=usd_per_unit(cached_row),
            image_usd_per_pixel=usd_per_unit(image_row),
            video_usd_per_pixel=usd_per_unit(video_row),
            # Every row that priced the call, so a figure stays traceable to
            # both halves of a text-plus-image bill.
            profile_ids=tuple(
                row.id
                for row in (input_row, output_row, cached_row, image_row, video_row)
                if row is not None
            ),
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

        Prices every axis the provider reported. A Voyage multimodal embedding
        returns ``text_tokens`` with ``image_pixels`` and ``video_pixels``
        beside them; before those were read, a live Voyage call settled
        nothing and its authorization closed at the quote ceiling.

        Returns None — leaving the usage UNCERTAIN for an operator — whenever
        the counts or the rates cannot support an honest figure: no countable
        usage at all, no pricing row, output tokens with no output rate, or
        pixels reported with no pixel rate. An unpriced axis is never treated
        as free. Cached input tokens without a cached rate are billed at the
        full input rate; overstating a settlement is safe where understating
        is not.
        """

        text_tokens = _count(usage, "prompt_tokens", "input_tokens", "text_tokens")
        image_pixels = _count(usage, "image_pixels")
        video_pixels = _count(usage, "video_pixels")
        if text_tokens is None and image_pixels is None and video_pixels is None:
            return None
        input_tokens = text_tokens or 0
        output_tokens = _count(usage, "completion_tokens", "output_tokens") or 0
        details = usage.get("prompt_tokens_details")
        cached = _count(details, "cached_tokens") or 0 if isinstance(details, Mapping) else 0
        cached = min(cached, input_tokens)
        rates = self.rates_for(provider, model)
        if rates is None:
            return None
        if output_tokens > 0 and rates.output_usd_per_token is None:
            return None
        # A *nonzero* pixel count with no pixel row cannot be priced, and a
        # partial figure that silently drops it would be read as the whole
        # bill. Zero pixels cost zero at any rate, so a text-only call whose
        # usage block still carries `"image_pixels": 0` settles normally.
        if image_pixels and rates.image_usd_per_pixel is None:
            return None
        if video_pixels and rates.video_usd_per_pixel is None:
            return None
        cached_rate = rates.cached_input_usd_per_token
        if cached_rate is None:
            cached_rate = rates.input_usd_per_token
        cost = (
            Decimal(input_tokens - cached) * rates.input_usd_per_token
            + Decimal(cached) * cached_rate
            + Decimal(output_tokens) * (rates.output_usd_per_token or Decimal(0))
            + Decimal(image_pixels or 0) * (rates.image_usd_per_pixel or Decimal(0))
            + Decimal(video_pixels or 0) * (rates.video_usd_per_pixel or Decimal(0))
        ).quantize(_MONEY)
        return TokenSettlement(
            cost_usd=cost,
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            output_tokens=output_tokens,
            image_pixels=image_pixels or 0,
            video_pixels=video_pixels or 0,
            detail=_settlement_detail(
                provider,
                model,
                input_tokens=input_tokens,
                cached=cached,
                output_tokens=output_tokens,
                image_pixels=image_pixels,
                video_pixels=video_pixels,
            ),
        )


def _settlement_detail(
    provider: str,
    model: str,
    *,
    input_tokens: int,
    cached: int,
    output_tokens: int,
    image_pixels: int | None,
    video_pixels: int | None,
) -> str:
    """One line an operator can check a figure against, axis by axis.

    The token prefix is stable: a call that reported no pixels reads exactly
    as it did before pixels were priced at all.
    """

    detail = f"{input_tokens}in+{cached}cached+{output_tokens}out"
    units = TOKEN_BILLING_UNIT
    if image_pixels is not None or video_pixels is not None:
        detail += f"+{image_pixels or 0}ipx+{video_pixels or 0}vpx"
        if image_pixels or video_pixels:
            units = f"{TOKEN_BILLING_UNIT}+{PIXEL_BILLING_UNIT}"
    return f"{detail}@{provider}:{model}:{units}"


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
    "PIXEL_BILLING_UNIT",
    "SETTLEABLE_BILLING_UNITS",
    "TOKEN_BILLING_UNIT",
    "TokenCostEngine",
    "TokenRates",
    "TokenSettlement",
]
