"""A Voyage multimodal call settles on the two axes it is actually billed on.

Migration ``0071_voyage_official_provider`` seeds the vendor's official list
prices for ``voyage/voyage-multimodal-3.5``, both open-ended and in USD:

    input_mode="input_tokens", billing_unit="1M_tokens", unit_price=0.12
    input_mode="image_input",  billing_unit="1B_pixels", unit_price=0.60

Until the pixel row was read, ``TokenCostEngine`` filtered on ``1M_tokens``
alone, so a live embedding settled nothing: ``actual_cost_usd`` stayed NULL,
``cost_source`` degraded, and the spend authorization closed at the quote
ceiling under ESTIMATED_QUOTE. These tests hold the arithmetic and the record.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cost_core import PIXEL_BILLING_UNIT, TOKEN_BILLING_UNIT, TokenCostEngine
from entitlement_core import (
    LiveCanaryPermitService,
    ModelRoleRuntime,
    ProductionBudgetPolicy,
    ProductionBudgetService,
    WorkspaceModelResolver,
)
from entitlement_core.production_budget import FENCE_PRODUCTION, SOURCE_TOKENS_LIST
from fastapi.testclient import TestClient
from model_registry_core import VERIFIED_LIVE, ModelRole
from production_domain.models import (
    GenerationSpendAuthorization,
    ModelDefinition,
    ModelExecutionRecord,
    ModelPricingProfile,
    utcnow,
)
from provider_sdk import (
    EmbeddingCapability,
    ProviderCapability,
    ProviderCapabilityCatalog,
    ProviderTrustLevel,
)
from sqlalchemy import select
from video_platform_api.main import create_app

PROVIDER = "voyage"
MODEL = "voyage-multimodal-3.5"
# The exact rows 0071 inserts, in the vendor's own currency (USD) at the
# vendor's own units. Nothing here is a discount, a promotion or a conversion.
TEXT_USD_PER_1M_TOKENS = Decimal("0.12")
IMAGE_USD_PER_1B_PIXELS = Decimal("0.60")
# 0079. The same rate, because the vendor counts each video frame as an
# image: "For pricing purposes, each video frame is considered an image."
VIDEO_USD_PER_1B_PIXELS = Decimal("0.60")

# One recorded multimodal embedding: 18 text tokens beside a single 224x224
# frame (50_176 pixels) and no video pixels.
USAGE: dict[str, Any] = {
    "text_tokens": 18,
    "image_pixels": 50_176,
    "video_pixels": 0,
    "total_tokens": 66,
    # A key this platform has never heard of must survive the round trip.
    "voyage_internal_hint": "keep-me",
}
# 18 * 0.12 / 1e6            = 0.00000216
# 50_176 * 0.60 / 1e9        = 0.0000301056
# ---------------------------------------- +
#                              0.0000322656, at the column's six decimals:
EXPECTED_COST = Decimal("0.000032")


def _seed_voyage_pricing(  # type: ignore[no-untyped-def]
    container, *, image: bool = True, video: bool = False
) -> list[str]:
    """The 0071 rows, plus 0079's video row, as the migrations write them."""

    rows = [("input_tokens", TOKEN_BILLING_UNIT, TEXT_USD_PER_1M_TOKENS)]
    if image:
        rows.append(("image_input", PIXEL_BILLING_UNIT, IMAGE_USD_PER_1B_PIXELS))
    if video:
        rows.append(("video_input", PIXEL_BILLING_UNIT, VIDEO_USD_PER_1B_PIXELS))
    created: list[str] = []
    with container.database.session() as session:
        for input_mode, billing_unit, price in rows:
            profile = ModelPricingProfile(
                provider=PROVIDER,
                provider_model_id=MODEL,
                input_mode=input_mode,
                resolution="",
                currency="USD",
                billing_unit=billing_unit,
                unit_price=price,
                estimate_unit=billing_unit,
                estimate_unit_price=price,
                usd_per_currency=Decimal("1.0"),
                effective_from=utcnow() - timedelta(days=1),
                source_url="https://docs.voyageai.com/docs/pricing",
                source_checked_at=utcnow() - timedelta(days=1),
            )
            session.add(profile)
            session.flush()
            created.append(profile.id)
    return created


class _FixtureVoyageCapability(EmbeddingCapability):
    trust_level = ProviderTrustLevel.PRODUCTION
    configured = True

    def __init__(self, usage: dict[str, Any] | None = None) -> None:
        self.call_count = 0
        self.usage = dict(USAGE if usage is None else usage)

    async def create_embeddings(
        self,
        *,
        model: str,
        inputs: str | list[str] | list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del model, inputs
        self.call_count += 1
        dimension = int((parameters or {}).get("dimensions", 256))
        # A vendor that bills per token reports counts and never a cost.
        return {"data": [{"embedding": [1.0] * dimension}], "usage": dict(self.usage)}


def _live_embedding_runtime(container, project, capability: _FixtureVoyageCapability):  # type: ignore[no-untyped-def]
    resolver = WorkspaceModelResolver(container.database, container.model_infrastructure)
    selected = resolver.resolve(project.id, ModelRole.MULTIMODAL_EMBEDDING)
    assert (selected.provider, selected.provider_model_id) == (PROVIDER, MODEL)
    with container.database.session() as session:
        definition = session.get(ModelDefinition, selected.definition_id)
        assert definition is not None
        definition.live_enabled = True
    providers = ProviderCapabilityCatalog()
    providers.register(selected.provider, capability, {ProviderCapability.EMBEDDINGS.value})
    _seed_voyage_pricing(container)
    runtime = ModelRoleRuntime(
        container.database,
        resolver,
        providers,
        provider_mode="live",
        live_canary=LiveCanaryPermitService(container.database),
        token_costs=TokenCostEngine(container.database),
        production_budget=ProductionBudgetService(
            container.database,
            ProductionBudgetPolicy(platform_limit_usd=Decimal("1.00"), provider_limits_usd={}),
        ),
    )
    return runtime, selected


# ------------------------------------------------------------- the arithmetic


def test_voyage_usage_settles_at_the_official_text_and_pixel_rates(container) -> None:  # type: ignore[no-untyped-def]
    _seed_voyage_pricing(container)
    engine = TokenCostEngine(container.database)

    settlement = engine.settle_from_usage(PROVIDER, MODEL, USAGE)

    assert settlement is not None
    expected = (
        Decimal(18) * TEXT_USD_PER_1M_TOKENS / Decimal(1_000_000)
        + Decimal(50_176) * IMAGE_USD_PER_1B_PIXELS / Decimal(1_000_000_000)
    ).quantize(Decimal("0.000001"))
    assert expected == EXPECTED_COST
    assert settlement.cost_usd == EXPECTED_COST
    assert settlement.input_tokens == 18
    assert settlement.image_pixels == 50_176
    assert settlement.video_pixels == 0
    # The counts an operator has to be able to re-derive the figure from.
    assert settlement.detail == (
        f"18in+0cached+0out+50176ipx+0vpx@{PROVIDER}:{MODEL}:{TOKEN_BILLING_UNIT}+{PIXEL_BILLING_UNIT}"
    )


def test_both_pricing_rows_stay_traceable_on_the_rates(container) -> None:  # type: ignore[no-untyped-def]
    seeded = _seed_voyage_pricing(container)

    rates = TokenCostEngine(container.database).rates_for(PROVIDER, MODEL)

    assert rates is not None
    assert rates.input_usd_per_token == TEXT_USD_PER_1M_TOKENS / Decimal(1_000_000)
    assert rates.image_usd_per_pixel == IMAGE_USD_PER_1B_PIXELS / Decimal(1_000_000_000)
    # This seed carries only 0071's two rows, and the engine invents nothing
    # from them: the video rate comes from 0079's row or from nowhere.
    assert rates.video_usd_per_pixel is None
    assert set(rates.profile_ids) == set(seeded)


def test_unpriced_pixels_are_never_settled_as_free(container) -> None:  # type: ignore[no-untyped-def]
    # Only the text row exists, as it did before 0071's image row was read.
    _seed_voyage_pricing(container, image=False)
    engine = TokenCostEngine(container.database)

    assert engine.settle_from_usage(PROVIDER, MODEL, USAGE) is None
    # A text-only call in the same shape still settles: zero pixels cost zero
    # at any rate, so an absent rate cannot understate it.
    text_only = engine.settle_from_usage(
        PROVIDER,
        MODEL,
        {"text_tokens": 18, "image_pixels": 0, "video_pixels": 0, "total_tokens": 18},
    )
    assert text_only is not None
    assert text_only.cost_usd == Decimal("0.000002")
    assert text_only.detail.endswith(f"@{PROVIDER}:{MODEL}:{TOKEN_BILLING_UNIT}")


def test_video_pixels_without_a_video_rate_refuse_to_settle(container) -> None:  # type: ignore[no-untyped-def]
    """Still true for any model whose video price is genuinely unknown.

    0079 prices this one, so the seed here deliberately omits that row: what
    is pinned is that a *missing* price refuses to settle rather than
    silently pricing video pixels off the image row.
    """

    _seed_voyage_pricing(container)
    engine = TokenCostEngine(container.database)

    assert (
        engine.settle_from_usage(
            PROVIDER,
            MODEL,
            {"text_tokens": 18, "image_pixels": 50_176, "video_pixels": 921_600},
        )
        is None
    )


def test_a_usage_block_with_no_countable_axis_still_settles_nothing(container) -> None:  # type: ignore[no-untyped-def]
    _seed_voyage_pricing(container)
    engine = TokenCostEngine(container.database)

    assert engine.settle_from_usage(PROVIDER, MODEL, {"voyage_internal_hint": "x"}) is None


def test_token_only_providers_keep_their_settlement_line(container) -> None:  # type: ignore[no-untyped-def]
    """Reading pixels must not change what a chat settlement looks like."""

    with container.database.session() as session:
        for input_mode, price in (("input_tokens", "0.60"), ("output_tokens", "3.60")):
            session.add(
                ModelPricingProfile(
                    provider="seedance",
                    provider_model_id="doubao-seed-2-0-lite-260428",
                    input_mode=input_mode,
                    resolution="",
                    currency="CNY",
                    billing_unit=TOKEN_BILLING_UNIT,
                    unit_price=Decimal(price),
                    estimate_unit=TOKEN_BILLING_UNIT,
                    estimate_unit_price=Decimal(price),
                    usd_per_currency=Decimal("0.14743"),
                    effective_from=utcnow() - timedelta(days=1),
                    source_url="https://example.invalid/test-token-pricing",
                    source_checked_at=utcnow() - timedelta(days=1),
                )
            )

    settlement = TokenCostEngine(container.database).settle_from_usage(
        "seedance",
        "doubao-seed-2-0-lite-260428",
        {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
    )

    assert settlement is not None
    assert settlement.cost_usd == Decimal("0.000354")
    assert settlement.detail == (
        "1000in+0cached+500out@seedance:doubao-seed-2-0-lite-260428:1M_tokens"
    )
    assert (settlement.image_pixels, settlement.video_pixels) == (0, 0)


# ------------------------------------------------------------- the settlement


@pytest.mark.asyncio
async def test_a_live_voyage_call_records_its_real_cost_and_settles_the_budget(
    container,
    project,
) -> None:  # type: ignore[no-untyped-def]
    capability = _FixtureVoyageCapability()
    runtime, selected = _live_embedding_runtime(container, project, capability)

    execution = await runtime.execute_embeddings(
        project.id,
        inputs=[{"content": [{"type": "text", "text": "rooftop lantern"}]}],
        parameters={"dimensions": 256, "input_type": "document"},
    )

    assert capability.call_count == 1
    with container.database.session() as session:
        # `live_canary` and `production_budget` both hard-reject a settlement
        # source they do not know, so a TOKENS_LIST figure reaching either is
        # itself the assertion that the string stayed the shared vocabulary.
        definition = session.get(ModelDefinition, selected.definition_id)
        assert definition is not None and definition.live_canary_status == VERIFIED_LIVE
        assert "settled USD 0.000032" in definition.live_canary_detail
        record = session.get(ModelExecutionRecord, execution.execution_record_id)
        assert record is not None
        assert record.actual_cost_usd == EXPECTED_COST
        # The vendor's official list price applied to the provider's own
        # reported usage: not a provider invoice, not an estimate.
        assert record.cost_source == "TOKENS_LIST"
        # The whole usage block survives, unknown keys included.
        assert record.token_usage_json == USAGE
        assert record.metadata_json["token_pricing_image_pixels"] == 50_176
        assert record.metadata_json["token_pricing_video_pixels"] == 0
        assert "50176ipx" in record.metadata_json["token_pricing_detail"]
        assert record.metadata_json["live_fence"] == FENCE_PRODUCTION

        authorizations = list(session.scalars(select(GenerationSpendAuthorization)))
    assert len(authorizations) == 1
    authorization = authorizations[0]
    assert authorization.status == "SETTLED"
    assert authorization.settlement_source == SOURCE_TOKENS_LIST
    # The quote ceiling is a planning hold with a margin on top; settling there
    # would charge the platform's breaker several times what the call cost.
    assert authorization.actual_cost_usd == EXPECTED_COST
    assert authorization.actual_cost_usd < authorization.max_cost_usd


def test_an_operator_can_see_the_usage_the_cost_was_derived_from(container, project) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        definition = session.scalar(
            select(ModelDefinition).where(
                ModelDefinition.provider == PROVIDER,
                ModelDefinition.provider_model_id == MODEL,
            )
        )
        assert definition is not None
        session.add(
            ModelExecutionRecord(
                project_id=project.id,
                role=ModelRole.MULTIMODAL_EMBEDDING.value,
                model_definition_id=definition.id,
                provider=PROVIDER,
                provider_model_id=MODEL,
                request_hash="e" * 64,
                latency_ms=12.5,
                token_usage_json=USAGE,
                actual_cost_usd=EXPECTED_COST,
                cost_source="TOKENS_LIST",
                status="SUCCEEDED",
                metadata_json={"capability": "embeddings", "input_count": 1},
            )
        )

    with TestClient(create_app(container)) as client:
        response = client.get(
            "/internal/production-evidence",
            params={"project_id": project.id},
            headers={"Authorization": f"Bearer {container.settings.platform_api_key}"},
        )

    assert response.status_code == 200, response.text
    view = response.json()["model_executions"][0]
    assert view["actual_cost_usd"] == "0.000032"
    assert view["cost_source"] == "TOKENS_LIST"
    assert view["token_usage"] == {
        "text_tokens": 18,
        "image_pixels": 50_176,
        "video_pixels": 0,
        "total_tokens": 66,
    }


# --- 0079: the video-pixel rate -------------------------------------------


def test_video_pixels_settle_at_the_official_rate_once_0079_prices_them(container) -> None:  # type: ignore[no-untyped-def]
    """All three axes, at the vendor's own list prices, to the exact cent-fraction.

    The frame path the platform actually uses never produces video pixels -
    `BoundedVideoFrameSampler` sends stills, which the provider bills as image
    pixels. This covers the case where a usage block reports video pixels
    directly, which before 0079 settled to nothing at all.
    """

    _seed_voyage_pricing(container, video=True)
    engine = TokenCostEngine(container.database)

    settlement = engine.settle_from_usage(
        PROVIDER,
        MODEL,
        {"text_tokens": 18, "image_pixels": 50_176, "video_pixels": 921_600},
    )

    assert settlement is not None
    # 18 * 0.12 / 1e6        = 0.00000216
    # 50_176 * 0.60 / 1e9    = 0.0000301056
    # 921_600 * 0.60 / 1e9   = 0.00055296
    # ------------------------------------- +
    #                          0.0005852256, at the column's six decimals:
    expected = (
        Decimal(18) * TEXT_USD_PER_1M_TOKENS / Decimal(1_000_000)
        + Decimal(50_176) * IMAGE_USD_PER_1B_PIXELS / Decimal(1_000_000_000)
        + Decimal(921_600) * VIDEO_USD_PER_1B_PIXELS / Decimal(1_000_000_000)
    ).quantize(Decimal("0.000001"))
    assert expected == Decimal("0.000585")
    assert settlement.cost_usd == Decimal("0.000585")
    assert (settlement.input_tokens, settlement.image_pixels, settlement.video_pixels) == (
        18,
        50_176,
        921_600,
    )
    assert settlement.detail == (
        f"18in+0cached+0out+50176ipx+921600vpx"
        f"@{PROVIDER}:{MODEL}:{TOKEN_BILLING_UNIT}+{PIXEL_BILLING_UNIT}"
    )


def test_the_video_rate_is_traceable_to_its_own_row(container) -> None:  # type: ignore[no-untyped-def]
    seeded = _seed_voyage_pricing(container, video=True)

    rates = TokenCostEngine(container.database).rates_for(PROVIDER, MODEL)

    assert rates is not None
    assert rates.video_usd_per_pixel == VIDEO_USD_PER_1B_PIXELS / Decimal(1_000_000_000)
    # Same number as the image rate, but its own row: nothing here falls back
    # from one input mode to another, so a model whose video price is unknown
    # stays UNCERTAIN rather than being priced off its image row.
    assert rates.video_usd_per_pixel == rates.image_usd_per_pixel
    assert len(set(rates.profile_ids)) == 3
    assert set(rates.profile_ids) == set(seeded)


def test_migration_0079_writes_the_video_row_and_takes_it_away_again(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The row an operator actually gets, written by alembic rather than a fixture."""

    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    database_url = f"sqlite:///{tmp_path / 'voyage-pricing.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)

    def _row() -> dict[str, Any] | None:
        engine = sa.create_engine(database_url)
        try:
            with engine.begin() as connection:
                found = connection.execute(
                    sa.text(
                        "select input_mode, billing_unit, unit_price, currency, source_url "
                        "from model_pricing_profiles "
                        "where provider = 'voyage' and provider_model_id = :model "
                        "and input_mode = 'video_input'"
                    ),
                    {"model": MODEL},
                ).mappings().one_or_none()
                return dict(found) if found else None
        finally:
            engine.dispose()

    command.upgrade(config, "head")
    row = _row()
    assert row is not None, "0079 did not write the voyage video-pixel price"
    assert row["billing_unit"] == PIXEL_BILLING_UNIT
    assert Decimal(str(row["unit_price"])) == VIDEO_USD_PER_1B_PIXELS
    assert row["currency"] == "USD"
    assert row["source_url"] == "https://docs.voyageai.com/docs/pricing"

    # Re-running must not write a second row for the same input mode.
    command.downgrade(config, "0078_creative_session_create_idempotency")
    assert _row() is None
    command.upgrade(config, "head")
    assert _row() is not None
