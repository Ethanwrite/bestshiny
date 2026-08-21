from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from media_service import WorkspaceStorageQuota
from PIL import Image
from production_domain.models import (
    AuthLoginThrottle,
    AuthSession,
    MediaAsset,
    PasswordResetToken,
    StorageReservation,
    User,
    Workspace,
)
from sqlalchemy import func, select
from video_platform_api.auth import AUTH_COOKIE_NAME, CSRF_COOKIE_NAME
from video_platform_api.main import create_app

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "new correct horse battery staple"


def _register(client: TestClient, email: str) -> dict:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Commercial Owner"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _bearer(issued: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {issued['access_token']}"}


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(output, format="PNG", compress_level=0)
    return output.getvalue()


def test_cookie_auth_is_httponly_and_cookie_unsafe_requests_require_double_submit_csrf(
    container,
) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        registered = _register(client, "cookie-owner@example.com")
        set_cookies = client.post(
            "/api/auth/login",
            json={"email": "cookie-owner@example.com", "password": PASSWORD},
        ).headers.get_list("set-cookie")
        auth_cookie = next(value for value in set_cookies if value.startswith(f"{AUTH_COOKIE_NAME}="))
        csrf_cookie = next(value for value in set_cookies if value.startswith(f"{CSRF_COOKIE_NAME}="))
        assert "HttpOnly" in auth_cookie
        assert "SameSite=lax" in auth_cookie
        assert "HttpOnly" not in csrf_cookie
        assert "SameSite=lax" in csrf_cookie
        assert client.get("/api/auth/me").status_code == 200

        rejected = client.post("/v1/projects", json={"title": "Missing CSRF"})
        assert rejected.status_code == 403

        csrf_token = client.cookies.get(CSRF_COOKIE_NAME)
        accepted = client.post(
            "/v1/projects",
            headers={"X-CSRF-Token": csrf_token},
            json={"title": "Cookie Project"},
        )
        assert accepted.status_code == 200

        # An existing browser cookie does not change bearer API semantics.
        bearer_request = client.post(
            "/v1/projects",
            headers=_bearer(registered),
            json={"title": "Bearer Project"},
        )
        assert bearer_request.status_code == 200

        assert client.post("/api/auth/logout").status_code == 403
        logged_out = client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert logged_out.status_code == 204
        assert client.cookies.get(AUTH_COOKIE_NAME) is None
        assert client.cookies.get(CSRF_COOKIE_NAME) is None


def test_production_auth_cookie_is_secure_and_reset_request_never_returns_token(
    container,
) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    container.settings.deployment_environment = "production"
    with TestClient(create_app(container), base_url="https://testserver") as client:
        registered = client.post(
            "/api/auth/register",
            json={"email": "secure@example.com", "password": PASSWORD},
        )
        assert registered.status_code == 201
        auth_cookie = next(
            value
            for value in registered.headers.get_list("set-cookie")
            if value.startswith(f"{AUTH_COOKIE_NAME}=")
        )
        assert "Secure" in auth_cookie
        assert "HttpOnly" in auth_cookie
        requested = client.post(
            "/api/auth/password-reset/request",
            json={"email": "secure@example.com"},
        )
        assert requested.status_code == 200
        assert requested.json() == {"message": "如果该邮箱存在，重置说明已发送"}


def test_login_throttle_is_durable_across_app_instances(container) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        _register(client, "throttled@example.com")
        for _ in range(5):
            response = client.post(
                "/api/auth/login",
                json={"email": "throttled@example.com", "password": "wrong-password"},
            )
            assert response.status_code == 401

    with container.database.session() as session:
        throttle = session.scalar(select(AuthLoginThrottle))
        assert throttle is not None
        assert throttle.failure_count == 5
        assert throttle.blocked_until is not None
        assert throttle.blocked_until.replace(tzinfo=UTC) > datetime.now(UTC)

    # A fresh app/service instance reads the same durable throttle row.
    with TestClient(create_app(container)) as restarted_client:
        blocked = restarted_client.post(
            "/api/auth/login",
            json={"email": "throttled@example.com", "password": PASSWORD},
        )
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) > 0


def test_password_reset_is_generic_single_use_and_revokes_existing_sessions(
    container,
) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        registered = _register(client, "reset-owner@example.com")
        unknown = client.post(
            "/api/auth/password-reset/request",
            json={"email": "missing@example.com"},
        )
        assert unknown.status_code == 200
        assert unknown.json() == {"message": "如果该邮箱存在，重置说明已发送"}

        requested = client.post(
            "/api/auth/password-reset/request",
            json={"email": "reset-owner@example.com"},
        )
        assert requested.status_code == 200
        reset_token = requested.json()["reset_token"]
        confirmed = client.post(
            "/api/auth/password-reset/confirm",
            json={"token": reset_token, "new_password": NEW_PASSWORD},
        )
        assert confirmed.status_code == 200
        assert client.get("/api/auth/me", headers=_bearer(registered)).status_code == 401
        replay = client.post(
            "/api/auth/password-reset/confirm",
            json={"token": reset_token, "new_password": NEW_PASSWORD},
        )
        assert replay.status_code == 400
        assert (
            client.post(
                "/api/auth/login",
                json={"email": "reset-owner@example.com", "password": NEW_PASSWORD},
            ).status_code
            == 200
        )

    with container.database.session() as session:
        reset = session.scalar(select(PasswordResetToken))
        user = session.scalar(select(User).where(User.email == "reset-owner@example.com"))
        assert reset is not None and reset.consumed_at is not None
        assert reset.token_hash == hashlib.sha256(reset_token.encode()).hexdigest()
        assert reset_token not in reset.token_hash
        assert user is not None and NEW_PASSWORD not in user.password_hash
        assert (
            session.scalar(
                select(func.count(AuthSession.id)).where(
                    AuthSession.user_id == user.id,
                    AuthSession.revoked_at.is_(None),
                )
            )
            == 1
        )


def test_workspace_upload_quota_settles_dedupe_without_double_charge_and_replays(
    container,
) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    first_image = _png_bytes((10, 20, 30))
    second_image = _png_bytes((200, 100, 50))
    assert len(first_image) == len(second_image)
    with TestClient(create_app(container)) as client:
        registered = _register(client, "quota-owner@example.com")
        headers = _bearer(registered)
        project = client.post(
            "/v1/projects",
            headers=headers,
            json={"title": "Quota Project"},
        ).json()
        workspace_id = registered["user"]["workspaces"][0]["id"]
        with container.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            assert workspace is not None
            workspace.max_storage_bytes = len(first_image)

        first = client.post(
            "/v1/assets",
            headers={**headers, "Idempotency-Key": "quota-upload-first"},
            data={"project_id": project["id"], "asset_type": "REFERENCE"},
            files={"file": ("first.png", io.BytesIO(first_image), "image/png")},
        )
        assert first.status_code == 200, first.text
        assert first.json()["reused"] is False

        # Exact logical dedupe is accepted even at the quota ceiling and never
        # increments used bytes a second time.
        deduped = client.post(
            "/v1/assets",
            headers={**headers, "Idempotency-Key": "quota-upload-dedupe"},
            data={"project_id": project["id"], "asset_type": "REFERENCE"},
            files={"file": ("first.png", io.BytesIO(first_image), "image/png")},
        )
        assert deduped.status_code == 200, deduped.text
        assert deduped.json()["id"] == first.json()["id"]
        assert deduped.json()["reused"] is True

        replayed = client.post(
            "/v1/assets",
            headers={**headers, "Idempotency-Key": "quota-upload-first"},
            data={"project_id": project["id"], "asset_type": "REFERENCE"},
            files={"file": ("first.png", io.BytesIO(first_image), "image/png")},
        )
        assert replayed.status_code == 200
        assert replayed.json()["id"] == first.json()["id"]
        assert replayed.json()["reused"] is True

        denied = client.post(
            "/v1/assets",
            headers={**headers, "Idempotency-Key": "quota-upload-over-limit"},
            data={"project_id": project["id"], "asset_type": "REFERENCE"},
            files={"file": ("second.png", io.BytesIO(second_image), "image/png")},
        )
        assert denied.status_code == 413

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        reservations = list(
            session.scalars(select(StorageReservation).where(StorageReservation.workspace_id == workspace_id))
        )
        assert workspace is not None
        assert workspace.used_storage_bytes == len(first_image)
        assert workspace.reserved_storage_bytes == 0
        assert {item.status for item in reservations} == {"SETTLED"}
        assert len(reservations) == 2
        assert session.scalar(select(func.count(MediaAsset.id))) == 1


def test_workspace_upload_failure_releases_reserved_capacity_without_media_leak(
    container,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    image = _png_bytes((50, 60, 70))
    app = create_app(container)
    with TestClient(app, raise_server_exceptions=False) as client:
        registered = _register(client, "quota-failure@example.com")
        headers = _bearer(registered)
        project = client.post(
            "/v1/projects",
            headers=headers,
            json={"title": "Quota Failure"},
        ).json()
        workspace_id = registered["user"]["workspaces"][0]["id"]

        def fail_registration(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("fixture registration failure")

        monkeypatch.setattr(container.media, "register", fail_registration)
        failed = client.post(
            "/v1/assets",
            headers={**headers, "Idempotency-Key": "quota-upload-failure"},
            data={"project_id": project["id"], "asset_type": "REFERENCE"},
            files={"file": ("failure.png", io.BytesIO(image), "image/png")},
        )
        assert failed.status_code == 500

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        reservation = session.scalar(select(StorageReservation))
        assert workspace is not None
        assert workspace.used_storage_bytes == 0
        assert workspace.reserved_storage_bytes == 0
        assert reservation is not None and reservation.status == "RELEASED"
        assert session.scalar(select(func.count(MediaAsset.id))) == 0


def test_workspace_upload_post_registration_settle_failure_keeps_fail_closed_hold(
    container,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    image = _png_bytes((80, 90, 100))
    app = create_app(container)
    with TestClient(app, raise_server_exceptions=False) as client:
        registered = _register(client, "quota-settle-failure@example.com")
        headers = _bearer(registered)
        project = client.post(
            "/v1/projects",
            headers=headers,
            json={"title": "Quota Settle Failure"},
        ).json()
        workspace_id = registered["user"]["workspaces"][0]["id"]

        def fail_settle(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("fixture settlement failure")

        monkeypatch.setattr(WorkspaceStorageQuota, "settle", fail_settle)
        failed = client.post(
            "/v1/assets",
            headers={**headers, "Idempotency-Key": "quota-settle-failure"},
            data={"project_id": project["id"], "asset_type": "REFERENCE"},
            files={"file": ("settle-failure.png", io.BytesIO(image), "image/png")},
        )
        assert failed.status_code == 500

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        reservation = session.scalar(select(StorageReservation))
        assert workspace is not None
        assert workspace.used_storage_bytes == 0
        assert workspace.reserved_storage_bytes == len(image)
        assert reservation is not None and reservation.status == "RESERVED"
        assert session.scalar(select(func.count(MediaAsset.id))) == 1
