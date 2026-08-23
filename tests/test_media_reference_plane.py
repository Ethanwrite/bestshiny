"""The media plane: what reaches a provider, and what never reaches this service.

Two rules are enforced here, and both are about keeping the API tier out of the
way of large files:

1. **The original is never re-encoded.** A provider's upload cap is a fact about
   that provider. A 7680x4320 character plate stays 7680x4320; the provider gets
   a derived copy sized for its limits. Character faces, product labels and
   fabric detail only ever exist at the resolution they arrived at.

2. **The provider fetches from object storage, not from us.** A reference URL is
   a short-lived credential issued by the storage backend. If the backend cannot
   issue one, generation fails closed rather than falling back to streaming the
   object through this process — a handful of concurrent 4K reference edits
   would otherwise turn the control plane into an image CDN.
"""

from __future__ import annotations

import io

import pytest
from media_service.renditions import (
    MINIMUM_REFERENCE_PIXELS,
    RenditionDerivationFailed,
    RenditionResolver,
)
from PIL import Image, ImageDraw
from production_domain.models import MediaAsset, MediaRendition, MediaRenditionKind
from provider_sdk import ProviderReferenceConstraints
from sqlalchemy import select


def _photo(width: int, height: int) -> bytes:
    """Photograph-like content: smooth gradients plus structure.

    Deliberately not random noise. Noise is the worst case for every lossy
    encoder and would make these tests measure JPEG's behaviour on data no
    camera produces, rather than the resolver's behaviour on plates.
    """

    image = Image.new("RGB", (width, height))
    image.putdata(
        [
            (
                (x * 255) // max(1, width - 1),
                (y * 255) // max(1, height - 1),
                160 if (x // 32 + y // 32) % 2 else 90,
            )
            for y in range(height)
            for x in range(width)
        ]
    )
    draw = ImageDraw.Draw(image)
    draw.ellipse((width // 4, height // 4, width * 3 // 4, height * 3 // 4), fill=(220, 60, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _register(container, project_id: str, payload: bytes, name: str = "plate.png"):  # type: ignore[no-untyped-def]
    return container.media.register(
        project_id,
        "CHARACTER_REFERENCE",
        io.BytesIO(payload),
        filename=name,
        mime_type="image/png",
    )[0]


# --- 1. The original survives ------------------------------------------------


def test_a_constrained_provider_never_alters_the_stored_original(container, project) -> None:  # type: ignore[no-untyped-def]
    original_bytes = _photo(1200, 900)
    asset = _register(container, project.id, original_bytes)
    resolver = RenditionResolver(container.storage)

    with container.database.session() as session:
        resolved = resolver.resolve(
            session,
            session.get(MediaAsset, asset.id),
            ProviderReferenceConstraints(max_pixels=256 * 256, max_bytes=64 * 1024),
        )

    assert resolved.derived is True
    assert resolved.kind == MediaRenditionKind.PROVIDER_REFERENCE.value
    assert resolved.storage_key != asset.storage_key
    # The asset row and the bytes behind it are untouched.
    with container.database.session() as session:
        stored = session.get(MediaAsset, asset.id)
        assert (stored.width, stored.height) == (1200, 900)
        assert stored.size_bytes == len(original_bytes)
    with container.storage.open(asset.storage_key, "rb") as stream:
        assert stream.read() == original_bytes


def test_an_original_that_already_fits_is_sent_as_is(container, project) -> None:  # type: ignore[no-untyped-def]
    asset = _register(container, project.id, _photo(64, 64))
    resolver = RenditionResolver(container.storage)

    with container.database.session() as session:
        resolved = resolver.resolve(
            session,
            session.get(MediaAsset, asset.id),
            ProviderReferenceConstraints(max_pixels=4096 * 4096, max_bytes=32 * 1024 * 1024),
        )
        assert session.scalars(select(MediaRendition)).all() == []

    assert resolved.is_original is True
    assert resolved.derived is False
    assert resolved.storage_key == asset.storage_key


def test_an_unbounded_consumer_is_not_treated_as_an_unlimited_one(container, project) -> None:  # type: ignore[no-untyped-def]
    """No declared limits means limits we have not established, so: no guessing."""

    asset = _register(container, project.id, _photo(900, 700))
    resolver = RenditionResolver(container.storage)

    with container.database.session() as session:
        resolved = resolver.resolve(
            session, session.get(MediaAsset, asset.id), ProviderReferenceConstraints()
        )
        assert session.scalars(select(MediaRendition)).all() == []

    assert resolved.is_original is True


# --- 2. Derived copies are bounded, cached and constraint-keyed ---------------


def test_a_derived_reference_respects_both_pixel_and_byte_bounds(container, project) -> None:  # type: ignore[no-untyped-def]
    asset = _register(container, project.id, _photo(1600, 1200))
    resolver = RenditionResolver(container.storage)
    constraints = ProviderReferenceConstraints(max_pixels=640 * 480, max_bytes=48 * 1024)

    with container.database.session() as session:
        resolved = resolver.resolve(session, session.get(MediaAsset, asset.id), constraints)

    assert resolved.width is not None and resolved.height is not None
    assert resolved.width * resolved.height <= 640 * 480
    assert resolved.size_bytes <= 48 * 1024
    assert resolved.mime_type == "image/jpeg"
    assert constraints.accepts(
        mime_type=resolved.mime_type,
        pixels=resolved.width * resolved.height,
        size_bytes=resolved.size_bytes,
    )


def test_a_derived_reference_is_reused_rather_than_rebuilt(container, project) -> None:  # type: ignore[no-untyped-def]
    asset = _register(container, project.id, _photo(1000, 800))
    resolver = RenditionResolver(container.storage)
    constraints = ProviderReferenceConstraints(max_pixels=320 * 240, max_bytes=64 * 1024)

    with container.database.session() as session:
        first = resolver.resolve(session, session.get(MediaAsset, asset.id), constraints)
    with container.database.session() as session:
        second = resolver.resolve(session, session.get(MediaAsset, asset.id), constraints)
        assert len(session.scalars(select(MediaRendition)).all()) == 1

    assert first.storage_key == second.storage_key


def test_changed_constraints_produce_a_new_rendition_not_a_stale_reuse(container, project) -> None:  # type: ignore[no-untyped-def]
    """A provider that lowers its cap must not keep receiving the old copy."""

    asset = _register(container, project.id, _photo(1000, 800))
    resolver = RenditionResolver(container.storage)

    with container.database.session() as session:
        generous = resolver.resolve(
            session,
            session.get(MediaAsset, asset.id),
            ProviderReferenceConstraints(max_pixels=640 * 480, max_bytes=256 * 1024),
        )
    with container.database.session() as session:
        strict = resolver.resolve(
            session,
            session.get(MediaAsset, asset.id),
            ProviderReferenceConstraints(max_pixels=200 * 150, max_bytes=32 * 1024),
        )
        assert len(session.scalars(select(MediaRendition)).all()) == 2

    assert generous.storage_key != strict.storage_key
    assert strict.size_bytes <= 32 * 1024


def test_a_format_only_constraint_re_encodes_without_downscaling(container, project) -> None:  # type: ignore[no-untyped-def]
    asset = _register(container, project.id, _photo(300, 200))
    resolver = RenditionResolver(container.storage)

    with container.database.session() as session:
        resolved = resolver.resolve(
            session,
            session.get(MediaAsset, asset.id),
            ProviderReferenceConstraints(
                max_bytes=4 * 1024 * 1024,
                accepted_mime_types=frozenset({"image/webp"}),
                preferred_mime_type="image/webp",
            ),
        )

    assert resolved.mime_type == "image/webp"
    assert (resolved.width, resolved.height) == (300, 200)


def test_an_impossible_bound_fails_rather_than_shipping_an_unusable_reference(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
) -> None:
    """Below a useful resolution a 'reference' no longer carries identity."""

    asset = _register(container, project.id, _photo(1200, 900))
    resolver = RenditionResolver(container.storage)

    with container.database.session() as session, pytest.raises(RenditionDerivationFailed):
        resolver.resolve(
            session,
            session.get(MediaAsset, asset.id),
            ProviderReferenceConstraints(max_bytes=64),
        )
    assert MINIMUM_REFERENCE_PIXELS == 256 * 256


def test_video_is_reported_as_unadaptable_instead_of_being_re_encoded(container, project) -> None:  # type: ignore[no-untyped-def]
    asset = container.media.register(
        project.id,
        "VIDEO",
        io.BytesIO(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256),
        filename="clip.mp4",
        mime_type="video/mp4",
    )[0]
    resolver = RenditionResolver(container.storage)

    with container.database.session() as session, pytest.raises(RenditionDerivationFailed):
        resolver.resolve(
            session,
            session.get(MediaAsset, asset.id),
            ProviderReferenceConstraints(max_bytes=16),
        )


# --- 3. The provider fetches from storage, not from this service -------------


def test_a_reference_url_is_short_lived_and_is_not_the_stored_public_url(container, project) -> None:  # type: ignore[no-untyped-def]
    asset = _register(container, project.id, _photo(100, 100))

    reference = container.media.reference_url(
        asset.id,
        project_id=project.id,
        provider="openrouter",
        require_https=False,
        constraints=ProviderReferenceConstraints(),
    )

    assert reference != asset.public_url
    # `public_url` points at this service's authenticated route: an external
    # fetcher gets a 403 there, and if it did not, we would be the CDN.
    assert "/v1/storage/" not in reference
    assert "expires=" in reference and "signature=" in reference


def test_reference_resolution_fails_closed_when_storage_cannot_issue_a_url(container, project) -> None:  # type: ignore[no-untyped-def]
    from media_service import ProviderReferenceUrlUnavailable

    asset = _register(container, project.id, _photo(100, 100))
    container.storage.reference_signing_key = ""

    with pytest.raises(ProviderReferenceUrlUnavailable, match="must not proxy"):
        container.media.reference_url(
            asset.id,
            project_id=project.id,
            provider="openrouter",
            require_https=False,
        )


def test_a_signed_reference_is_rejected_after_it_expires(container) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import parse_qs, urlsplit

    from platform_shared import signed_local_reference_url, verify_local_reference_signature

    url = signed_local_reference_url(
        "https://media.example", "ab/abc.png", signing_key="k", expires_in=900
    )
    query = parse_qs(urlsplit(url).query)
    assert verify_local_reference_signature(
        "ab/abc.png",
        expires=query["expires"][0],
        signature=query["signature"][0],
        signing_key="k",
    )
    # A different object, a different key, and a lapsed window are all refused.
    assert not verify_local_reference_signature(
        "ab/other.png",
        expires=query["expires"][0],
        signature=query["signature"][0],
        signing_key="k",
    )
    assert not verify_local_reference_signature(
        "ab/abc.png",
        expires=query["expires"][0],
        signature=query["signature"][0],
        signing_key="different",
    )
    assert not verify_local_reference_signature(
        "ab/abc.png", expires=1, signature=query["signature"][0], signing_key="k"
    )


def test_the_gateway_hands_the_provider_the_encoding_its_limits_allow(container, project) -> None:  # type: ignore[no-untyped-def]
    """End to end: a plate above OpenRouter's declared cap arrives downscaled."""

    from openrouter_provider import OpenRouterProvider

    constraints = OpenRouterProvider.reference_constraints
    assert constraints.bounded, "the shipped provider must declare real bounds"

    asset = _register(container, project.id, _photo(1400, 1100))
    with container.database.session() as session:
        stored = session.get(MediaAsset, asset.id)
        stored.width, stored.height = 7680, 4320
        stored.size_bytes = 38 * 1024 * 1024

    resolver = RenditionResolver(container.storage)
    with container.database.session() as session:
        resolved = resolver.resolve(session, session.get(MediaAsset, asset.id), constraints)

    assert resolved.derived is True
    assert resolved.width is not None and resolved.height is not None
    assert resolved.width * resolved.height <= constraints.max_pixels
    assert resolved.size_bytes <= constraints.max_bytes
