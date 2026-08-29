"""A model with no published price cannot be opened for live routing.

`reconcile-live` re-derives `live_enabled` from the credentials present now,
which was the right fix for its own problem — adding a key to `.env` used to do
nothing. But it read only the transport, so a provider with working credentials
and no published rate was opened anyway.

Live mode refuses an unpriced model rather than estimating one, so that
combination is a promise the platform cannot keep: the router offers the model
and every generation fails on `PricingUnverified`. It was not hypothetical.
`google_flow / flow-veo-3.1` and `google_flow / NARWHAL` were both `enabled` and
`live_enabled` in production with no pricing row at all, because Flow's
credentials are configured and nothing checked further.

Credit for the transport is not credit for the price.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from production_domain.models import (
    ModelCapabilityProfile as ModelCapabilityProfileRow,
)
from production_domain.models import ModelDefinition, ModelPricingProfile
from provider_sdk import LIVE_PROVIDER_CONFIRMATION
from test_provider_gateway import FakeProvider, add_fake_route
from video_platform_api.main import create_app


def _report(container, client) -> dict:  # type: ignore[no-untyped-def]
    response = client.post(
        "/internal/models/reconcile-live",
        headers={"Authorization": f"Bearer {container.settings.platform_api_key}"},
    )
    assert response.status_code == 200, response.text
    return {row["logical_name"]: row for row in response.json()["models"]}


@pytest.fixture
def unpriced_model(container):  # type: ignore[no-untyped-def]
    """A model whose provider is configured but which carries no rate.

    The live gate is opened too, so the only thing left that can hold this model
    shut is the missing price -- otherwise every assertion here would pass for
    the wrong reason.
    """

    container.settings.provider_mode = "live"
    container.settings.allow_live_provider_calls = True
    container.settings.live_provider_confirmation = LIVE_PROVIDER_CONFIRMATION
    add_fake_route(container, FakeProvider())
    with container.database.session() as session:
        definition = ModelDefinition(
            logical_name="unpriced-probe",
            provider="fake",
            provider_model_id="fake-model",
            modality="video",
            capabilities=["video_generation"],
            enabled=True,
            live_enabled=False,
        )
        session.add(definition)
        session.flush([definition])
        # `all_runtime_models` inner-joins the capability profile, so a
        # definition without one is invisible to reconciliation entirely.
        session.add(
            ModelCapabilityProfileRow(
                model_definition_id=definition.id,
                supported_operations=["video_generation"],
            )
        )
    return "unpriced-probe"


def test_an_unpriced_model_is_not_opened_and_says_why(container, unpriced_model):  # type: ignore[no-untyped-def]
    with TestClient(create_app(container)) as client:
        rows = _report(container, client)

    row = rows[unpriced_model]
    assert row["would_change"] is False, "a model with no rate must not be opened"
    assert row["live_enabled"] is False
    assert "pricing" in (row["blocked_by"] or "").lower(), row["blocked_by"]


def test_the_same_model_opens_once_it_carries_a_published_rate(container, unpriced_model):  # type: ignore[no-untyped-def]
    """The gate is the missing price, not the model -- give it one and it opens.

    Without this, the first test would still pass if the gate were simply
    "never open anything".
    """

    with TestClient(create_app(container)) as client:
        assert _report(container, client)[unpriced_model]["would_change"] is False

        with container.database.session() as session:
            session.add(
                ModelPricingProfile(
                    provider="fake",
                    provider_model_id="fake-model",
                    input_mode="no_video_input",
                    resolution="720p",
                    currency="USD",
                    billing_unit="second",
                    unit_price=Decimal("0.05"),
                    estimate_unit="second",
                    estimate_unit_price=Decimal("0.05"),
                    usd_per_currency=Decimal("1"),
                    effective_from=datetime(2026, 8, 26, tzinfo=UTC),
                    source_url="https://example.invalid/rates",
                    source_checked_at=datetime(2026, 8, 26, tzinfo=UTC),
                )
            )

        after = _report(container, client)[unpriced_model]

    assert after["would_change"] is True, "a priced model with a working transport opens"
    assert not after["blocked_by"]
