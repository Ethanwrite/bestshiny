"""The Admin Console and `live_canary_status` telling the same story.

`live_canary_status` was added to the schema, and then a writer was added for it,
without the Admin Console learning either fact. Two things followed, and these
tests hold both closed.

The console could not show the verdict at all, so the strongest production claim
this platform makes was invisible to the only person who acts on it. And a
capability change invalidated every *other* piece of production proof — lifecycle,
live gate, router, both timestamps — while leaving the canary verdict untouched,
so a model whose request contract had just been rewritten went on reading
`VERIFIED_LIVE` about a request shape that no longer existed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from production_domain.models import ModelDefinition, PlatformRole, User
from sqlalchemy import select
from video_platform_api.main import create_app


def _register(client: TestClient, email: str) -> dict:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "correct horse battery staple", "display_name": email},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _promote(container, user_id: str) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        user = session.get(User, user_id)
        assert user is not None
        user.platform_role = PlatformRole.SUPER_ADMIN.value


def _a_video_model(container) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        model = session.scalar(
            select(ModelDefinition).where(
                ModelDefinition.enabled.is_(True), ModelDefinition.modality == "video"
            )
        )
        assert model is not None
        return model.id, model.logical_name


CAPABILITIES = {
    "reason": "Provider republished the envelope; re-declaring it here",
    "supported_operations": ["video_generation"],
    "supports_image_generation": False,
    "supports_video_generation": True,
    "supports_t2v": True,
    "supports_i2v": False,
    "supports_v2v": False,
    "supports_reference_image": False,
    "supports_multi_reference": False,
    "supports_start_frame": False,
    "supports_end_frame": False,
    "supports_audio": False,
    "max_reference_images": 0,
    "min_duration": 2.0,
    "max_duration": 10.0,
    "supported_aspect_ratios": ["16:9"],
    "supported_resolutions": ["720p"],
}


@pytest.fixture
def admin(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        issued = _register(client, "canary-admin@example.com")
        _promote(container, issued["user"]["id"])
        yield client, {"Authorization": f"Bearer {issued['access_token']}"}


def test_the_console_shows_the_canary_verdict_not_only_a_timestamp(admin, container) -> None:  # type: ignore[no-untyped-def]
    client, headers = admin
    model_id, _ = _a_video_model(container)
    with container.database.session() as session:
        model = session.get(ModelDefinition, model_id)
        model.live_canary_status = "VERIFIED_LIVE"
        model.live_canary_detail = "job j-1 · provider task t-9 · 413652 B artifact registered"

    listing = client.get("/api/admin/models", headers=headers)
    assert listing.status_code == 200, listing.text
    row = next(item for item in listing.json()["items"] if item["id"] == model_id)
    assert row["live_canary_status"] == "VERIFIED_LIVE"
    # The handle an auditor takes back to the vendor's console.
    assert "t-9" in row["live_canary_detail"]

    detail = client.get(f"/api/admin/models/{model_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["live_canary_status"] == "VERIFIED_LIVE"


def test_a_manual_admin_verification_does_not_grant_verified_live(admin, container) -> None:  # type: ignore[no-untyped-def]
    """`verified` follows an admin's own record; the canary verdict does not.

    Both used to be readable only through `last_verified_at`, which an admin
    moves by recording a verification. If the console reported that as the live
    canary result, a reviewed model would read as production-proven.
    """

    client, headers = admin
    model_id, _ = _a_video_model(container)

    recorded = client.post(
        f"/api/admin/models/{model_id}/verifications",
        headers={**headers, "Idempotency-Key": "manual-verification-1"},
        json={
            "protocol_version": "admin-manual-v1",
            "result": "SUCCESS",
            "evidence_reference": "internal-run-42",
            "billable": False,
        },
    )
    assert recorded.status_code == 201, recorded.text

    row = client.get(f"/api/admin/models/{model_id}", headers=headers).json()
    assert row["verified"] is True, "the admin's own verification did land"
    assert row["live_canary_status"] == "NOT_RUN", "but no canary ever ran"


def test_changing_the_contract_retires_the_canary_verdict_with_the_rest(admin, container) -> None:  # type: ignore[no-untyped-def]
    client, headers = admin
    model_id, _ = _a_video_model(container)
    with container.database.session() as session:
        model = session.get(ModelDefinition, model_id)
        model.live_canary_status = "VERIFIED_LIVE"
        model.live_canary_detail = "job j-1 · provider task t-9"
        model.live_enabled = True
        model.router_enabled = True

    changed = client.post(
        f"/api/admin/models/{model_id}/capabilities", headers=headers, json=CAPABILITIES
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["verification_invalidated"] is True

    with container.database.session() as session:
        model = session.get(ModelDefinition, model_id)
        # Everything that was proof of the old contract is retired together.
        assert model.lifecycle_status == "CONFIGURED"
        assert model.live_enabled is False
        assert model.router_enabled is False
        assert model.last_verified_at is None
        assert model.last_live_test_at is None
        # The one that used to survive, and must not.
        assert model.live_canary_status == "NOT_RUN"
        assert "capability contract changed" in model.live_canary_detail
