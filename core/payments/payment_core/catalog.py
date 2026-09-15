from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

# Bumping this never rewrites an order that already froze the old value: the
# snapshot is copied onto the order row at checkout, and settlement reads the
# row, not this module. Change the packages and the version together.
PRICING_VERSION = "2026-09-15.v3"
XUNHUPAY_PRICING_VERSION = "2026-09-15.cny.v3"

# The packs are priced in CNY first: 18 / 48 / 158 (the operator's prices,
# 2026-09-15). The USDC price is the same amount at the platform's CNY snapshot
# (about 6.78 to the dollar) rounded to a whole dollar, and the credits are that
# dollar price at the rate credits are spent (1 credit = USD 0.01), so a pack
# buys what it costs. A SKU is an identifier frozen onto every order row and
# keyed by the web copy, so it does not follow a reprice: `starter_20` sells
# for 3 USDC.


@dataclass(frozen=True)
class PaymentPackage:
    """One server-owned price/credit tuple that may be snapshotted into an order."""

    sku: str
    amount: Decimal
    credits: int
    recommended: bool = False
    currency: str = "USDC"
    pricing_version: str = PRICING_VERSION
    provider: str = "DEPAY"

    def __post_init__(self) -> None:
        raw = self.amount * Decimal(1_000_000)
        if (
            not self.sku
            or not self.pricing_version
            or not self.amount.is_finite()
            or self.amount <= 0
            or raw != raw.to_integral_value()
            or self.credits < 1
            or (self.currency, self.provider) not in {("USDC", "DEPAY"), ("CNY", "XUNHUPAY")}
            or (
                self.currency == "CNY"
                and self.amount * Decimal(100) != (self.amount * Decimal(100)).to_integral_value()
            )
        ):
            raise ValueError("invalid payment package")

    @property
    def raw_amount_microunits(self) -> int:
        return int(self.amount * Decimal(1_000_000))

    def as_dict(self) -> dict[str, object]:
        return {
            "sku": self.sku,
            "amount": f"{self.amount:.2f}",
            "currency": self.currency,
            "credits": self.credits,
            "pricing_version": self.pricing_version,
            "provider": self.provider,
            "recommended": self.recommended,
        }

    def as_public_dict(self) -> dict[str, object]:
        """What a buyer is shown: the price, the credits, and which is picked.

        `pricing_version` and `provider` are bookkeeping the browser has no use
        for, and neither is anything a client is allowed to send back.
        """
        return {
            "sku": self.sku,
            "amount": f"{self.amount:.2f}",
            "currency": self.currency,
            "credits": self.credits,
            "recommended": self.recommended,
        }


PAYMENT_PACKAGES: Mapping[str, PaymentPackage] = MappingProxyType(
    {
        "starter_20": PaymentPackage(
            sku="starter_20",
            amount=Decimal("3"),
            credits=300,
        ),
        "creator_50": PaymentPackage(
            sku="creator_50",
            amount=Decimal("7"),
            credits=700,
            recommended=True,
        ),
        "pro_100": PaymentPackage(
            sku="pro_100",
            amount=Decimal("23"),
            credits=2_300,
        ),
    }
)


XUNHUPAY_PACKAGES: Mapping[str, PaymentPackage] = MappingProxyType(
    {
        "starter_20": PaymentPackage(
            sku="starter_20",
            amount=Decimal("18"),
            credits=300,
            currency="CNY",
            provider="XUNHUPAY",
            pricing_version=XUNHUPAY_PRICING_VERSION,
        ),
        "creator_50": PaymentPackage(
            sku="creator_50",
            amount=Decimal("48"),
            credits=700,
            currency="CNY",
            provider="XUNHUPAY",
            pricing_version=XUNHUPAY_PRICING_VERSION,
            recommended=True,
        ),
        "pro_100": PaymentPackage(
            sku="pro_100",
            amount=Decimal("158"),
            credits=2_300,
            currency="CNY",
            provider="XUNHUPAY",
            pricing_version=XUNHUPAY_PRICING_VERSION,
        ),
    }
)
