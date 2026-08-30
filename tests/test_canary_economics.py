"""Live-canary permit economics (E2E audit 2026-08-30, C5 / §4.1).

The live audit proved every permit was a one-call permit regardless of
``max_requests``: an unquoted call held the whole remaining budget, chat costs
never settled because token-billing providers report counts rather than a cost
figure, and EXHAUSTED stayed terminal even after the hold settled to nearly
zero. These tests pin the repaired economics — token-derived holds, list-priced
settlement (``cost_source=TOKENS_LIST``), and EXHAUSTED as a measurement that
recovery from settlement can reverse.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from cost_core import TokenCostEngine
from entitlement_core import (
    LiveCanaryDenied,
    LiveCanaryPermitService,
    ModelRoleRuntime,
    WorkspaceModelResolver,
)
from model_registry_core import ModelRole
from production_domain.models import (
    LiveCanaryPermit,
    LiveCanaryUsage,
    ModelDefinition,
    ModelExecutionRecord,
    ModelPricingProfile,
    utcnow,
)
from provider_sdk import (
    ChatCapability,
    FactLockSet,
    ProviderCapability,
    ProviderCapabilityCatalog,
    ProviderTrustLevel,
)
from sqlalchemy import select

USD_PER_CNY = Decimal("0.14743")


def _seed_token_pricing(
    container,  # type: ignore[no-untyped-def]
    provider: str,
    model: str,
    *,
    input_cny: str = "0.60",
    output_cny: str | None = "3.60",
    cached_cny: str | None = None,
) -> None:
    directions = [("input_tokens", input_cny)]
    if output_cny is not None:
        directions.append(("output_tokens", output_cny))
    if cached_cny is not None:
        directions.append(("cached_input_tokens", cached_cny))
    with container.database.session() as session:
        for direction, price in directions:
            session.add(
                ModelPricingProfile(
                    provider=provider,
                    provider_model_id=model,
                    input_mode=direction,
                    resolution="",
                    currency="CNY",
                    billing_unit="1M_tokens",
                    unit_price=Decimal(price),
                    estimate_unit="1M_tokens",
                    estimate_unit_price=Decimal(price),
                    usd_per_currency=USD_PER_CNY,
                    effective_from=utcnow() - timedelta(days=1),
                    source_url="https://example.invalid/test-token-pricing",
                    source_checked_at=utcnow() - timedelta(days=1),
                )
            )


# --------------------------------------------------------------------- permits


def test_settlement_reactivates_an_exhausted_permit(container) -> None:  # type: ignore[no-untyped-def]
    service = LiveCanaryPermitService(container.database)
    permit = service.create(
        provider="seedance",
        model="doubao-seed-2-0-lite-260428",
        max_requests=3,
        max_cost_usd="0.10",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="multi-request chat permit",
    )

    # An unquoted call holds everything and exhausts the permit — deliberately.
    first = service.reserve_matching(
        provider="seedance",
        model="doubao-seed-2-0-lite-260428",
        idempotency_key="chat-turn-1",
    )
    assert first.estimated_cost_usd == Decimal("0.100000")
    with container.database.session() as session:
        stored = session.get(LiveCanaryPermit, permit.id)
        assert stored is not None and stored.status == "EXHAUSTED"

    # Settlement replaces the hold with the real figure and hands the budget
    # back: the permit is usable again, which is what max_requests=3 promised.
    service.settle(first.usage_id, actual_cost_usd="0.000400", evidence_reference="tokens:turn-1")
    with container.database.session() as session:
        stored = session.get(LiveCanaryPermit, permit.id)
        assert stored is not None
        assert stored.status == "ACTIVE"
        assert stored.reserved_cost_usd == Decimal("0.000000")
        assert stored.actual_cost_usd == Decimal("0.000400")

    second = service.reserve_matching(
        provider="seedance",
        model="doubao-seed-2-0-lite-260428",
        idempotency_key="chat-turn-2",
        estimated_cost_usd="0.001000",
    )
    assert second.replayed is False
    service.settle(second.usage_id, actual_cost_usd="0.000500", evidence_reference="tokens:turn-2")

    third = service.reserve_matching(
        provider="seedance",
        model="doubao-seed-2-0-lite-260428",
        idempotency_key="chat-turn-3",
        estimated_cost_usd="0.001000",
    )
    service.settle(third.usage_id, actual_cost_usd="0.000500", evidence_reference="tokens:turn-3")

    # The request count is spent for real: settlement never revives that.
    with container.database.session() as session:
        stored = session.get(LiveCanaryPermit, permit.id)
        assert stored is not None
        assert stored.used_requests == 3
        assert stored.status == "EXHAUSTED"
    with pytest.raises(LiveCanaryDenied, match="request limit"):
        service.reserve_matching(
            provider="seedance",
            model="doubao-seed-2-0-lite-260428",
            idempotency_key="chat-turn-4",
            estimated_cost_usd="0.001000",
        )


def test_settlement_never_revives_an_expired_permit(container) -> None:  # type: ignore[no-untyped-def]
    service = LiveCanaryPermitService(container.database)
    permit = service.create(
        provider="seedance",
        model="doubao-seed-2-0-lite-260428",
        max_requests=2,
        max_cost_usd="0.10",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="expires before settlement",
    )
    reservation = service.reserve_matching(
        provider="seedance",
        model="doubao-seed-2-0-lite-260428",
        idempotency_key="expired-chat-turn",
    )
    with container.database.session() as session:
        stored = session.get(LiveCanaryPermit, permit.id)
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    service.settle(
        reservation.usage_id,
        actual_cost_usd="0.000400",
        evidence_reference="tokens:late",
    )
    with container.database.session() as session:
        stored = session.get(LiveCanaryPermit, permit.id)
        assert stored is not None
        assert stored.status != "ACTIVE"


def test_reconciliation_hands_capacity_back(container) -> None:  # type: ignore[no-untyped-def]
    service = LiveCanaryPermitService(container.database)
    permit = service.create(
        provider="seedance",
        model="doubao-seedream-5-0-260128",
        max_requests=2,
        max_cost_usd="0.10",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="reconciled image permit",
    )
    reservation = service.reserve_matching(
        provider="seedance",
        model="doubao-seedream-5-0-260128",
        idempotency_key="image-op-1",
    )
    service.mark_uncertain(reservation.usage_id, evidence_reference="boundary:image-op-1")
    with container.database.session() as session:
        stored = session.get(LiveCanaryPermit, permit.id)
        assert stored is not None and stored.status == "EXHAUSTED"

    reconciled, _, replayed = service.reconcile_uncertain(
        reservation.usage_id,
        action="CONFIRM_PROVIDER_NOT_CREATED",
        actual_cost_usd=None,
        idempotency_key="rec-image-op-1",
        reason="provider refused before creating a job",
        evidence_reference="ark-console:no-task",
    )
    assert replayed is False and reconciled.status == "SETTLED"
    with container.database.session() as session:
        stored = session.get(LiveCanaryPermit, permit.id)
        assert stored is not None
        assert stored.status == "ACTIVE"
        assert stored.reserved_cost_usd == Decimal("0.000000")


# ---------------------------------------------------------------- token engine


def test_token_engine_settles_counts_at_list_rates(container) -> None:  # type: ignore[no-untyped-def]
    _seed_token_pricing(container, "seedance", "doubao-seed-2-0-lite-260428")
    engine = TokenCostEngine(container.database)
    settlement = engine.settle_from_usage(
        "seedance",
        "doubao-seed-2-0-lite-260428",
        {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
    )
    assert settlement is not None
    # 1000 × 0.60 CNY/1M + 500 × 3.60 CNY/1M, at 0.14743 USD/CNY.
    assert settlement.cost_usd == Decimal("0.000354")
    assert settlement.detail == (
        "1000in+0cached+500out@seedance:doubao-seed-2-0-lite-260428:1M_tokens"
    )


def test_token_engine_prices_cached_input_at_its_own_rate(container) -> None:  # type: ignore[no-untyped-def]
    _seed_token_pricing(
        container,
        "deepseek",
        "deepseek-v4-flash",
        input_cny="0.44",
        output_cny="1.32",
        cached_cny="0.014",
    )
    engine = TokenCostEngine(container.database)
    settlement = engine.settle_from_usage(
        "deepseek",
        "deepseek-v4-flash",
        {
            "prompt_tokens": 1000,
            "completion_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 600},
        },
    )
    assert settlement is not None
    assert settlement.cached_input_tokens == 600
    # 400 full-rate + 600 cached-rate input tokens, no output.
    expected = (
        (Decimal(400) * Decimal("0.44") + Decimal(600) * Decimal("0.014"))
        * USD_PER_CNY
        / Decimal(1_000_000)
    ).quantize(Decimal("0.000001"))
    assert settlement.cost_usd == expected


def test_token_engine_refuses_what_it_cannot_price(container) -> None:  # type: ignore[no-untyped-def]
    engine = TokenCostEngine(container.database)
    # No pricing rows at all.
    assert (
        engine.settle_from_usage("seedance", "unpriced-model", {"prompt_tokens": 10}) is None
    )
    # Output tokens with no output rate must not settle at a wrong figure.
    _seed_token_pricing(container, "seedance", "input-only-model", output_cny=None)
    assert (
        engine.settle_from_usage(
            "seedance", "input-only-model", {"prompt_tokens": 10, "completion_tokens": 5}
        )
        is None
    )
    assert engine.estimate_call(
        "seedance", "input-only-model", input_characters=100, max_output_tokens=100
    ) is None
    # No counts, no settlement.
    _seed_token_pricing(container, "seedance", "doubao-seed-2-0-lite-260428")
    assert (
        engine.settle_from_usage("seedance", "doubao-seed-2-0-lite-260428", {"cost_hint": "x"})
        is None
    )


def test_token_engine_estimate_is_a_bounded_margin_hold(container) -> None:  # type: ignore[no-untyped-def]
    _seed_token_pricing(container, "seedance", "doubao-seed-2-0-lite-260428")
    engine = TokenCostEngine(container.database)
    estimate = engine.estimate_call(
        "seedance",
        "doubao-seed-2-0-lite-260428",
        input_characters=100,
        max_output_tokens=1000,
    )
    # (100 in-tokens + 1000 out-tokens at list) × margin 2, and never below the
    # minimum hold that keeps a permit from fanning out at zero.
    expected = (
        (Decimal(100) * Decimal("0.60") + Decimal(1000) * Decimal("3.60"))
        * USD_PER_CNY
        / Decimal(1_000_000)
        * Decimal(2)
    ).quantize(Decimal("0.000001"))
    assert estimate == expected
    tiny = engine.estimate_call(
        "seedance",
        "doubao-seed-2-0-lite-260428",
        input_characters=0,
        max_output_tokens=0,
    )
    assert tiny == Decimal("0.000100")
    assert engine.estimate_call("seedance", "unpriced", input_characters=1, max_output_tokens=1) is None


# -------------------------------------------------------------------- runtime


class _FixtureChatCapability(ChatCapability):
    trust_level = ProviderTrustLevel.PRODUCTION
    configured = True

    def __init__(self) -> None:
        self.call_count = 0

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del model, messages, parameters
        self.call_count += 1
        return {
            "choices": [{"message": {"content": "{}"}}],
            # Token counts and no cost figure — the Ark chat shape that could
            # never settle before TOKENS_LIST pricing existed.
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        }


def _live_chat_runtime(container, project, role: ModelRole, *, criticality: str = "STANDARD"):  # type: ignore[no-untyped-def]
    resolver = WorkspaceModelResolver(container.database, container.model_infrastructure)
    capability = _FixtureChatCapability()
    providers = ProviderCapabilityCatalog()
    registered: set[str] = set()
    selected = resolver.resolve(project.id, role, asset_criticality=criticality)
    with container.database.session() as session:
        definition = session.get(ModelDefinition, selected.definition_id)
        assert definition is not None
        definition.live_enabled = True
    providers.register(selected.provider, capability, {ProviderCapability.CHAT.value})
    registered.add(selected.provider)
    service = LiveCanaryPermitService(container.database)
    runtime = ModelRoleRuntime(
        container.database,
        resolver,
        providers,
        provider_mode="live",
        live_canary=service,
        token_costs=TokenCostEngine(container.database),
    )
    return runtime, service, capability, selected, resolver, providers, registered


@pytest.mark.asyncio
async def test_live_chat_holds_a_token_estimate_and_settles_from_counts(container, project) -> None:  # type: ignore[no-untyped-def]
    runtime, service, capability, selected, _, _, _ = _live_chat_runtime(
        container, project, ModelRole.DIRECTOR
    )
    _seed_token_pricing(container, selected.provider, selected.provider_model_id)
    service.create(
        provider=selected.provider,
        model=selected.provider_model_id,
        max_requests=3,
        max_cost_usd="0.10",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="a whole director conversation on one permit",
    )

    execution = await runtime.execute_chat(
        project.id,
        ModelRole.DIRECTOR,
        messages=[{"role": "user", "content": "一支30秒的城市天台悬疑广告"}],
        parameters={"response_format": {"type": "json_object"}},
    )
    assert capability.call_count == 1
    with container.database.session() as session:
        usage = session.scalars(select(LiveCanaryUsage)).one()
        # The hold is a bounded token-derived figure, not the whole budget.
        assert usage.estimated_cost_usd < Decimal("0.05")
        assert usage.status == "SETTLED"
        assert usage.actual_cost_usd == Decimal("0.000354")
        permit = session.scalars(select(LiveCanaryPermit)).one()
        assert permit.status == "ACTIVE"
        assert permit.reserved_cost_usd == Decimal("0.000000")
        assert permit.actual_cost_usd == Decimal("0.000354")
        record = session.get(ModelExecutionRecord, execution.execution_record_id)
        assert record is not None
        assert record.cost_source == "TOKENS_LIST"
        assert record.actual_cost_usd == Decimal("0.000354")
        assert record.metadata_json["token_pricing_detail"].startswith("1000in+0cached+500out@")

    # The same permit carries a second turn: it is no longer a one-call permit.
    await runtime.execute_chat(
        project.id,
        ModelRole.DIRECTOR,
        messages=[{"role": "user", "content": "地点改成城市天台"}],
    )
    assert capability.call_count == 2
    with container.database.session() as session:
        permit = session.scalars(select(LiveCanaryPermit)).one()
        assert permit.used_requests == 2
        assert permit.status == "ACTIVE"


@pytest.mark.asyncio
async def test_live_chat_without_token_pricing_still_holds_everything(container, project) -> None:  # type: ignore[no-untyped-def]
    runtime, service, capability, selected, _, _, _ = _live_chat_runtime(
        container, project, ModelRole.DIRECTOR
    )
    service.create(
        provider=selected.provider,
        model=selected.provider_model_id,
        max_requests=3,
        max_cost_usd="0.10",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        purpose="no pricing rows: conservative hold",
    )
    await runtime.execute_chat(
        project.id,
        ModelRole.DIRECTOR,
        messages=[{"role": "user", "content": "unpriced model call"}],
    )
    assert capability.call_count == 1
    with container.database.session() as session:
        usage = session.scalars(select(LiveCanaryUsage)).one()
        # No canonical rates: the whole remaining budget was held, and with no
        # way to price the counted tokens the usage stays UNCERTAIN for an
        # operator instead of being settled at an invented figure.
        assert usage.estimated_cost_usd == Decimal("0.100000")
        assert usage.status == "UNCERTAIN"


@pytest.mark.asyncio
async def test_refine_prompt_degrades_on_canary_denial(container, project) -> None:  # type: ignore[no-untyped-def]
    # The ALL-plan low-cost refiner is a disabled placeholder, exactly as on
    # production: the primary leg degrades on the missing binding and the
    # fallback leg reaches the live-canary fence.
    runtime, _, capability, selected, _, _, _ = _live_chat_runtime(
        container, project, ModelRole.PROMPT_REFINER_FALLBACK, criticality="EDGE"
    )
    del selected

    # No permit exists for the fallback refiner model. The call is refused at
    # the spending fence, and the route-facing result degrades instead of
    # raising — the 500 the 2026-08-30 live audit hit on production.
    result = await runtime.refine_prompt(
        project.id,
        original_prompt="雨夜城市天台，霓虹反光",
        fact_locks=FactLockSet(
            {"narrative_event": "雨夜城市天台，霓虹反光"},
            locked_spans={"narrative_event": ("雨夜城市天台，霓虹反光",)},
        ),
    )
    assert capability.call_count == 0
    assert result.source == "local_safe_fallback"
    assert result.accepted is False
    assert result.optimized_candidate == "雨夜城市天台，霓虹反光"
    assert "PRIMARY_UNAVAILABLE" in result.reason_codes
    assert "FALLBACK_UNAVAILABLE" in result.reason_codes
