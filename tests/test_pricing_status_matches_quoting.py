"""`pricing_status` is a report, and a report that disagrees with the till is worse than none.

The column exists so an operator can see which models carry a sourced price. It
is derived at boot from `model_pricing_profiles` — the same table the quote reads
— so the two cannot drift. They drifted anyway, because the derivation ran in the
wrong place.

`reconcile_pricing_status()` was called immediately after `ensure_defaults()`,
before the block that rewrites `provider_model_id` for any model an operator has
declared. On a **fresh deployment** the Wan row was created holding the family key
`wan-2.7`, marked VERIFIED against the profile keyed on that string, and then moved
to a mode snapshot. The status stayed VERIFIED; the live quote raised
`PricingUnverified`. Every new install of this platform could not quote the one
model with real verified generations behind it, and the admin report said it was fine.

The existing deployment was unaffected only by accident: its Wan row predated the
rewrite, so `newly_created_models` never contained it.

Pinned here: that a price keyed on the ID a deployment actually configures is
found, and that no model anywhere reports a status its own quote contradicts.
"""

from __future__ import annotations

import pytest
from cost_core import CreditPricingEngine, PricingUnverified
from model_registry_core import ModelCapabilityRegistry
from production_domain.models import ModelDefinition
from sqlalchemy import select

QUOTABLE_MODALITIES = {"image", "video"}


def _definitions(container) -> list[ModelDefinition]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        return [
            definition
            for definition in session.scalars(select(ModelDefinition)).all()
            if definition.modality in QUOTABLE_MODALITIES
        ]


def _quote_succeeds(container, definition: ModelDefinition) -> bool:  # type: ignore[no-untyped-def]
    """Ask the engine the way live mode asks it: a placeholder is not an answer."""

    engine = CreditPricingEngine(
        ModelCapabilityRegistry(database=container.database),
        database=container.database,
        require_verified_pricing=True,
    )
    request = {
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


@pytest.fixture
def wan_declared_container(tmp_path, database_url):  # type: ignore[no-untyped-def]
    """A container built the way a real deployment configures Wan.

    The shared `container` fixture leaves `wan2_7_t2v_model_id` at its empty
    default, so the rewrite branch never runs and the defect is invisible. Every
    deployed `.env` sets it — that is the whole point of the setting — so the
    regression has to be reproduced with it declared.
    """

    from datetime import UTC, datetime
    from decimal import Decimal

    from platform_database import Database
    from platform_shared import Settings
    from production_domain.models import ModelPricingProfile, new_id
    from video_platform_api.container import build_container

    settings = Settings(
        _env_file=None,
        database_url=database_url,
        storage_root=tmp_path / "media",
        public_base_url="http://testserver",
        auth_required=False,
        platform_api_key="test-platform-key",
        deployment_environment="test",
        wan_api_key="test-wan-key",
        wan2_7_t2v_model_id="wan2.7-t2v-2026-06-12",
        wan2_7_i2v_model_id="wan2.7-i2v-2026-04-25",
        wan2_7_r2v_model_id="wan2.7-r2v-2026-06-12",
    )

    # A per-test database is built from ORM metadata, so the rows migrations
    # 0044-0048 seeded are not here. Without this the whole file passes
    # vacuously: nothing is priced, so nothing can disagree about being priced.
    # Seeded before the container is built, because the ordering under test is
    # exactly when `reconcile_pricing_status()` reads this table.
    seeded = datetime(2026, 8, 26, tzinfo=UTC)
    Database(settings.database_url).create_all_and_stamp()
    with Database(settings.database_url).session() as session:
        session.add(
            ModelPricingProfile(
                id=new_id(),
                provider="wan",
                provider_model_id="wan2.7-t2v-2026-06-12",
                input_mode="no_video_input",
                resolution="720p",
                currency="CNY",
                billing_unit="second",
                unit_price=Decimal("0.60000000"),
                estimate_unit="second",
                estimate_unit_price=Decimal("0.60000000"),
                usd_per_currency=Decimal("0.14743000"),
                effective_from=seeded,
                source_url="https://help.aliyun.com/zh/model-studio/model-pricing",
                source_checked_at=seeded,
            )
        )

    built = build_container(settings)
    try:
        yield built
    finally:
        built.database.engine.dispose()


def test_a_declared_wan_deployment_can_still_quote_it(wan_declared_container) -> None:  # type: ignore[no-untyped-def]
    """The failure a fresh install actually hit: VERIFIED, and refused at the till.

    The price has to be keyed on the ID the registry ends up holding. Wan's
    profile was keyed on the family key `wan-2.7`, and every deployment that
    declares `WAN2_7_T2V_MODEL_ID` — which is what the setting is for — moves
    the row to the mode snapshot and leaves the price unfindable.
    """

    with wan_declared_container.database.session() as session:
        wan = session.scalar(
            select(ModelDefinition).where(ModelDefinition.logical_name == "wan-2.7-official")
        )
    assert wan is not None
    assert wan.pricing_status == "VERIFIED"
    assert _quote_succeeds(wan_declared_container, wan)


@pytest.mark.parametrize("modality", sorted(QUOTABLE_MODALITIES))
def test_no_model_reports_a_status_its_own_quote_contradicts(container, modality: str) -> None:  # type: ignore[no-untyped-def]
    disagreements = [
        (definition.logical_name, definition.provider_model_id, definition.pricing_status)
        for definition in _definitions(container)
        if definition.modality == modality
        and (definition.pricing_status == "VERIFIED") != _quote_succeeds(container, definition)
    ]
    assert not disagreements, (
        "these models report a pricing_status their own live-mode quote does not support: "
        f"{disagreements}"
    )


def test_an_unpriced_model_is_refused_rather_than_estimated(container) -> None:  # type: ignore[no-untyped-def]
    """The fail-closed half, so the test above cannot pass by everything being VERIFIED."""

    unpriced = [
        definition
        for definition in _definitions(container)
        if definition.pricing_status == "UNVERIFIED"
    ]
    assert unpriced, "expected at least one deliberately unpriced model to prove the refusal"
    for definition in unpriced:
        assert not _quote_succeeds(container, definition)


def test_a_model_priced_under_its_configured_id_is_reported_verified(tmp_path, database_url) -> None:  # type: ignore[no-untyped-def]
    """The ordering half, on a model whose ID legitimately changes at startup.

    `doubao-free-reasoner` is seeded holding the placeholder
    `CONFIGURE_DOUBAO_MODEL_ID` and rewritten to whatever `DOUBAO_MODEL_ID`
    declares. A price is recorded against the *configured* ID, because that is
    the string Ark bills under.

    Derive the status before that rewrite and it is computed against the
    placeholder: UNVERIFIED, for a model that is priced. Wan showed the mirror
    image of the same bug — VERIFIED, for a model that is not. Both come from
    reading the row before the block that writes it.
    """

    from datetime import UTC, datetime
    from decimal import Decimal

    from platform_database import Database
    from platform_shared import Settings
    from production_domain.models import ModelPricingProfile, new_id
    from video_platform_api.container import build_container

    configured_id = "doubao-seed-2-0-lite-260428"
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        storage_root=tmp_path / "media",
        public_base_url="http://testserver",
        auth_required=False,
        platform_api_key="test-platform-key",
        deployment_environment="test",
        ark_api_key="test-ark-key",
        doubao_model_id=configured_id,
    )

    seeded = datetime(2026, 8, 26, tzinfo=UTC)
    Database(settings.database_url).create_all_and_stamp()
    with Database(settings.database_url).session() as session:
        session.add(
            ModelPricingProfile(
                id=new_id(),
                provider="seedance",
                provider_model_id=configured_id,
                input_mode="input_tokens",
                resolution="",
                currency="CNY",
                billing_unit="1M_tokens",
                # Ark Beijing list, [0, 32]k input band, 常规 online inference.
                unit_price=Decimal("0.60000000"),
                estimate_unit="1M_tokens",
                estimate_unit_price=Decimal("0.60000000"),
                usd_per_currency=Decimal("0.14743000"),
                effective_from=seeded,
                source_url="https://www.volcengine.com/docs/82379/1544106",
                source_checked_at=seeded,
            )
        )

    built = build_container(settings)
    try:
        with built.database.session() as session:
            doubao = session.scalar(
                select(ModelDefinition).where(
                    ModelDefinition.logical_name == "doubao-free-reasoner"
                )
            )
        assert doubao is not None
        assert doubao.provider_model_id == configured_id
        assert doubao.pricing_status == "VERIFIED"
    finally:
        built.database.engine.dispose()
