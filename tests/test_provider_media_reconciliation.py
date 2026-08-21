from __future__ import annotations

import asyncio
import io
from typing import Any

import httpx
import media_service.registry as media_registry_module
import pytest
from asset_registry_core import AssetRegistry, AssetVersionNotPromotable
from fastapi.testclient import TestClient
from media_service import ProviderMediaReconciliationConflict
from PIL import Image
from production_domain.models import (
    BrowserWorker,
    Character,
    CharacterIdentityVersion,
    DecisionRecord,
    MediaAsset,
    MediaProviderBinding,
    ProviderAccount,
    utcnow,
)
from provider_sdk import GenerationProvider, ProviderHealth, ProviderJob, ProviderSubmission
from sqlalchemy import func, select
from video_platform_api.main import create_app


class ReconciliationProvider(GenerationProvider):
    name = "reconciliation-test"

    def __init__(self, valid_ids: set[str] | None = None) -> None:
        self.valid_ids = set(valid_ids or set())
        self.validate_calls: list[str] = []
        self.upload_count = 0

    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        return ProviderSubmission("unused-image-job")

    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        return ProviderSubmission("unused-video-job")

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        self.upload_count += 1
        self.valid_ids.add("fresh-provider-media")
        return "fresh-provider-media"

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool:
        self.validate_calls.append(provider_media_id)
        return provider_media_id in self.valid_ids

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
    ) -> ProviderJob:
        return ProviderJob(provider_job_id, "RUNNING")

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        return False

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        return 100

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, "test")


class BlockingValidationProvider(ReconciliationProvider):
    def __init__(self) -> None:
        super().__init__({"known-provider-media"})
        self.validation_started = asyncio.Event()
        self.release_validation = asyncio.Event()

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool:
        self.validate_calls.append(provider_media_id)
        self.validation_started.set()
        await self.release_validation.wait()
        return True


class HighTrustUploadProvider(ReconciliationProvider):
    name = "google_flow"


def _png_bytes() -> bytes:
    content = io.BytesIO()
    Image.new("RGB", (2, 2), (30, 90, 180)).save(content, format="PNG")
    return content.getvalue()


def _uncertain_binding(container, project, provider: ReconciliationProvider) -> tuple[str, str, str]:
    container.providers.register(provider)
    asset, _ = container.media.register(
        project.id,
        "CHARACTER_REFERENCE",
        io.BytesIO(b"provider reconciliation reference"),
        filename="reference.png",
        mime_type="image/png",
    )
    with container.database.session() as session:
        account = ProviderAccount(
            provider=provider.name,
            account_identifier="reconciliation@example.com",
            credits=100,
            supported_models=[],
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id="reconciliation-worker",
            provider=provider.name,
            account_id=account.id,
            connection_id="reconciliation-connection",
            capabilities=["upload"],
            max_jobs=1,
        )
        session.add(worker)
        account.worker_id = worker.id
        binding = MediaProviderBinding(
            asset_id=asset.id,
            provider=provider.name,
            account_id=account.id,
            provider_media_id=None,
            status="NEEDS_RECONCILIATION",
            upload_claim_token="uncertain-upload-owner",
            upload_claim_expires_at=utcnow(),
            upload_started_at=utcnow(),
        )
        session.add(binding)
        session.flush()
        return binding.id, asset.id, account.id


def _known_media_request() -> dict[str, Any]:
    return {
        "action": "SET_KNOWN_MEDIA_ID",
        "provider_media_id": "known-provider-media",
        "reason": "Provider support confirmed the durable upload identifier.",
    }


def _platform_headers(container) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {"Authorization": f"Bearer {container.settings.platform_api_key}"}


def test_provider_media_reconciliation_rejects_missing_key_and_normal_user(container, project) -> None:  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    provider = ReconciliationProvider({"known-provider-media"})
    binding_id, _asset_id, _account_id = _uncertain_binding(container, project, provider)
    path = f"/internal/provider-media-bindings/{binding_id}/reconcile"

    with TestClient(create_app(container)) as client:
        assert client.post(path, json=_known_media_request()).status_code == 401
        registered = client.post(
            "/api/auth/register",
            json={
                "email": "ordinary-user@example.com",
                "password": "correct horse battery staple",
            },
        )
        assert registered.status_code == 201
        user_headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        assert client.post(path, headers=user_headers, json=_known_media_request()).status_code == 401

    assert provider.validate_calls == []


def test_known_provider_media_must_validate_before_ready_and_replay_is_idempotent(container, project) -> None:  # type: ignore[no-untyped-def]
    provider = ReconciliationProvider({"known-provider-media"})
    binding_id, asset_id, account_id = _uncertain_binding(container, project, provider)
    path = f"/internal/provider-media-bindings/{binding_id}/reconcile"

    with TestClient(create_app(container)) as client:
        first = client.post(path, headers=_platform_headers(container), json=_known_media_request())
        replay = client.post(path, headers=_platform_headers(container), json=_known_media_request())

    assert first.status_code == 200, first.text
    assert first.json()["status"] == "READY"
    assert first.json()["replayed"] is False
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert provider.validate_calls == ["known-provider-media"]

    with container.database.session() as session:
        binding = session.get(MediaProviderBinding, binding_id)
        asset = session.get(MediaAsset, asset_id)
        audits = list(
            session.scalars(
                select(DecisionRecord).where(DecisionRecord.decision_type == "PROVIDER_MEDIA_RECONCILIATION")
            )
        )
        assert binding.status == "READY"
        assert binding.provider_media_id == "known-provider-media"
        assert asset.provider is None
        assert asset.provider_media_id is None
        assert len(audits) == 1
        assert audits[0].selected_action == "SET_KNOWN_MEDIA_ID"
        assert audits[0].input_features == {
            "binding_id": binding_id,
            "asset_id": asset_id,
            "project_id": project.id,
            "provider": provider.name,
            "account_id": account_id,
            "action": "SET_KNOWN_MEDIA_ID",
            "reason": "Provider support confirmed the durable upload identifier.",
            "server_actor": "PLATFORM_API_KEY",
            "provider_media_id": "known-provider-media",
        }


def test_failed_provider_media_validation_keeps_binding_quarantined(container, project) -> None:  # type: ignore[no-untyped-def]
    provider = ReconciliationProvider()
    binding_id, _asset_id, _account_id = _uncertain_binding(container, project, provider)

    with TestClient(create_app(container)) as client:
        response = client.post(
            f"/internal/provider-media-bindings/{binding_id}/reconcile",
            headers=_platform_headers(container),
            json=_known_media_request(),
        )

    assert response.status_code == 409
    assert "remains quarantined" in response.json()["detail"]
    assert provider.validate_calls == ["known-provider-media"]
    with container.database.session() as session:
        assert session.get(MediaProviderBinding, binding_id).status == "NEEDS_RECONCILIATION"
        assert (
            session.scalar(
                select(func.count(DecisionRecord.id)).where(
                    DecisionRecord.decision_type == "PROVIDER_MEDIA_RECONCILIATION"
                )
            )
            == 0
        )


def test_explicit_no_remote_confirmation_allows_exactly_one_fresh_upload(container, project) -> None:  # type: ignore[no-untyped-def]
    provider = ReconciliationProvider()
    binding_id, asset_id, account_id = _uncertain_binding(container, project, provider)
    path = f"/internal/provider-media-bindings/{binding_id}/reconcile"
    request = {
        "action": "CONFIRM_REMOTE_NOT_CREATED",
        "reason": "Provider activity log confirms no upload request was created.",
        "explicit_confirmation": True,
    }

    with TestClient(create_app(container)) as client:
        rejected = client.post(
            path,
            headers=_platform_headers(container),
            json={**request, "explicit_confirmation": False},
        )
        confirmed = client.post(path, headers=_platform_headers(container), json=request)

    assert rejected.status_code == 422
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "UPLOAD_CLAIMED"

    async def resolve_twice() -> tuple[tuple[str, bool], tuple[str, bool]]:
        first = await container.media.resolve_provider_media(
            asset_id,
            provider,
            project_id=project.id,
            account_id=account_id,
            worker_id="reconciliation-worker",
        )
        second = await container.media.resolve_provider_media(
            asset_id,
            provider,
            project_id=project.id,
            account_id=account_id,
            worker_id="reconciliation-worker",
        )
        return first, second

    first, second = asyncio.run(resolve_twice())
    assert first == ("fresh-provider-media", False)
    assert second == ("fresh-provider-media", True)
    assert provider.upload_count == 1

    with container.database.session() as session:
        audit = session.scalar(
            select(DecisionRecord).where(DecisionRecord.decision_type == "PROVIDER_MEDIA_RECONCILIATION")
        )
        assert audit is not None
        assert audit.selected_action == "CONFIRM_REMOTE_NOT_CREATED"
        assert audit.input_features["asset_id"] == asset_id
        assert audit.input_features["project_id"] == project.id
        assert audit.input_features["provider"] == provider.name
        assert audit.input_features["account_id"] == account_id
        assert audit.input_features["reason"] == request["reason"]
        assert audit.input_features["server_actor"] == "PLATFORM_API_KEY"
        asset = session.get(MediaAsset, asset_id)
        binding = session.get(MediaProviderBinding, binding_id)
        assert asset.provider is None
        assert asset.provider_media_id is None
        assert binding.provider == provider.name
        assert binding.provider_media_id == "fresh-provider-media"


@pytest.mark.asyncio
async def test_provider_upload_cannot_relabel_runapi_origin_or_launder_canonical_trust(
    container,
    project,
) -> None:  # type: ignore[no-untyped-def]
    provider = HighTrustUploadProvider()
    asset, _ = container.media.register(
        project.id,
        "CHARACTER_MASTER",
        io.BytesIO(b"runapi-edge-character"),
        filename="edge-character.png",
        mime_type="image/png",
    )
    with container.database.session() as session:
        stored = session.get(MediaAsset, asset.id)
        stored.provider = "runapi"
        stored.provider_media_id = "runapi-origin-media"
        account = ProviderAccount(
            provider=provider.name,
            account_identifier="canonical-provenance@example.com",
            credits=100,
            supported_models=[],
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id="canonical-provenance-worker",
            provider=provider.name,
            account_id=account.id,
            connection_id="canonical-provenance-connection",
            capabilities=["upload"],
            max_jobs=1,
        )
        session.add(worker)
        account.worker_id = worker.id
        session.flush()
        account_id = account.id

    media_id, reused = await container.media.resolve_provider_media(
        asset.id,
        provider,
        project_id=project.id,
        account_id=account_id,
        worker_id="canonical-provenance-worker",
    )
    assert (media_id, reused) == ("fresh-provider-media", False)

    registry = AssetRegistry(container.database)
    logical = registry.create(project.id, "CHARACTER", "Edge identity draft")
    version = registry.add_version(
        logical.id,
        primary_media_asset_id=asset.id,
        source="USER_UPLOAD",
    )
    with pytest.raises(AssetVersionNotPromotable, match="low-trust generated media"):
        registry.promote(logical.id, version.id, reason="upload destination cannot raise origin trust")

    character = container.characters.create_character(project.id, "Edge identity draft")
    with pytest.raises(ValueError, match="low-trust generated media"):
        container.characters.confirm_identity(character.id, asset.id)

    with container.database.session() as session:
        stored = session.get(MediaAsset, asset.id)
        binding = session.scalar(
            select(MediaProviderBinding).where(
                MediaProviderBinding.asset_id == asset.id,
                MediaProviderBinding.provider == provider.name,
                MediaProviderBinding.account_id == account_id,
            )
        )
        persisted_character = session.get(Character, character.id)
        assert stored.provider == "runapi"
        assert stored.provider_media_id == "runapi-origin-media"
        assert binding is not None
        assert binding.provider_media_id == "fresh-provider-media"
        assert persisted_character.status == "DRAFT"
        assert persisted_character.current_identity_version_id is None
        assert stored.character_id is None
        assert (
            session.scalar(
                select(func.count(CharacterIdentityVersion.id)).where(
                    CharacterIdentityVersion.character_id == character.id
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_downloaded_generation_origin_is_recorded_once_and_never_relabelled(
    container,
    project,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    payload = _png_bytes()

    async def allow_fixture_url(_url: str, *, provider: str) -> None:
        del provider

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=payload,
            request=request,
        )
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(container.media, "_validate_remote_url", allow_fixture_url)
    monkeypatch.setattr(
        media_registry_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    original = await container.media.download_and_register(
        project.id,
        "IMAGE",
        "https://runapi.example/output.png",
        filename="output.png",
        provider="runapi",
        provider_media_id="runapi-output-1",
    )
    deduplicated = await container.media.download_and_register(
        project.id,
        "IMAGE",
        "https://flow.example/output.png",
        filename="same-output.png",
        provider="google_flow",
        provider_media_id="flow-copy-1",
    )

    assert deduplicated.id == original.id
    with container.database.session() as session:
        stored = session.get(MediaAsset, original.id)
        assert stored.provider == "runapi"
        assert stored.provider_media_id == "runapi-output-1"


@pytest.mark.asyncio
async def test_download_dedup_cannot_relabel_an_existing_user_upload_origin(
    container,
    project,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    payload = _png_bytes()
    user_upload, reused = container.media.register(
        project.id,
        "IMAGE",
        io.BytesIO(payload),
        filename="user-upload.png",
        mime_type="image/png",
    )
    assert reused is False

    async def allow_fixture_url(_url: str, *, provider: str) -> None:
        del provider

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=payload,
            request=request,
        )
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(container.media, "_validate_remote_url", allow_fixture_url)
    monkeypatch.setattr(
        media_registry_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    deduplicated = await container.media.download_and_register(
        project.id,
        "IMAGE",
        "https://runapi.example/same-user-bytes.png",
        filename="provider-copy.png",
        provider="runapi",
        provider_media_id="runapi-copy-1",
    )

    assert deduplicated.id == user_upload.id
    with container.database.session() as session:
        stored = session.get(MediaAsset, user_upload.id)
        assert stored.provider is None
        assert stored.provider_media_id is None


@pytest.mark.asyncio
async def test_reconciliation_state_race_fails_closed_without_audit(container, project) -> None:  # type: ignore[no-untyped-def]
    provider = BlockingValidationProvider()
    binding_id, _asset_id, _account_id = _uncertain_binding(container, project, provider)
    operation = asyncio.create_task(
        container.media.reconcile_provider_media(
            binding_id,
            provider,
            action="SET_KNOWN_MEDIA_ID",
            provider_media_id="known-provider-media",
            reason="Provider support supplied the identifier.",
            explicit_confirmation=False,
        )
    )
    await asyncio.wait_for(provider.validation_started.wait(), timeout=2)
    with container.database.session() as session:
        binding = session.get(MediaProviderBinding, binding_id)
        binding.status = "UPLOAD_CLAIMED"
        binding.upload_claim_token = "newer-state-owner"
        binding.upload_started_at = None
        binding.upload_claim_expires_at = utcnow()
    provider.release_validation.set()

    with pytest.raises(ProviderMediaReconciliationConflict, match="changed while"):
        await operation
    with container.database.session() as session:
        binding = session.get(MediaProviderBinding, binding_id)
        assert binding.status == "UPLOAD_CLAIMED"
        assert binding.provider_media_id is None
        assert (
            session.scalar(
                select(func.count(DecisionRecord.id)).where(
                    DecisionRecord.decision_type == "PROVIDER_MEDIA_RECONCILIATION"
                )
            )
            == 0
        )
