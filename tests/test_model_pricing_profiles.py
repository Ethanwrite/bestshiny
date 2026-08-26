"""What a paid quote is allowed to be made of.

Before 0044 a video was priced from one `estimated_per_second` per model scaled
by a resolution table shared by every provider on the platform. Both halves were
guesses. Seedance 2.5 carried `estimated_per_second = 0.09` against a published
Ark rate of 1.512 CNY/s — about 40% of the real price, on every call, silently —
and the multiplier charged 1080p at 1.30x where Ark's own rates put it at 2.47x.

These tests pin the replacement: a price is a row read off a provider's page
with a date and a URL, and where there is no such row the platform refuses the
money rather than inventing a number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from cost_core import CreditPricingEngine, PricingUnverified
from platform_contracts import GenerationRequest
from production_domain.models import ModelDefinition, ModelPricingProfile
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]

ARK_MODEL = "doubao-seedance-2-5-260628"
ARK_URL = "https://www.volcengine.com/docs/82379/1544106"
USD_PER_CNY = 0.14743
# Volcengine Ark, Seedance 2.5, 16:9, no video input, read 2026-08-26.
ARK_CNY_PER_SECOND = {"480p": 0.672, "720p": 1.512, "1080p": 3.742}


def _profile(**overrides: object) -> ModelPricingProfile:
    checked = datetime(2026, 8, 26, tzinfo=UTC)
    values: dict[str, object] = {
        "provider": "seedance",
        "provider_model_id": ARK_MODEL,
        "input_mode": "no_video_input",
        "resolution": "720p",
        "currency": "CNY",
        "billing_unit": "token",
        "unit_price": 70.0,
        "estimate_unit": "second",
        "estimate_unit_price": ARK_CNY_PER_SECOND["720p"],
        "usd_per_currency": USD_PER_CNY,
        "fx_source": "PBOC/CFETS central parity 2026-08-26",
        "fx_checked_at": checked,
        "estimate_formula": "estimate_unit_price * duration_seconds * usd_per_currency",
        "settlement_formula": "unit_price * usage.completion_tokens / 1000000 * usd_per_currency",
        "effective_from": checked,
        "effective_until": None,
        "source_url": ARK_URL,
        "source_checked_at": checked,
        "notes": "",
    }
    values.update(overrides)
    return ModelPricingProfile(**values)  # type: ignore[arg-type]


def _seed(container, *profiles: ModelPricingProfile) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        for item in profiles:
            session.add(item)


def _strict(container) -> CreditPricingEngine:  # type: ignore[no-untyped-def]
    """The engine as a live deployment builds it: no unverified price is quotable."""

    return CreditPricingEngine(
        container.model_registry,
        database=container.database,
        require_verified_pricing=True,
    )


def _estimate(engine: CreditPricingEngine, **overrides: object):  # type: ignore[no-untyped-def]
    request: dict[str, object] = {
        "provider": "seedance",
        "model": ARK_MODEL,
        "media_type": "video",
        "duration": 5,
        "resolution": "720p",
    }
    request.update(overrides)
    return engine.estimate(**request)  # type: ignore[arg-type]


def test_resolution_is_priced_by_its_own_profile_not_a_platform_wide_multiplier(container) -> None:
    """The multiplier table is gone from the paid path, not merely corrected.

    It claimed 1080p costs 1.30x of 720p for every provider at once. Ark charges
    3.742 CNY/s against 1.512 — 2.47x — and sells 480p at 0.44x, which the table
    had no entry for at all, so 480p was billed as though it were 720p.
    """

    _seed(
        container,
        *[
            _profile(resolution=name, estimate_unit_price=price)
            for name, price in ARK_CNY_PER_SECOND.items()
        ],
    )
    engine = _strict(container)
    priced = {name: _estimate(engine, resolution=name) for name in ARK_CNY_PER_SECOND}

    # Nothing is scaled after the fact: the profile already is the price.
    assert {item.resolution_multiplier for item in priced.values()} == {1.0}

    ratio = priced["1080p"].provider_cost_usd / priced["720p"].provider_cost_usd
    assert ratio == pytest.approx(2.475, abs=0.01)
    assert ratio != pytest.approx(1.30, abs=0.01)
    assert priced["480p"].provider_cost_usd / priced["720p"].provider_cost_usd == pytest.approx(
        0.444, abs=0.01
    )
    # 5s at 1.512 CNY/s converted at the recorded rate, and no other arithmetic
    # (the estimate is reported to four decimal places).
    assert priced["720p"].provider_cost_usd == pytest.approx(
        round(5 * 1.512 * USD_PER_CNY, 4), abs=1e-9
    )


def test_a_model_with_no_published_price_cannot_be_quoted_for_money(container) -> None:
    """Failing closed is the whole point: an unknown price is not a cheap price."""

    engine = _strict(container)
    with pytest.raises(PricingUnverified) as raised:
        _estimate(engine)
    assert "no verified price" in str(raised.value)
    assert raised.value.model == ARK_MODEL


def test_the_seeded_placeholder_still_serves_development_and_says_that_it_did(container) -> None:
    """Offline work keeps a number. What it must not do is keep it quietly."""

    lenient = CreditPricingEngine(
        container.model_registry, database=container.database, require_verified_pricing=False
    )
    estimate = _estimate(lenient)
    assert estimate.pricing_status == "UNVERIFIED"
    assert estimate.pricing_source_url == ""

    _seed(container, _profile())
    verified = _estimate(lenient)
    assert verified.pricing_status == "VERIFIED"
    assert verified.pricing_source_url == ARK_URL
    assert verified.pricing_checked_at.startswith("2026-08-26")
    assert verified.billing_unit == "token"
    # Reservation and settlement are different arithmetic on purpose: Ark quotes
    # per second and bills on completion tokens nobody can count in advance.
    assert "completion_tokens" in verified.settlement_formula


def test_unverified_pricing_is_never_absorbed_into_a_free_generation(container, project) -> None:
    """The compatibility path prices legacy requests at zero. This must not reach it.

    `PricingUnverified` subclasses `ValueError` so the routes keep answering 400,
    and admission catches `ValueError` to price pre-commercial projects at zero.
    Left alone, that combination turns "we do not know what this costs" into
    "this is free" — the one answer that is certainly wrong.
    """

    container.credit_pricing.require_verified_pricing = True
    request = GenerationRequest(
        project_id=project.id,
        type="video",
        provider="seedance",
        model=ARK_MODEL,
        prompt="a street at night",
        duration=5,
        idempotency_key="pricing-unverified-must-not-be-free",
    )
    with pytest.raises(PricingUnverified):
        container.generation_admission.admit_passenger(request, resolution="720p")


def test_video_input_pricing_follows_the_policy_not_the_reference_count(container) -> None:
    """Ark discounts video-input tokens. A character reference is not a video.

    Keying the input mode off `reference_count` charged every reference-guided
    shot as video input, and because the video-input rates are published as a
    range and deliberately unseeded, fail-closed then refused the entire class
    of shots this platform generates most.
    """

    _seed(container, _profile())
    engine = _strict(container)

    # References, start frames, continuation from a still: all image inputs.
    for policy in ("TEXT_TO_VIDEO", "IMAGE_TO_VIDEO", "REFERENCE_TO_VIDEO", "CONTINUE_I2V"):
        estimate = _estimate(engine, generation_policy=policy, reference_count=3)
        assert estimate.pricing_status == "VERIFIED", policy

    # The one policy that feeds a clip back in has no published rate seeded, and
    # an unseeded mode is a refusal rather than a guess.
    with pytest.raises(PricingUnverified):
        _estimate(engine, generation_policy="CONTINUE_V2V")


def test_a_dated_promotion_wins_while_it_runs_and_lapses_without_anyone_acting(container) -> None:
    """A discount folded into the base price is a discount that never ends."""

    now = datetime.now(UTC)
    list_rate = _profile(resolution="1080p", estimate_unit_price=3.742)
    promotion = _profile(
        resolution="1080p",
        estimate_unit_price=3.742 * 0.72,
        effective_from=now - timedelta(days=1),
        effective_until=now + timedelta(days=7),
    )
    _seed(container, list_rate, promotion)
    engine = _strict(container)
    assert _estimate(engine, resolution="1080p").provider_cost_usd == pytest.approx(
        round(5 * 3.742 * 0.72 * USD_PER_CNY, 4), abs=1e-9
    )

    with container.database.session() as session:
        row = session.get(ModelPricingProfile, promotion.id)
        assert row is not None
        row.effective_until = now - timedelta(minutes=1)

    # Nobody edited the list price to bring it back; the window simply closed.
    assert _estimate(engine, resolution="1080p").provider_cost_usd == pytest.approx(
        round(5 * 3.742 * USD_PER_CNY, 4), abs=1e-9
    )


def test_migration_seeds_the_ark_rates_that_were_actually_published(tmp_path, monkeypatch) -> None:
    """The seeded profile is checked against the vendor page, not against itself."""

    database_path = tmp_path / "pricing-seed.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "select resolution, estimate_unit_price, unit_price, currency, billing_unit, "
                "source_url, effective_until from model_pricing_profiles "
                "where provider_model_id = :model and effective_until is null"
            ),
            {"model": ARK_MODEL},
        ).mappings().all()
    engine.dispose()

    seeded = {item["resolution"]: item for item in rows}
    assert set(seeded) == set(ARK_CNY_PER_SECOND)
    for name, cny_per_second in ARK_CNY_PER_SECOND.items():
        assert float(seeded[name]["estimate_unit_price"]) == pytest.approx(cny_per_second)
        assert seeded[name]["currency"] == "CNY"
        assert seeded[name]["billing_unit"] == "token"
        assert seeded[name]["source_url"] == ARK_URL
    # 1080p is a different token rate, not the same rate at a higher resolution.
    assert float(seeded["1080p"]["unit_price"]) == pytest.approx(77.0)
    assert float(seeded["720p"]["unit_price"]) == pytest.approx(70.0)


def test_pricing_status_is_derived_from_the_profiles_rather_than_left_to_drift(container) -> None:
    """The status column is a report. A report nobody recomputes becomes a claim.

    0044's migration marks the models that exist when it runs, so on a fresh
    deployment — migrated before the registry seeds its rows — the one model
    with published rates would report UNVERIFIED, and a model whose profiles
    were later withdrawn would go on reporting VERIFIED. Both are resolved by
    deriving the column from the table the quote already consults.
    """

    infrastructure = container.model_infrastructure

    def status_of(logical_name: str) -> str | None:
        with container.database.session() as session:
            row = session.scalar(
                select(ModelDefinition).where(ModelDefinition.logical_name == logical_name)
            )
            return None if row is None else row.pricing_status

    # Nothing is priced in a test database, so nothing may claim to be.
    infrastructure.reconcile_pricing_status()
    assert status_of("seedance-2.5-official") == "UNVERIFIED"

    _seed(container, _profile())
    assert infrastructure.reconcile_pricing_status() == 1
    assert status_of("seedance-2.5-official") == "VERIFIED"

    # Withdrawing the price withdraws the claim, without anyone remembering to.
    with container.database.session() as session:
        for row in session.scalars(select(ModelPricingProfile)).all():
            session.delete(row)
    assert infrastructure.reconcile_pricing_status() == 1
    assert status_of("seedance-2.5-official") == "UNVERIFIED"


def test_video_input_rates_are_deliberately_absent_rather_than_estimated(tmp_path, monkeypatch) -> None:
    """Ark publishes video-input pricing as a range. A range is not a price."""

    database_path = tmp_path / "pricing-modes.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{database_path}")
    with engine.connect() as connection:
        modes = connection.execute(
            sa.text(
                "select distinct input_mode from model_pricing_profiles "
                "where provider_model_id = :model"
            ),
            {"model": ARK_MODEL},
        ).scalars().all()
    engine.dispose()
    assert set(modes) == {"no_video_input"}
