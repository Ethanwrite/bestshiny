from __future__ import annotations

from collections.abc import Iterable

from production_domain.models import MediaAsset, ModelDefinition
from provider_sdk import AssetCriticality, provider_can_handle
from sqlalchemy import select
from sqlalchemy.orm import Session


class CanonicalMediaProvenanceViolation(ValueError):
    """A media asset's immutable origin is not trusted for canonical use."""


def assert_canonical_media_provenance(
    session: Session,
    media_assets: Iterable[MediaAsset],
) -> None:
    """Fail closed unless every provider-originated asset meets the canonical trust floor.

    ``MediaAsset.provider`` and ``provider_media_id`` describe where the bytes were
    originally generated or downloaded. Provider-specific upload identifiers live in
    ``MediaProviderBinding`` and therefore cannot raise an asset's origin trust.
    User uploads have neither origin field and are eligible for explicit canonical
    confirmation.
    """

    providers: set[str] = set()
    for media in media_assets:
        provider = (media.provider or "").strip()
        provider_media_id = (media.provider_media_id or "").strip()
        if not provider and not provider_media_id:
            continue
        if not provider:
            raise CanonicalMediaProvenanceViolation("generated media has incomplete provider provenance")
        providers.add(provider)

    for provider in providers:
        definitions = list(
            session.scalars(select(ModelDefinition).where(ModelDefinition.provider == provider))
        )
        if not definitions:
            raise CanonicalMediaProvenanceViolation(
                f"generated media provider has no trusted model definition: {provider}"
            )
        try:
            provider_is_eligible = all(
                provider_can_handle(
                    definition.provider_trust_level,
                    AssetCriticality.CANONICAL,
                )
                for definition in definitions
            )
        except ValueError as exc:
            raise CanonicalMediaProvenanceViolation(
                f"generated media provider has invalid trust provenance: {provider}"
            ) from exc
        if not provider_is_eligible:
            raise CanonicalMediaProvenanceViolation("low-trust generated media cannot become canonical")
