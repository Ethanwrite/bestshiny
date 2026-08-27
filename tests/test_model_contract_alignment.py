"""Every model in the registry must agree with itself, four ways.

For each of the twenty-five models there are four independent facts, and this
audit found each of them wrong somewhere:

    registry ID        the string sent to the provider — `grok-video`,
                       `veo-3.1-quality`, `wan-3.0` and `seedance-2.5` were all
                       names no provider publishes
    pricing profile    a row in `model_pricing_profiles` for that exact string
    pricing_status     the report shown to an operator
    live quote         what `estimate()` actually does when money is at stake

They are computed from different places, so nothing forced them to agree, and
they did not: Wan reported VERIFIED and raised `PricingUnverified` at the till,
Doubao reported UNVERIFIED for a model that was priced.

This asserts the alignment on a container built from the shipped defaults, so a
model added without a price, or priced under a string the registry does not
hold, fails here rather than at a provider's invoice.

`model_pricing_profiles` is populated by migrations, and a per-test database is
built from ORM metadata instead — so the rows are seeded here from the same
table `0051` writes, via the loader the audit script uses. Without that every
assertion below would pass vacuously on an empty price table.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from cost_core import CreditPricingEngine, PricingUnverified
from model_registry_core import ModelCapabilityRegistry
from platform_database import Database
from platform_shared import Settings
from production_domain.models import ModelDefinition, ModelPricingProfile, new_id
from sqlalchemy import select
from video_platform_api.container import build_container

from scripts.seed_token_pricing import CHECKED_AT, RATES, USD_PER_CNY, formula_for

EXPECTED_MODEL_COUNT = 25

# The five that carry no price, and why. Each is a deliberate refusal, not a gap
# waiting to be filled with a guess.
UNPRICEABLE = {
    "flow-veo-3.1-internal": "Google publishes no third-party Flow API and no per-call price",
    "flow-narwhal-image-internal": "NARWHAL is not a Google-published identifier",
    "grok-video-official": "xAI rate not confirmed for this account; provider has no transport",
    "veo-3.1-quality-official": "Google rate not confirmed for this account; no transport",
    "wan-3.0-official": "Wan 3.0 is invitation-only Beta; no access, no confirmed rate",
}

# Priced by migrations 0045-0048, which this fixture does not replay: a per-test
# database is built from ORM metadata, and only 0051's table is importable as a
# single object. They are listed so the assertion below stays exact — a new model
# arriving unpriced still fails, rather than hiding in a subset check. The full
# twenty-five-way alignment is verified against a real migrated PostgreSQL
# database, where every migration in the chain has actually run.
PRICED_BY_EARLIER_MIGRATIONS = {
    "gpt-image-2-openrouter",
    "seedream-5.0-ark",
    "seedance-2.5-official",
    "grok-imagine-video-openrouter",
    "kling-3-pro-openrouter",
    "kling-3-standard-openrouter",
    "veo-3.1-openrouter",
    "veo-3.1-fast-openrouter",
    "veo-3.1-lite-openrouter",
}

# Strings that were once in the registry and are not model IDs at any provider.
RETIRED_IDS = {"grok-video", "veo-3.1-quality", "wan-3.0", "seedance-2.5", "seedream-5-0"}


@pytest.fixture
def aligned(tmp_path, database_url):  # type: ignore[no-untyped-def]
    root = tmp_path
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        storage_root=root / "media",
        public_base_url="http://testserver",
        auth_required=False,
        platform_api_key="test-platform-key",
        deployment_environment="test",
        # Declared the way a real deployment declares them, so the registry rows
        # end up holding the IDs a deployment actually sends.
        ark_api_key="test-ark-key",
        doubao_model_id="doubao-seed-2-0-lite-260428",
        wan_api_key="test-wan-key",
        wan2_7_t2v_model_id="wan2.7-t2v-2026-06-12",
        runapi_api_key="test-runapi-key",
        runapi_base_url="https://runapi.ai",
        runapi_model_id="gpt-5.6-luna",
        deepseek_api_key="test-deepseek-key",
        deepseek_model_id="deepseek-v4-flash",
    )
    database = Database(settings.database_url)
    database.create_all_and_stamp()
    now = CHECKED_AT
    with database.session() as session:
        for provider, model, direction, price, currency, unit, resolution, source, note in RATES:
            session.add(
                ModelPricingProfile(
                    id=new_id(),
                    provider=provider,
                    provider_model_id=model,
                    input_mode=direction,
                    resolution=resolution,
                    currency=currency,
                    billing_unit=unit,
                    unit_price=Decimal(price),
                    estimate_unit=unit,
                    estimate_unit_price=Decimal(price),
                    usd_per_currency=Decimal("1") if currency == "USD" else USD_PER_CNY,
                    estimate_formula=formula_for(unit, direction, "estimate_unit_price"),
                    settlement_formula=formula_for(unit, direction, "unit_price"),
                    effective_from=now,
                    source_url=source,
                    source_checked_at=now,
                    notes=note,
                )
            )
    built = build_container(settings)
    try:
        yield built
    finally:
        built.database.engine.dispose()


def _definitions(container) -> list[ModelDefinition]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        return list(session.scalars(select(ModelDefinition)).all())


def _has_profile(container, definition: ModelDefinition) -> bool:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        return (
            session.scalar(
                select(ModelPricingProfile).where(
                    ModelPricingProfile.provider == definition.provider,
                    ModelPricingProfile.provider_model_id == definition.provider_model_id,
                )
            )
            is not None
        )


def _quote_succeeds(container, definition: ModelDefinition) -> bool | None:  # type: ignore[no-untyped-def]
    """None for a model the credit engine does not quote (chat and embeddings)."""

    if definition.modality not in {"image", "video"}:
        return None
    engine = CreditPricingEngine(
        ModelCapabilityRegistry(database=container.database),
        database=container.database,
        require_verified_pricing=True,
    )
    request: dict[str, object] = {
        "provider": definition.provider,
        "model": definition.provider_model_id,
        "media_type": "video" if definition.modality == "video" else "image",
        "resolution": "720p",
    }
    if definition.modality == "video":
        request["duration"] = 4.0
    try:
        engine.estimate(**request)  # type: ignore[arg-type]
    except PricingUnverified:
        return False
    return True


def test_the_registry_holds_every_model_this_audit_covered(aligned) -> None:  # type: ignore[no-untyped-def]
    assert len(_definitions(aligned)) == EXPECTED_MODEL_COUNT


def test_no_model_sends_a_string_its_provider_does_not_publish(aligned) -> None:  # type: ignore[no-untyped-def]
    """A logical name must never reach a provider as an API model ID — §20."""

    offenders = [
        (definition.logical_name, definition.provider_model_id)
        for definition in _definitions(aligned)
        if definition.provider_model_id in RETIRED_IDS
    ]
    assert not offenders


def test_pricing_status_agrees_with_whether_a_profile_exists(aligned) -> None:  # type: ignore[no-untyped-def]
    """The report is derived from the price table; it must not describe a stale row."""

    disagreements = [
        (definition.logical_name, definition.provider_model_id, definition.pricing_status)
        for definition in _definitions(aligned)
        if (definition.pricing_status == "VERIFIED") != _has_profile(aligned, definition)
    ]
    assert not disagreements


def test_pricing_status_agrees_with_what_the_till_does(aligned) -> None:  # type: ignore[no-untyped-def]
    """Wan reported VERIFIED and raised PricingUnverified. Never again."""

    disagreements = [
        (definition.logical_name, definition.pricing_status, quote)
        for definition in _definitions(aligned)
        for quote in [_quote_succeeds(aligned, definition)]
        if quote is not None and (definition.pricing_status == "VERIFIED") != quote
    ]
    assert not disagreements


def test_exactly_the_unpriceable_models_are_refused(aligned) -> None:  # type: ignore[no-untyped-def]
    """Neither more nor fewer: a shrinking list means someone guessed a price.

    `PRICED_BY_EARLIER_MIGRATIONS` is on the right-hand side because this fixture
    seeds 0051's rows only. Those models are priced in any real deployment.
    """

    unverified = {
        definition.logical_name
        for definition in _definitions(aligned)
        if definition.pricing_status == "UNVERIFIED"
    }
    assert unverified == set(UNPRICEABLE) | PRICED_BY_EARLIER_MIGRATIONS


def test_every_priced_model_is_priced_under_the_string_it_actually_sends(aligned) -> None:  # type: ignore[no-untyped-def]
    """The Wan defect in general form: a price keyed on a string nobody sends."""

    for definition in _definitions(aligned):
        if definition.pricing_status != "VERIFIED":
            continue
        assert _has_profile(aligned, definition), (
            f"{definition.logical_name} is VERIFIED but no profile is keyed on "
            f"{definition.provider}:{definition.provider_model_id}"
        )
