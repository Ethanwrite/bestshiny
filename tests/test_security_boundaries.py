from __future__ import annotations

import hashlib
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from media_service import RemoteMediaSecurityError
from media_service import registry as media_registry_module
from PIL import Image
from platform_database import Database
from platform_shared import (
    CredentialVault,
    Settings,
    StorageLimitExceeded,
    UnsafeMediaUpload,
    validate_user_media_upload,
)
from production_domain.models import (
    BrowserWorker,
    MediaAsset,
    WorkerAccessCredential,
    WorkerSocketTicket,
    WorkerStatus,
    utcnow,
)
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect
from video_platform_api.container import build_container
from video_platform_api.main import create_app
from video_platform_api.request_limits import UploadSizeLimitMiddleware

ADMIN_HEADERS = {"Authorization": "Bearer test-platform-key"}


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (3, 3), (20, 100, 220)).save(output, format="PNG")
    return output.getvalue()


def _create_account(client: TestClient, identifier: str, provider: str = "google_flow") -> str:
    response = client.post(
        "/v1/accounts",
        headers=ADMIN_HEADERS,
        json={"provider": provider, "account_identifier": identifier},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _issue_worker(
    client: TestClient,
    *,
    worker_id: str,
    account_id: str,
    provider: str = "google_flow",
) -> dict:
    response = client.post(
        "/internal/workers/credentials",
        headers=ADMIN_HEADERS,
        json={
            "worker_id": worker_id,
            "provider": provider,
            "account_id": account_id,
            "expires_in_seconds": 300,
        },
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def _worker_headers(issued: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {issued['access_token']}"}


def test_worker_credentials_are_scoped_hashed_revocable_and_not_admin(container) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_app(container)) as client:
        account_id = _create_account(client, "worker-a@example.com")
        other_account_id = _create_account(client, "worker-b@example.com")
        issued = _issue_worker(client, worker_id="worker-a", account_id=account_id)
        token = issued["access_token"]

        with container.database.session() as session:
            stored = session.get(WorkerAccessCredential, issued["id"])
            assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
            assert token not in stored.token_hash

        # A worker secret is never an internal/admin service key.
        assert client.get("/internal/benchmarks", headers=_worker_headers(issued)).status_code == 401
        # The global admin key is not accepted as a worker credential either.
        assert (
            client.post(
                "/v1/workers/register",
                headers=ADMIN_HEADERS,
                json={
                    "worker_id": "worker-a",
                    "provider": "google_flow",
                    "account_id": account_id,
                },
            ).status_code
            == 401
        )

        wrong_worker = client.post(
            "/v1/workers/register",
            headers=_worker_headers(issued),
            json={
                "worker_id": "worker-b",
                "provider": "google_flow",
                "account_id": account_id,
            },
        )
        assert wrong_worker.status_code == 403
        wrong_provider = client.post(
            "/v1/workers/register",
            headers=_worker_headers(issued),
            json={"worker_id": "worker-a", "provider": "seedance", "account_id": account_id},
        )
        assert wrong_provider.status_code == 403
        wrong_account = client.post(
            "/v1/workers/register",
            headers=_worker_headers(issued),
            json={
                "worker_id": "worker-a",
                "provider": "google_flow",
                "account_id": other_account_id,
            },
        )
        assert wrong_account.status_code == 403

        registered = client.post(
            "/v1/workers/register",
            headers=_worker_headers(issued),
            json={
                "worker_id": "worker-a",
                "provider": "google_flow",
                "account_id": account_id,
            },
        )
        assert registered.status_code == 200

        revoked = client.post(
            f"/internal/workers/credentials/{issued['id']}/revoke",
            headers=ADMIN_HEADERS,
        )
        assert revoked.status_code == 200
        assert (
            client.post(
                "/v1/workers/worker-a/heartbeat",
                headers=_worker_headers(issued),
                json={"connection_id": registered.json()["connection_id"]},
            ).status_code
            == 401
        )


def test_worker_credential_expiry_is_enforced(container) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_app(container)) as client:
        account_id = _create_account(client, "expired-worker@example.com")
        issued = _issue_worker(client, worker_id="expired-worker", account_id=account_id)
        with container.database.session() as session:
            credential = session.get(WorkerAccessCredential, issued["id"])
            credential.expires_at = utcnow() - timedelta(seconds=1)

        response = client.post(
            "/v1/workers/register",
            headers=_worker_headers(issued),
            json={
                "worker_id": "expired-worker",
                "provider": "google_flow",
                "account_id": account_id,
            },
        )
        assert response.status_code == 401


def test_websocket_uses_one_time_subprotocol_ticket_not_query_secret(container) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_app(container)) as client:
        account_id = _create_account(client, "socket-worker@example.com")
        issued = _issue_worker(client, worker_id="socket-worker", account_id=account_id)
        ticket_response = client.post(
            "/v1/workers/socket-worker/socket-ticket",
            headers=_worker_headers(issued),
        )
        assert ticket_response.status_code == 201
        assert ticket_response.headers["cache-control"] == "no-store"
        ticket = ticket_response.json()["ticket"]
        protocols = ticket_response.json()["websocket_protocols"]
        assert all(ticket not in value for value in ["/v1/workers/ws/socket-worker"])

        with client.websocket_connect(
            "/v1/workers/ws/socket-worker",
            subprotocols=protocols,
        ) as socket:
            socket.send_json(
                {
                    "type": "worker.register",
                    "payload": {
                        "provider": "google_flow",
                        "account_id": account_id,
                        "capabilities": ["image", "video", "upload", "poll"],
                    },
                }
            )
            registered = socket.receive_json()
            assert registered["type"] == "worker.registered"

        with container.database.session() as session:
            stored = session.scalar(
                select(WorkerSocketTicket).where(
                    WorkerSocketTicket.token_hash == hashlib.sha256(ticket.encode()).hexdigest()
                )
            )
            assert stored is not None and stored.consumed_at is not None
            assert ticket not in stored.token_hash

        # The ticket cannot be replayed, and even a valid worker token in the URL is ignored.
        with pytest.raises(WebSocketDisconnect) as replayed:
            with client.websocket_connect(
                "/v1/workers/ws/socket-worker",
                subprotocols=protocols,
            ):
                pass
        assert replayed.value.code == 4401
        with pytest.raises(WebSocketDisconnect) as query_secret:
            with client.websocket_connect(f"/v1/workers/ws/socket-worker?token={issued['access_token']}"):
                pass
        assert query_secret.value.code == 4401


@pytest.mark.parametrize("invalidate", ["revoke", "expire"])
def test_connected_websocket_rechecks_credential_state(container, invalidate: str) -> None:  # type: ignore[no-untyped-def]
    worker_id = f"live-{invalidate}-worker"
    with TestClient(create_app(container)) as client:
        account_id = _create_account(client, f"{worker_id}@example.com")
        issued = _issue_worker(client, worker_id=worker_id, account_id=account_id)
        ticket = client.post(
            f"/v1/workers/{worker_id}/socket-ticket",
            headers=_worker_headers(issued),
        ).json()
        with client.websocket_connect(
            f"/v1/workers/ws/{worker_id}",
            subprotocols=ticket["websocket_protocols"],
        ) as socket:
            socket.send_json(
                {
                    "type": "worker.register",
                    "payload": {"provider": "google_flow", "account_id": account_id},
                }
            )
            assert socket.receive_json()["type"] == "worker.registered"
            if invalidate == "revoke":
                assert (
                    client.post(
                        f"/internal/workers/credentials/{issued['id']}/revoke",
                        headers=ADMIN_HEADERS,
                    ).status_code
                    == 200
                )
            else:
                with container.database.session() as session:
                    credential = session.get(WorkerAccessCredential, issued["id"])
                    credential.expires_at = utcnow() - timedelta(seconds=1)
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()
            assert closed.value.code == 4401

        with container.database.session() as session:
            worker = session.get(BrowserWorker, worker_id)
            assert worker.status == WorkerStatus.OFFLINE.value


def test_vault_uses_ephemeral_dev_key_and_production_fails_closed(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    development = CredentialVault("", allow_ephemeral_key=True)
    ciphertext = development.encrypt("provider-secret")
    assert development.decrypt(ciphertext) == "provider-secret"
    with pytest.raises(ValueError):
        CredentialVault("", allow_ephemeral_key=True).decrypt(ciphertext)

    with pytest.raises(RuntimeError, match="CREDENTIAL_ENCRYPTION_KEY"):
        CredentialVault("")
    weak_but_well_formed = Fernet.generate_key()
    weak_but_well_formed = b"A" * len(weak_but_well_formed)
    with pytest.raises(RuntimeError, match="high-entropy"):
        CredentialVault(weak_but_well_formed.decode())

    secure_key = Fernet.generate_key().decode()
    assert CredentialVault(secure_key).decrypt(CredentialVault(secure_key).encrypt("secret")) == "secret"
    monkeypatch.delenv("DEPLOYMENT_ENVIRONMENT", raising=False)
    settings = Settings(
        _env_file=None,
        # Production refuses a non-PostgreSQL database, so a SQLite URL here
        # would be rejected for that reason and never reach the vault. No
        # connection is opened: every production guard is a configuration
        # guard, and they all run before the first connect.
        database_url="postgresql+psycopg://unused:unused@127.0.0.1:1/unreachable",
        storage_root=tmp_path / "media",
        platform_api_key="production-platform-key-32-bytes-unique-A7z9",
        credential_encryption_key="",
    )
    assert settings.deployment_environment == "production"
    with pytest.raises(RuntimeError, match="CREDENTIAL_ENCRYPTION_KEY"):
        build_container(settings)


@pytest.mark.parametrize("weak_key", ["", "x", "a" * 64, "short-but-varied-123"])
def test_production_rejects_weak_internal_service_keys(tmp_path, weak_key) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'weak-service-key.db'}",
        storage_root=tmp_path / "media",
        deployment_environment="production",
        platform_api_key=weak_key,
        credential_encryption_key=Fernet.generate_key().decode("ascii"),
    )
    with pytest.raises(RuntimeError, match="PLATFORM_API_KEY"):
        build_container(settings)


def test_production_rejects_disabled_auth_while_development_allows_it(tmp_path) -> None:  # type: ignore[no-untyped-def]
    production = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://unused:unused@127.0.0.1:1/unreachable",
        storage_root=tmp_path / "production-media",
        deployment_environment="production",
        auth_required=False,
        platform_api_key="production-platform-key-32-bytes-unique-A7z9",
        credential_encryption_key=Fernet.generate_key().decode("ascii"),
    )
    with pytest.raises(RuntimeError, match="AUTH_REQUIRED must remain enabled"):
        build_container(production)

    development_url = f"sqlite:///{tmp_path / 'development-auth.db'}"
    # Startup no longer creates a schema. A development database is migrated by
    # alembic; this test is about the auth guard, so it takes the sanctioned
    # throwaway-database shortcut instead of replaying every revision.
    Database(development_url).create_all_and_stamp()
    development = Settings(
        _env_file=None,
        database_url=development_url,
        storage_root=tmp_path / "development-media",
        deployment_environment="development",
        auth_required=False,
    )
    development_container = build_container(development)
    with TestClient(create_app(development_container)) as client:
        assert client.get("/v1/projects").status_code == 200


def test_user_upload_rejects_active_content_and_mime_disguises(container) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_app(container)) as client:
        project = client.post("/v1/projects", json={"title": "Safe uploads"}).json()
        project_id = project["id"]
        cases = [
            ("fake.png", b"<html><script>alert(1)</script></html>", "image/png"),
            ("vector.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>', "image/svg+xml"),
            ("image.png", _png_bytes(), "text/html"),
            ("image.html", _png_bytes(), "image/png"),
        ]
        for filename, payload, mime_type in cases:
            response = client.post(
                "/v1/assets",
                data={"project_id": project_id, "asset_type": "REFERENCE"},
                files={"file": (filename, io.BytesIO(payload), mime_type)},
            )
            assert response.status_code == 415, (filename, response.text)


def test_upload_ingress_limit_rejects_before_multipart_persistence(container) -> None:  # type: ignore[no-untyped-def]
    container.settings.max_upload_bytes = 32
    container.settings.max_upload_request_overhead_bytes = 4096
    container.storage.max_object_bytes = 32  # type: ignore[attr-defined]
    with TestClient(create_app(container)) as client:
        project_id = client.post("/v1/projects", json={"title": "Ingress limit"}).json()["id"]
        response = client.post(
            "/v1/assets",
            data={"project_id": project_id, "asset_type": "REFERENCE"},
            files={"file": ("large.png", io.BytesIO(b"x" * 5000), "image/png")},
        )
    assert response.status_code == 413
    with container.database.session() as session:
        assert session.scalar(select(MediaAsset.id)) is None


@pytest.mark.asyncio
async def test_upload_ingress_limit_counts_chunked_body_without_content_length() -> None:
    sent: list[dict] = []
    chunks = iter(
        [
            {"type": "http.request", "body": b"x" * 3000, "more_body": True},
            {"type": "http.request", "body": b"y" * 2000, "more_body": False},
        ]
    )

    async def receive() -> dict:
        return next(chunks)

    async def send(message: dict) -> None:
        sent.append(message)

    async def consume(scope, limited_receive, inner_send) -> None:  # type: ignore[no-untyped-def]
        del scope, inner_send
        while (await limited_receive()).get("more_body"):
            pass

    middleware = UploadSizeLimitMiddleware(consume, max_file_bytes=1, multipart_overhead_bytes=4096)
    await middleware(
        {"type": "http", "method": "POST", "path": "/v1/assets", "headers": []},
        receive,
        send,
    )
    assert next(message for message in sent if message["type"] == "http.response.start")["status"] == 413


def test_image_pixel_limit_rejects_decompression_bomb_shape() -> None:
    with pytest.raises(UnsafeMediaUpload, match="pixel safety limit"):
        validate_user_media_upload(
            io.BytesIO(_png_bytes()),
            filename="small-file.png",
            declared_mime="image/png",
            asset_type="IMAGE",
            max_bytes=1024,
            max_image_pixels=4,
        )


def test_storage_download_headers_prevent_content_sniffing(container) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_app(container)) as client:
        project_id = client.post("/v1/projects", json={"title": "Safe delivery"}).json()["id"]
        uploaded = client.post(
            "/v1/assets",
            data={"project_id": project_id, "asset_type": "REFERENCE"},
            files={"file": ("trusted.png", io.BytesIO(_png_bytes()), "image/png")},
        )
        assert uploaded.status_code == 200
        safe = client.get(f"/v1/storage/{uploaded.json()['storage_key']}")
        assert safe.status_code == 200
        assert safe.headers["x-content-type-options"] == "nosniff"
        assert safe.headers["content-type"].startswith("image/png")
        assert safe.headers["content-disposition"].startswith("inline")

        legacy, _ = container.media.register(
            project_id,
            "REFERENCE",
            io.BytesIO(b"<html><script>alert(1)</script></html>"),
            filename="legacy.html",
            mime_type="text/html",
        )
        unsafe = client.get(f"/v1/storage/{legacy.storage_key}")
        assert unsafe.status_code == 200
        assert unsafe.headers["x-content-type-options"] == "nosniff"
        assert unsafe.headers["content-disposition"].startswith("attachment")
        assert unsafe.headers["content-type"].startswith("text/html")


def test_media_registration_concurrently_replays_unique_winner(container, project, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    barrier = threading.Barrier(2)
    original_dimensions = container.media._image_dimensions

    def synchronized_dimensions(path: str, mime_type: str):  # type: ignore[no-untyped-def]
        result = original_dimensions(path, mime_type)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(container.media, "_image_dimensions", synchronized_dimensions)

    def register(_index: int):  # type: ignore[no-untyped-def]
        return container.media.register(
            project.id,
            "IMAGE",
            io.BytesIO(_png_bytes()),
            filename="same.png",
            mime_type="image/png",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, range(2)))

    assert results[0][0].id == results[1][0].id
    assert sorted(reused for _asset, reused in results) == [False, True]
    with container.database.session() as session:
        assert (
            len(
                list(
                    session.scalars(
                        select(MediaAsset).where(
                            MediaAsset.project_id == project.id,
                            MediaAsset.asset_type == "IMAGE",
                        )
                    )
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_provider_media_download_blocks_private_metadata_and_redirect_ssrf(
    container, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    container.media.provider_media_hosts = {
        "google_flow": ("media.example.com", "127.0.0.1", "169.254.169.254")
    }

    def resolve(host: str, port: int, **_kwargs):  # type: ignore[no-untyped-def]
        address = {
            "media.example.com": "93.184.216.34",
            "127.0.0.1": "127.0.0.1",
            "169.254.169.254": "169.254.169.254",
        }[host]
        return [(2, 1, 6, "", (address, port))]

    monkeypatch.setattr(media_registry_module.socket, "getaddrinfo", resolve)
    for url in ("https://127.0.0.1/", "https://169.254.169.254/latest/meta-data"):
        with pytest.raises(RemoteMediaSecurityError, match="non-public"):
            await container.media._validate_remote_url(url, provider="google_flow")

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302,
            headers={"location": "https://169.254.169.254/latest/meta-data"},
            request=request,
        )
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        media_registry_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    with pytest.raises(RemoteMediaSecurityError, match="non-public"):
        await container.media.download_and_register(
            "unused-project",
            "IMAGE",
            "https://media.example.com/output.png",
            filename="output.png",
            provider="google_flow",
            provider_media_id="provider-media",
        )


@pytest.mark.asyncio
async def test_provider_media_download_stream_limit_blocks_memory_dos(container, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    container.media.provider_media_hosts = {"google_flow": ("media.example.com",)}
    container.media.max_download_bytes = 16
    monkeypatch.setattr(
        media_registry_module.socket,
        "getaddrinfo",
        lambda _host, port, **_kwargs: [(2, 1, 6, "", ("93.184.216.34", port))],
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"x" * 32,
            request=request,
        )
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        media_registry_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    with pytest.raises(StorageLimitExceeded):
        await container.media.download_and_register(
            "unused-project",
            "IMAGE",
            "https://media.example.com/output.png",
            filename="output.png",
            provider="google_flow",
            provider_media_id="provider-media",
        )
