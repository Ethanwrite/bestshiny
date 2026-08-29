"""The three live facts stay separate: enabled is not registered is not proven."""

from __future__ import annotations

from fastapi.testclient import TestClient
from production_domain.models import ModelDefinition
from sqlalchemy import select, update
from video_platform_api.main import create_app


def test_live_status_reports_zero_verified_regardless_of_enablement(container) -> None:  # type: ignore[no-untyped-def]
    """live_enabled=true must never surface as a completed live canary."""

    with container.database.session() as session:
        session.execute(update(ModelDefinition).values(live_enabled=True))
        total = len(list(session.scalars(select(ModelDefinition))))
    assert total > 0, "the seeded registry is the fixture here"
    container.settings.platform_api_key = "live-status-test-key"
    headers = {"Authorization": "Bearer live-status-test-key"}
    with TestClient(create_app(container)) as client:
        response = client.get("/internal/models/live-status", headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
    summary = body["summary"]
    assert summary["total"] == total
    assert summary["live_enabled"] == total
    assert summary["verified_live"] == 0
    assert summary["verified_live_models"] == []
    assert "neither is production validation" in summary["note"]
    assert all(item["live_canary_status"] == "NOT_RUN" for item in body["models"])


def test_a_recorded_canary_is_the_only_thing_that_counts(container) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        first = session.scalar(select(ModelDefinition).order_by(ModelDefinition.logical_name))
        first.live_canary_status = "VERIFIED_LIVE"
        first.live_canary_detail = "one real generation completed and reconciled"
        verified_name = first.logical_name
    container.settings.platform_api_key = "live-status-test-key"
    headers = {"Authorization": "Bearer live-status-test-key"}
    with TestClient(create_app(container)) as client:
        body = client.get("/internal/models/live-status", headers=headers).json()
    assert body["summary"]["verified_live"] == 1
    assert body["summary"]["verified_live_models"] == [verified_name]
