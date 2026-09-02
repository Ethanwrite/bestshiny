from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

# Bumping this never rewrites an order that already froze the old value: the
# snapshot is copied onto the order row at checkout, and settlement reads the
# row, not this module. Change the packages and the version together.
PRICING_VERSION = "2026-09-01.v2"
XUNHUPAY_PRICING_VERSION = "2026-09-02.cny.v2"


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
            amount=Decimal("20"),
            credits=1_800,
        ),
        "creator_50": PaymentPackage(
            sku="creator_50",
            amount=Decimal("50"),
            credits=6_000,
            recommended=True,
        ),
        "pro_100": PaymentPackage(
            sku="pro_100",
            amount=Decimal("100"),
            credits=11_000,
        ),
    }
)


XUNHUPAY_PACKAGES: Mapping[str, PaymentPackage] = MappingProxyType(
    {
        "starter_20": PaymentPackage(
            sku="starter_20",
            amount=Decimal("140"),
            credits=1_800,
            currency="CNY",
            provider="XUNHUPAY",
            pricing_version=XUNHUPAY_PRICING_VERSION,
        ),
        "creator_50": PaymentPackage(
            sku="creator_50",
            amount=Decimal("450"),
            credits=6_000,
            currency="CNY",
            provider="XUNHUPAY",
            pricing_version=XUNHUPAY_PRICING_VERSION,
            recommended=True,
        ),
        "pro_100": PaymentPackage(
            sku="pro_100",
            amount=Decimal("700"),
            credits=11_000,
            currency="CNY",
            provider="XUNHUPAY",
            pricing_version=XUNHUPAY_PRICING_VERSION,
        ),
    }
)
