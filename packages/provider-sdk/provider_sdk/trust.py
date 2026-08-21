from __future__ import annotations

from enum import StrEnum


class ProviderTrustLevel(StrEnum):
    """Operational trust assigned to a provider transport.

    The string values are persistence-safe identifiers.  Never compare them
    lexically; use :func:`provider_can_handle` so policy order stays explicit.
    """

    CANONICAL = "CANONICAL"
    PRODUCTION = "PRODUCTION"
    STANDARD = "STANDARD"
    EDGE = "EDGE"
    TEST_ONLY = "TEST_ONLY"


class AssetCriticality(StrEnum):
    """Business criticality of the requested output or operation."""

    CANONICAL = "CANONICAL"
    HERO = "HERO"
    IMPORTANT = "IMPORTANT"
    STANDARD = "STANDARD"
    EDGE = "EDGE"
    TEMPORARY = "TEMPORARY"


class ProviderTrustViolation(ValueError):
    """Raised when a provider is below the hard trust floor for a request."""


_TRUST_RANK: dict[ProviderTrustLevel, int] = {
    ProviderTrustLevel.TEST_ONLY: 0,
    ProviderTrustLevel.EDGE: 1,
    ProviderTrustLevel.STANDARD: 2,
    ProviderTrustLevel.PRODUCTION: 3,
    ProviderTrustLevel.CANONICAL: 4,
}

_REQUIRED_TRUST: dict[AssetCriticality, ProviderTrustLevel] = {
    # Production providers may create canonical candidates, but promotion into
    # canonical state remains a separate audited product operation.
    AssetCriticality.CANONICAL: ProviderTrustLevel.PRODUCTION,
    AssetCriticality.HERO: ProviderTrustLevel.PRODUCTION,
    AssetCriticality.IMPORTANT: ProviderTrustLevel.PRODUCTION,
    AssetCriticality.STANDARD: ProviderTrustLevel.STANDARD,
    AssetCriticality.EDGE: ProviderTrustLevel.EDGE,
    AssetCriticality.TEMPORARY: ProviderTrustLevel.TEST_ONLY,
}


def required_trust_for_asset(
    criticality: AssetCriticality | str,
) -> ProviderTrustLevel:
    """Return the minimum provider trust required by an asset criticality."""

    return _REQUIRED_TRUST[AssetCriticality(criticality)]


def provider_can_handle(
    provider_trust: ProviderTrustLevel | str,
    criticality: AssetCriticality | str,
) -> bool:
    """Apply the hard trust floor without importing persistence code."""

    actual = ProviderTrustLevel(provider_trust)
    required = required_trust_for_asset(criticality)
    return _TRUST_RANK[actual] >= _TRUST_RANK[required]


def assert_provider_can_handle(
    provider_trust: ProviderTrustLevel | str,
    criticality: AssetCriticality | str,
) -> None:
    """Fail closed when a provider cannot process the requested criticality."""

    actual = ProviderTrustLevel(provider_trust)
    requested = AssetCriticality(criticality)
    if not provider_can_handle(actual, requested):
        required = required_trust_for_asset(requested)
        raise ProviderTrustViolation(
            f"provider trust {actual.value} cannot handle {requested.value}; "
            f"minimum trust is {required.value}"
        )
