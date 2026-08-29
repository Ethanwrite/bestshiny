from __future__ import annotations

import asyncio
import io
import ipaddress
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from PIL import Image
from platform_database import Database
from platform_shared import (
    MEDIA_HEADER_BYTES,
    StorageLimitExceeded,
    StorageProvider,
    UnsafeMediaUpload,
    affected_rows,
    validate_user_media_upload,
)
from production_domain.models import (
    DecisionRecord,
    MediaAsset,
    MediaProviderBinding,
    ProviderAccount,
    new_id,
    utcnow,
)
from provider_sdk import GenerationProvider, ProviderReferenceConstraints
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .renditions import RenditionDerivationFailed, RenditionResolver
from .staging import StagedProviderOutput


class RemoteMediaSecurityError(ValueError):
    pass


class ProviderMediaReconciliationRequired(RuntimeError):
    """An upload may have reached the paid provider and must be reconciled manually."""


class ProviderMediaUploadInProgress(RuntimeError):
    """Another process still owns the live upload claim."""


class ProviderMediaReconciliationConflict(RuntimeError):
    """The binding state changed or does not permit the requested manual action."""


class ProviderMediaValidationFailed(RuntimeError):
    """The provider could not validate the media identifier supplied by an operator."""


class ProviderReferenceUrlUnavailable(RuntimeError):
    """A fetchable reference URL does not exist for a provider that requires one."""


@dataclass(frozen=True)
class ProviderMediaReconciliationResult:
    binding_id: str
    asset_id: str
    project_id: str
    provider: str
    account_id: str
    status: str
    provider_media_id: str | None
    action: str
    replayed: bool = False


@dataclass(frozen=True)
class _ProviderMediaBindingContext:
    binding_id: str
    asset_id: str
    project_id: str
    provider: str
    account_id: str
    worker_id: str | None
    status: str
    provider_media_id: str | None
    claim_token: str | None


def lineage_key(
    *,
    character_id: str | None = None,
    scene_id: str | None = None,
    shot_id: str | None = None,
    parent_asset_id: str | None = None,
    generation_candidate_id: str | None = None,
) -> str:
    """The scope one media asset is deduplicated within.

    The single definition on purpose. This value decides whether an upload is a
    duplicate of one the project already holds, and it used to be computed by
    two independent formulas — one here and one in the API's upload route — that
    agreed only because their association order happened to match. A change to
    either would not have failed; it would have turned deduplication off.
    """

    associations = (
        ("candidate", generation_candidate_id),
        ("shot", shot_id),
        ("parent", parent_asset_id),
        ("character", character_id),
        ("scene", scene_id),
    )
    parts = [f"{name}:{value}" for name, value in associations if value]
    return "|".join(parts) if parts else "shared"


class MediaRegistry:
    def __init__(
        self,
        database: Database,
        storage: StorageProvider,
        *,
        provider_media_hosts: dict[str, tuple[str, ...]] | None = None,
        provider_media_credentials: dict[str, str] | None = None,
        max_download_bytes: int = 100 * 1024 * 1024,
        max_image_pixels: int = 50_000_000,
        provider_upload_claim_seconds: float = 120.0,
        provider_upload_wait_seconds: float | None = None,
        provider_upload_poll_seconds: float = 0.05,
        reference_url_ttl_seconds: int = 900,
    ):
        self.database = database
        self.storage = storage
        self.renditions = RenditionResolver(storage, max_derived_bytes=max_download_bytes)
        self.reference_url_ttl_seconds = max(60, reference_url_ttl_seconds)
        self.provider_media_hosts = provider_media_hosts or {}
        # Bearer tokens for providers that serve their own artefacts from an
        # authenticated endpoint rather than a signed CDN URL. Keyed by provider,
        # so one provider's key can never be presented to another's host.
        self.provider_media_credentials = provider_media_credentials or {}
        self.max_download_bytes = max(1, max_download_bytes)
        self.max_image_pixels = max(1, max_image_pixels)
        self.provider_upload_claim_seconds = max(1.0, provider_upload_claim_seconds)
        configured_wait = (
            provider_upload_wait_seconds
            if provider_upload_wait_seconds is not None
            else self.provider_upload_claim_seconds + 5.0
        )
        self.provider_upload_wait_seconds = max(0.1, configured_wait)
        self.provider_upload_poll_seconds = max(0.01, provider_upload_poll_seconds)

    @staticmethod
    def _expired(value: datetime | None, *, now: datetime) -> bool:
        if value is None:
            return True
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return normalized <= now

    def _new_upload_expiry(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self.provider_upload_claim_seconds)

    def _mark_upload_reconciliation(self, binding_id: str, claim_token: str) -> bool:
        """Fence a known upload owner after the provider-call boundary was crossed."""

        with self.database.session() as session:
            result = session.execute(
                update(MediaProviderBinding)
                .where(
                    MediaProviderBinding.id == binding_id,
                    MediaProviderBinding.status == "UPLOADING",
                    MediaProviderBinding.upload_claim_token == claim_token,
                )
                .values(status="NEEDS_RECONCILIATION", updated_at=utcnow())
            )
            return affected_rows(result) == 1

    def _expire_pre_boundary_claim(self, binding_id: str, claim_token: str) -> bool:
        """Make a failed local preparation claim immediately safe to take over."""

        with self.database.session() as session:
            result = session.execute(
                update(MediaProviderBinding)
                .where(
                    MediaProviderBinding.id == binding_id,
                    MediaProviderBinding.status == "UPLOAD_CLAIMED",
                    MediaProviderBinding.upload_claim_token == claim_token,
                    MediaProviderBinding.upload_started_at.is_(None),
                )
                .values(upload_claim_expires_at=utcnow(), updated_at=utcnow())
            )
            return affected_rows(result) == 1

    @staticmethod
    def _host_matches(host: str, pattern: str) -> bool:
        normalized = pattern.strip().lower().rstrip(".")
        if normalized.startswith("*."):
            suffix = normalized[1:]
            return host.endswith(suffix) and host != suffix[1:]
        return host == normalized

    async def _validate_remote_url(self, url: str, *, provider: str) -> None:
        try:
            parsed = urlsplit(url)
            port = parsed.port or 443
        except ValueError as exc:
            raise RemoteMediaSecurityError("provider media URL is malformed") from exc
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise RemoteMediaSecurityError("provider media URL must use HTTPS")
        if parsed.username or parsed.password or port != 443:
            raise RemoteMediaSecurityError("provider media URL contains forbidden authority fields")
        host = parsed.hostname.lower().rstrip(".")
        patterns = self.provider_media_hosts.get(provider, ())
        if not patterns or not any(self._host_matches(host, pattern) for pattern in patterns):
            # Name the host. The generation that hits this has already been paid
            # for, and without the host the operator is left re-running a billed
            # call to learn a string the provider already told us. A hostname
            # from a provider response is untrusted, so it is bounded before it
            # reaches a log line.
            raise RemoteMediaSecurityError(
                f"provider media host is not allowlisted: {provider} returned {host[:120]!r}; "
                "add it to PROVIDER_MEDIA_ALLOWED_HOSTS if it is the provider's own CDN"
            )

        try:
            addresses = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise RemoteMediaSecurityError("provider media host could not be resolved") from exc
        if not addresses:
            raise RemoteMediaSecurityError("provider media host resolved to no addresses")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address[4][0])
            except ValueError as exc:
                raise RemoteMediaSecurityError("provider media host returned an invalid address") from exc
            if not ip.is_global:
                raise RemoteMediaSecurityError("provider media host resolved to a non-public address")

    @staticmethod
    def _validate_connected_peer(response: httpx.Response) -> None:
        stream = response.extensions.get("network_stream")
        if stream is None or not hasattr(stream, "get_extra_info"):
            return
        peer = stream.get_extra_info("server_addr")
        if not peer:
            return
        try:
            ip = ipaddress.ip_address(peer[0] if isinstance(peer, tuple) else peer)
        except ValueError as exc:
            raise RemoteMediaSecurityError("provider media peer address is invalid") from exc
        if not ip.is_global:
            raise RemoteMediaSecurityError("provider media connection reached a non-public address")

    @staticmethod
    def _lineage_key(
        *,
        character_id: str | None,
        scene_id: str | None,
        shot_id: str | None,
        parent_asset_id: str | None,
        generation_candidate_id: str | None,
    ) -> str:
        return lineage_key(
            character_id=character_id,
            scene_id=scene_id,
            shot_id=shot_id,
            parent_asset_id=parent_asset_id,
            generation_candidate_id=generation_candidate_id,
        )

    @staticmethod
    def _image_dimensions(path: str, mime_type: str) -> tuple[int | None, int | None]:
        if not mime_type.startswith("image/"):
            return None, None
        try:
            with Image.open(path) as image:
                return image.width, image.height
        except Exception:
            return None, None

    @staticmethod
    def _video_duration(path: str, mime_type: str) -> float | None:
        if not mime_type.startswith("video/"):
            return None
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=nw=1:nk=1",
                    path,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            return float(result.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def register(
        self,
        project_id: str,
        asset_type: str,
        stream: BinaryIO,
        *,
        filename: str,
        mime_type: str | None = None,
        character_id: str | None = None,
        scene_id: str | None = None,
        shot_id: str | None = None,
        parent_asset_id: str | None = None,
        generation_candidate_id: str | None = None,
        metadata: dict | None = None,
        provider: str | None = None,
        provider_media_id: str | None = None,
    ) -> tuple[MediaAsset, bool]:
        if bool(provider) != bool(provider_media_id):
            raise ValueError("provider origin requires both provider and provider_media_id")
        stored = self.storage.put(stream, filename=filename, mime_type=mime_type)
        lineage_key = self._lineage_key(
            character_id=character_id,
            scene_id=scene_id,
            shot_id=shot_id,
            parent_asset_id=parent_asset_id,
            generation_candidate_id=generation_candidate_id,
        )
        with self.database.session() as session:
            width, height = self._image_dimensions(stored.local_path, stored.mime_type)
            asset = MediaAsset(
                project_id=project_id,
                asset_type=asset_type,
                sha256=stored.sha256,
                lineage_key=lineage_key,
                storage_key=stored.key,
                local_path=stored.local_path,
                public_url=stored.public_url,
                mime_type=stored.mime_type,
                size_bytes=stored.size,
                width=width,
                height=height,
                duration=self._video_duration(stored.local_path, stored.mime_type),
                character_id=character_id,
                scene_id=scene_id,
                shot_id=shot_id,
                parent_asset_id=parent_asset_id,
                generation_candidate_id=generation_candidate_id,
                provider=provider,
                provider_media_id=provider_media_id,
                metadata_json=metadata or {},
            )
            try:
                with session.begin_nested():
                    session.add(asset)
                    session.flush()
            except IntegrityError:
                winner = session.scalar(
                    select(MediaAsset).where(
                        MediaAsset.project_id == project_id,
                        MediaAsset.sha256 == stored.sha256,
                        MediaAsset.asset_type == asset_type,
                        MediaAsset.lineage_key == lineage_key,
                    )
                )
                if winner is None:
                    raise
                return winner, True
            return asset, False

    def get(self, asset_id: str) -> MediaAsset | None:
        with self.database.session() as session:
            return session.get(MediaAsset, asset_id)

    def _provider_binding_context(self, binding_id: str) -> _ProviderMediaBindingContext:
        with self.database.session() as session:
            binding = session.get(MediaProviderBinding, binding_id)
            if binding is None:
                raise LookupError(f"provider media binding not found: {binding_id}")
            asset = session.get(MediaAsset, binding.asset_id)
            account = session.get(ProviderAccount, binding.account_id)
            if asset is None or account is None:
                raise ProviderMediaReconciliationConflict(
                    "provider media binding has incomplete local ownership records"
                )
            if account.provider != binding.provider:
                raise ProviderMediaReconciliationConflict(
                    "provider media binding account does not match its provider"
                )
            return _ProviderMediaBindingContext(
                binding_id=binding.id,
                asset_id=asset.id,
                project_id=asset.project_id,
                provider=binding.provider,
                account_id=binding.account_id,
                worker_id=account.worker_id,
                status=binding.status,
                provider_media_id=binding.provider_media_id,
                claim_token=binding.upload_claim_token,
            )

    @staticmethod
    def _reconciliation_result(
        context: _ProviderMediaBindingContext,
        *,
        status: str,
        provider_media_id: str | None,
        action: str,
        replayed: bool = False,
    ) -> ProviderMediaReconciliationResult:
        return ProviderMediaReconciliationResult(
            binding_id=context.binding_id,
            asset_id=context.asset_id,
            project_id=context.project_id,
            provider=context.provider,
            account_id=context.account_id,
            status=status,
            provider_media_id=provider_media_id,
            action=action,
            replayed=replayed,
        )

    @staticmethod
    def _reconciliation_audit(
        context: _ProviderMediaBindingContext,
        *,
        action: str,
        reason: str,
        provider_media_id: str | None,
    ) -> DecisionRecord:
        return DecisionRecord(
            project_id=context.project_id,
            shot_id=None,
            decision_type="PROVIDER_MEDIA_RECONCILIATION",
            input_features={
                "binding_id": context.binding_id,
                "asset_id": context.asset_id,
                "project_id": context.project_id,
                "provider": context.provider,
                "account_id": context.account_id,
                "action": action,
                "reason": reason,
                "server_actor": "PLATFORM_API_KEY",
                "provider_media_id": provider_media_id,
            },
            selected_action=action,
            reason_codes=["EXPLICIT_INTERNAL_RECONCILIATION"],
            model_version="provider-validation-v1",
            policy_version="provider-media-reconciliation-v1",
        )

    async def reconcile_provider_media(
        self,
        binding_id: str,
        provider: GenerationProvider,
        *,
        action: str,
        provider_media_id: str | None,
        reason: str,
        explicit_confirmation: bool,
    ) -> ProviderMediaReconciliationResult:
        """Resolve an uncertain paid upload through one explicit internal action.

        This method never retries ``upload_asset``. It either validates a known remote
        identifier or records an operator's explicit confirmation that no remote asset
        was created, after which the normal resolver may establish a new safe claim.
        """

        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("a non-empty reconciliation reason is required")
        if action not in {"SET_KNOWN_MEDIA_ID", "CONFIRM_REMOTE_NOT_CREATED"}:
            raise ValueError(f"unsupported provider media reconciliation action: {action}")

        normalized_media_id = provider_media_id.strip() if provider_media_id else None
        if action == "SET_KNOWN_MEDIA_ID" and not normalized_media_id:
            raise ValueError("provider_media_id is required for SET_KNOWN_MEDIA_ID")
        if action == "CONFIRM_REMOTE_NOT_CREATED":
            if not explicit_confirmation:
                raise ValueError("explicit confirmation is required before allowing a new upload")
            if normalized_media_id is not None:
                raise ValueError("provider_media_id is not allowed when confirming no remote creation")

        context = self._provider_binding_context(binding_id)
        if provider.name != context.provider:
            raise ProviderMediaReconciliationConflict(
                "configured provider does not match the uncertain media binding"
            )

        if action == "SET_KNOWN_MEDIA_ID":
            assert normalized_media_id is not None
            if context.status == "READY":
                if context.provider_media_id == normalized_media_id:
                    return self._reconciliation_result(
                        context,
                        status="READY",
                        provider_media_id=normalized_media_id,
                        action=action,
                        replayed=True,
                    )
                raise ProviderMediaReconciliationConflict(
                    "provider media binding is already READY with a different identifier"
                )
            if context.status != "NEEDS_RECONCILIATION":
                raise ProviderMediaReconciliationConflict(
                    f"provider media binding cannot be reconciled from {context.status}"
                )
            if not context.worker_id:
                raise ProviderMediaReconciliationConflict(
                    "provider account has no bound worker for remote media validation"
                )
            try:
                is_valid = await provider.validate_asset(
                    normalized_media_id,
                    account_id=context.account_id,
                    worker_id=context.worker_id,
                )
            except Exception as exc:
                raise ProviderMediaValidationFailed(
                    "provider validation failed; binding remains quarantined"
                ) from exc
            if not is_valid:
                raise ProviderMediaValidationFailed(
                    "provider rejected the supplied media identifier; binding remains quarantined"
                )

            reconciled_at = utcnow()
            with self.database.session() as session:
                result = session.execute(
                    update(MediaProviderBinding)
                    .where(
                        MediaProviderBinding.id == context.binding_id,
                        MediaProviderBinding.status == "NEEDS_RECONCILIATION",
                        MediaProviderBinding.provider == context.provider,
                        MediaProviderBinding.account_id == context.account_id,
                        MediaProviderBinding.asset_id == context.asset_id,
                        MediaProviderBinding.upload_claim_token == context.claim_token,
                        MediaProviderBinding.provider_media_id == context.provider_media_id,
                    )
                    .values(
                        provider_media_id=normalized_media_id,
                        status="READY",
                        last_validated_at=reconciled_at,
                        upload_claim_token=None,
                        upload_claim_expires_at=None,
                        upload_started_at=None,
                        updated_at=reconciled_at,
                    )
                )
                if affected_rows(result) == 1:
                    asset = session.get(MediaAsset, context.asset_id)
                    if asset is None or asset.project_id != context.project_id:
                        raise ProviderMediaReconciliationConflict(
                            "media asset changed during provider reconciliation"
                        )
                    session.add(
                        self._reconciliation_audit(
                            context,
                            action=action,
                            reason=normalized_reason,
                            provider_media_id=normalized_media_id,
                        )
                    )
                    return self._reconciliation_result(
                        context,
                        status="READY",
                        provider_media_id=normalized_media_id,
                        action=action,
                    )

            current = self._provider_binding_context(binding_id)
            if current.status == "READY" and current.provider_media_id == normalized_media_id:
                return self._reconciliation_result(
                    current,
                    status="READY",
                    provider_media_id=normalized_media_id,
                    action=action,
                    replayed=True,
                )
            raise ProviderMediaReconciliationConflict(
                "provider media binding changed while its remote identifier was being validated"
            )

        if context.status != "NEEDS_RECONCILIATION":
            raise ProviderMediaReconciliationConflict(
                f"provider media binding cannot be cleared from {context.status}"
            )
        released_at = utcnow()
        with self.database.session() as session:
            result = session.execute(
                update(MediaProviderBinding)
                .where(
                    MediaProviderBinding.id == context.binding_id,
                    MediaProviderBinding.status == "NEEDS_RECONCILIATION",
                    MediaProviderBinding.provider == context.provider,
                    MediaProviderBinding.account_id == context.account_id,
                    MediaProviderBinding.asset_id == context.asset_id,
                    MediaProviderBinding.upload_claim_token == context.claim_token,
                    MediaProviderBinding.provider_media_id == context.provider_media_id,
                )
                .values(
                    provider_media_id=None,
                    status="UPLOAD_CLAIMED",
                    last_validated_at=None,
                    upload_claim_token=None,
                    upload_claim_expires_at=released_at,
                    upload_started_at=None,
                    updated_at=released_at,
                )
            )
            if affected_rows(result) != 1:
                raise ProviderMediaReconciliationConflict(
                    "provider media binding changed before the operator confirmation was recorded"
                )
            session.add(
                self._reconciliation_audit(
                    context,
                    action=action,
                    reason=normalized_reason,
                    provider_media_id=None,
                )
            )
            return self._reconciliation_result(
                context,
                status="UPLOAD_CLAIMED",
                provider_media_id=None,
                action=action,
            )

    def reference_url(
        self,
        asset_id: str,
        *,
        project_id: str,
        provider: str,
        require_https: bool,
        constraints: ProviderReferenceConstraints | None = None,
    ) -> str:
        """Resolve one fetchable reference URL for a provider that never ingests uploads.

        Two things happen here, and both exist to keep the application out of the
        media path:

        1. The encoding the provider gets is chosen against its declared
           constraints. The user's original is never re-encoded to satisfy a
           provider; a derived rendition is, and only when the original does not
           already fit.
        2. The URL is a short-lived credential issued by *object storage*, not a
           route on this service. The provider fetches the bytes directly. When
           the backend cannot issue one this fails closed before the submission
           boundary rather than falling back to streaming the object through the
           API, which is how a control plane becomes an image CDN.
        """

        bounds = constraints or ProviderReferenceConstraints()
        with self.database.session() as session:
            asset = session.get(MediaAsset, asset_id)
            if asset is None:
                raise LookupError(f"media asset not found: {asset_id}")
            if asset.project_id != project_id:
                raise LookupError("media asset does not belong to the generation project")
            if asset.verification_status != "READY":
                # A provider call is billed on submission; an unverified or
                # rejected file must never reach one. Directly uploaded
                # assets become READY only after the async full-content
                # verification passes.
                raise ProviderReferenceUrlUnavailable(
                    f"media asset {asset_id} is not verified for provider use "
                    f"(MEDIA_NOT_VERIFIED:{asset.verification_status})"
                )
            try:
                rendition = self.renditions.resolve(session, asset, bounds)
            except RenditionDerivationFailed as exc:
                raise ProviderReferenceUrlUnavailable(
                    f"{provider} cannot be given a usable reference for media asset "
                    f"{asset_id}: {exc}"
                ) from exc
            session.flush()

        reference = (
            self.storage.presigned_reference_url(
                rendition.storage_key,
                expires_in=self.reference_url_ttl_seconds,
                mime_type=rendition.mime_type,
            )
            or ""
        ).strip()
        if not reference:
            raise ProviderReferenceUrlUnavailable(
                f"{provider} requires a fetchable reference URL but the storage backend "
                f"cannot issue one for media asset {asset_id}. Configure S3-compatible "
                f"storage so the provider fetches from object storage directly; the API "
                f"must not proxy reference media."
            )
        scheme = urlsplit(reference).scheme.lower()
        if scheme not in {"http", "https"}:
            raise ProviderReferenceUrlUnavailable(
                f"{provider} reference URL for media asset {asset_id} is not an http(s) URL"
            )
        if require_https and scheme != "https":
            raise ProviderReferenceUrlUnavailable(
                f"{provider} cannot fetch the non-HTTPS reference URL for media asset {asset_id}"
            )
        return reference

    async def resolve_provider_media(
        self,
        asset_id: str,
        provider: GenerationProvider,
        *,
        project_id: str,
        account_id: str,
        worker_id: str,
        provider_project_id: str | None = None,
        on_paid_boundary: Callable[[Session, str, str], None] | None = None,
    ) -> tuple[str, bool]:
        """Resolve one durable provider upload for an asset/provider/account tuple.

        The database claim is deliberately split into a pre-boundary ``UPLOAD_CLAIMED``
        state and a post-boundary ``UPLOADING`` state. An expired pre-boundary claim can
        be taken over safely. Once ``upload_asset`` may have reached a paid provider, an
        exception or expired lease is fail-closed as ``NEEDS_RECONCILIATION`` instead of
        risking a second charge.
        """

        with self.database.session() as session:
            asset = session.get(MediaAsset, asset_id)
            if asset is None:
                raise LookupError(f"media asset not found: {asset_id}")
            if asset.project_id != project_id:
                raise LookupError("media asset does not belong to the generation project")
            local_path = asset.local_path
            mime_type = asset.mime_type
            storage_key = asset.storage_key

        wait_deadline = time.monotonic() + self.provider_upload_wait_seconds
        claim_token: str | None = None
        binding_id: str | None = None

        while claim_token is None:
            now = utcnow()
            claim_expiry = self._new_upload_expiry(now)
            ready_binding: tuple[str, str] | None = None
            should_wait = False
            reconciliation_reason: str | None = None

            with self.database.session() as session:
                binding = session.scalar(
                    select(MediaProviderBinding).where(
                        MediaProviderBinding.asset_id == asset_id,
                        MediaProviderBinding.provider == provider.name,
                        MediaProviderBinding.account_id == account_id,
                    )
                )
                if binding is None:
                    candidate_token = new_id()
                    candidate = MediaProviderBinding(
                        asset_id=asset_id,
                        provider=provider.name,
                        account_id=account_id,
                        provider_media_id=None,
                        status="UPLOAD_CLAIMED",
                        upload_claim_token=candidate_token,
                        upload_claim_expires_at=claim_expiry,
                        upload_started_at=None,
                    )
                    try:
                        with session.begin_nested():
                            session.add(candidate)
                            session.flush()
                    except IntegrityError:
                        should_wait = True
                    else:
                        binding_id = candidate.id
                        claim_token = candidate_token
                elif binding.status == "READY" and binding.provider_media_id:
                    ready_binding = (binding.id, binding.provider_media_id)
                elif binding.status == "UPLOAD_CLAIMED":
                    if self._expired(binding.upload_claim_expires_at, now=now):
                        if binding.upload_started_at is not None:
                            result = session.execute(
                                update(MediaProviderBinding)
                                .where(
                                    MediaProviderBinding.id == binding.id,
                                    MediaProviderBinding.status == "UPLOAD_CLAIMED",
                                    MediaProviderBinding.upload_claim_token == binding.upload_claim_token,
                                    MediaProviderBinding.upload_started_at.is_not(None),
                                )
                                .values(status="NEEDS_RECONCILIATION", updated_at=now)
                            )
                            if affected_rows(result) == 1:
                                reconciliation_reason = (
                                    "provider upload lease expired after the paid-call boundary"
                                )
                            else:
                                should_wait = True
                        else:
                            candidate_token = new_id()
                            result = session.execute(
                                update(MediaProviderBinding)
                                .where(
                                    MediaProviderBinding.id == binding.id,
                                    MediaProviderBinding.status == "UPLOAD_CLAIMED",
                                    MediaProviderBinding.upload_claim_token == binding.upload_claim_token,
                                    MediaProviderBinding.upload_started_at.is_(None),
                                )
                                .values(
                                    provider_media_id=None,
                                    upload_claim_token=candidate_token,
                                    upload_claim_expires_at=claim_expiry,
                                    updated_at=now,
                                )
                            )
                            if affected_rows(result) == 1:
                                binding_id = binding.id
                                claim_token = candidate_token
                            else:
                                should_wait = True
                    else:
                        should_wait = True
                elif binding.status == "UPLOADING":
                    if self._expired(binding.upload_claim_expires_at, now=now):
                        result = session.execute(
                            update(MediaProviderBinding)
                            .where(
                                MediaProviderBinding.id == binding.id,
                                MediaProviderBinding.status == "UPLOADING",
                                MediaProviderBinding.upload_claim_token == binding.upload_claim_token,
                                MediaProviderBinding.upload_started_at.is_not(None),
                            )
                            .values(status="NEEDS_RECONCILIATION", updated_at=now)
                        )
                        if affected_rows(result) == 1:
                            reconciliation_reason = (
                                "provider upload lease expired after the paid-call boundary"
                            )
                        else:
                            should_wait = True
                    else:
                        should_wait = True
                elif binding.status == "NEEDS_RECONCILIATION":
                    raise ProviderMediaReconciliationRequired(
                        "provider upload outcome is uncertain and requires reconciliation"
                    )
                else:
                    raise ProviderMediaReconciliationRequired(
                        f"provider media binding is not safely reusable: {binding.status}"
                    )

            if reconciliation_reason is not None:
                raise ProviderMediaReconciliationRequired(reconciliation_reason)
            if claim_token is not None:
                break

            if ready_binding is not None:
                ready_binding_id, ready_media_id = ready_binding
                valid = await provider.validate_asset(
                    ready_media_id,
                    account_id=account_id,
                    worker_id=worker_id,
                )
                if valid:
                    with self.database.session() as session:
                        result = session.execute(
                            update(MediaProviderBinding)
                            .where(
                                MediaProviderBinding.id == ready_binding_id,
                                MediaProviderBinding.status == "READY",
                                MediaProviderBinding.provider_media_id == ready_media_id,
                            )
                            .values(last_validated_at=utcnow(), updated_at=utcnow())
                        )
                    if affected_rows(result) == 1:
                        return ready_media_id, True
                    continue

                candidate_token = new_id()
                invalidated_at = utcnow()
                with self.database.session() as session:
                    result = session.execute(
                        update(MediaProviderBinding)
                        .where(
                            MediaProviderBinding.id == ready_binding_id,
                            MediaProviderBinding.status == "READY",
                            MediaProviderBinding.provider_media_id == ready_media_id,
                        )
                        .values(
                            provider_media_id=None,
                            status="UPLOAD_CLAIMED",
                            upload_claim_token=candidate_token,
                            upload_claim_expires_at=self._new_upload_expiry(invalidated_at),
                            upload_started_at=None,
                            updated_at=invalidated_at,
                        )
                    )
                if affected_rows(result) == 1:
                    binding_id = ready_binding_id
                    claim_token = candidate_token
                    break
                continue

            if should_wait:
                if time.monotonic() >= wait_deadline:
                    raise ProviderMediaUploadInProgress(
                        "provider media upload is still owned by another worker"
                    )
                await asyncio.sleep(self.provider_upload_poll_seconds)

        if binding_id is None or claim_token is None:  # pragma: no cover - loop invariant.
            raise RuntimeError("provider media upload claim was not established")

        try:
            path = local_path or str(self.storage.path_for(storage_key))
            upload_request = {
                "asset_id": asset_id,
                "local_path": path,
                "mime_type": mime_type,
                "_provider_project_id": provider_project_id,
            }
        except BaseException:
            try:
                self._expire_pre_boundary_claim(binding_id, claim_token)
            except Exception:
                pass
            raise

        boundary_at = utcnow()
        try:
            with self.database.session() as session:
                result = session.execute(
                    update(MediaProviderBinding)
                    .where(
                        MediaProviderBinding.id == binding_id,
                        MediaProviderBinding.status == "UPLOAD_CLAIMED",
                        MediaProviderBinding.upload_claim_token == claim_token,
                        MediaProviderBinding.upload_started_at.is_(None),
                    )
                    .values(
                        status="UPLOADING",
                        upload_started_at=boundary_at,
                        upload_claim_expires_at=self._new_upload_expiry(boundary_at),
                        updated_at=boundary_at,
                    )
                )
                if affected_rows(result) == 1 and on_paid_boundary is not None:
                    # The binding and its owning generation job must cross the
                    # paid boundary in one commit. If the hook rejects a stale
                    # job claim, this transaction rolls back before upload.
                    on_paid_boundary(session, binding_id, asset_id)
        except BaseException:
            try:
                self._expire_pre_boundary_claim(binding_id, claim_token)
            except Exception:
                pass
            raise
        if affected_rows(result) != 1:
            raise ProviderMediaUploadInProgress("provider media upload claim was superseded")

        try:
            provider_media_id = await provider.upload_asset(
                upload_request,
                account_id=account_id,
                worker_id=worker_id,
            )
            if not isinstance(provider_media_id, str) or not provider_media_id.strip():
                raise ProviderMediaReconciliationRequired(
                    "provider upload returned no durable media identifier"
                )
            completed_at = utcnow()
            with self.database.session() as session:
                result = session.execute(
                    update(MediaProviderBinding)
                    .where(
                        MediaProviderBinding.id == binding_id,
                        MediaProviderBinding.status == "UPLOADING",
                        MediaProviderBinding.upload_claim_token == claim_token,
                    )
                    .values(
                        provider_media_id=provider_media_id,
                        status="READY",
                        last_validated_at=completed_at,
                        upload_claim_token=None,
                        upload_claim_expires_at=None,
                        updated_at=completed_at,
                    )
                )
                if affected_rows(result) != 1:
                    raise ProviderMediaReconciliationRequired(
                        "provider upload completed after its fenced claim was superseded"
                    )
                current_asset = session.get(MediaAsset, asset_id)
                if current_asset is None:
                    raise ProviderMediaReconciliationRequired(
                        "provider upload completed after its local asset disappeared"
                    )
        except BaseException:
            try:
                self._mark_upload_reconciliation(binding_id, claim_token)
            except Exception:
                # UPLOADING + upload_started_at remains fail-closed. A later observer
                # will move the expired fenced claim to NEEDS_RECONCILIATION.
                pass
            raise
        return provider_media_id, False

    # Extensions the platform accepts for provider-returned raster output. The
    # suffix must agree with the MIME type or upload validation rejects it.
    _INLINE_IMAGE_EXTENSIONS = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }

    def find_by_content(
        self,
        *,
        project_id: str,
        sha256: str,
        asset_type: str,
        lineage_key: str,
    ) -> MediaAsset | None:
        """The existing asset for this exact content, if the project already holds it."""

        with self.database.session() as session:
            return session.scalar(
                select(MediaAsset).where(
                    MediaAsset.project_id == project_id,
                    MediaAsset.sha256 == sha256,
                    MediaAsset.asset_type == asset_type,
                    MediaAsset.lineage_key == lineage_key,
                )
            )

    def adopt_stored_object(
        self,
        project_id: str,
        asset_type: str,
        storage_key: str,
        *,
        sha256: str,
        mime_type: str,
        size_bytes: int,
        shot_id: str | None = None,
        character_id: str | None = None,
        metadata: dict | None = None,
        lineage_key: str | None = None,
    ) -> tuple[MediaAsset, bool]:
        with self.database.session() as session:
            return self.adopt_stored_object_in(
                session,
                project_id,
                asset_type,
                storage_key,
                sha256=sha256,
                mime_type=mime_type,
                size_bytes=size_bytes,
                shot_id=shot_id,
                character_id=character_id,
                metadata=metadata,
                lineage_key=lineage_key,
            )

    def adopt_stored_object_in(
        self,
        session: Session,
        project_id: str,
        asset_type: str,
        storage_key: str,
        *,
        sha256: str,
        mime_type: str,
        size_bytes: int,
        shot_id: str | None = None,
        character_id: str | None = None,
        metadata: dict | None = None,
        lineage_key: str | None = None,
    ) -> tuple[MediaAsset, bool]:
        """Register an object the client already uploaded straight to storage.

        Deliberately not a `put`: the bytes are in the bucket and must stay
        there. This only records what they are. Dimensions are read from a
        bounded header prefix rather than by opening the whole object, for the
        same reason the upload skipped this process in the first place.

        ``lineage_key`` is passed by the direct-upload path rather than
        recomputed: the authorization already decided it, wrote it to the
        upload row, and deduplicated against it. Deriving it a second time here
        made dedupe depend on two formulas agreeing.

        The session is the caller's, so registering the asset, completing the
        upload row and settling the quota hold commit as one transaction.
        """

        width, height = self._dimensions_from_header(storage_key, mime_type)
        resolved_lineage = (
            lineage_key
            if lineage_key is not None
            else self._lineage_key(
                character_id=character_id,
                scene_id=None,
                shot_id=shot_id,
                parent_asset_id=None,
                generation_candidate_id=None,
            )
        )
        # Deliberately no SAVEPOINT here. This runs inside a transaction the
        # caller also uses to complete the upload row and settle the quota hold,
        # and under pysqlite a `begin_nested()` insert survives a rollback of
        # the enclosing transaction — which would leave exactly the half-
        # committed state the single transaction exists to prevent. A read
        # before the insert covers the duplicate; a genuine collision in the
        # window between them rolls the whole completion back, and the retry
        # finds the winner on this same read.
        existing = session.scalar(
            select(MediaAsset).where(
                MediaAsset.project_id == project_id,
                MediaAsset.sha256 == sha256,
                MediaAsset.asset_type == asset_type,
                MediaAsset.lineage_key == resolved_lineage,
            )
        )
        if existing is not None:
            return existing, True
        asset = MediaAsset(
            project_id=project_id,
            asset_type=asset_type,
            sha256=sha256,
            lineage_key=resolved_lineage,
            storage_key=storage_key,
            local_path=None,
            public_url=None,
            mime_type=mime_type,
            size_bytes=size_bytes,
            width=width,
            height=height,
            duration=None,
            # Adopted from a HEAD and a 64 KB header, not a full decode: the
            # asset is registered but not READY until the asynchronous
            # verification worker decodes the complete object. Providers and
            # build chains refuse it meanwhile.
            verification_status="PENDING_VERIFICATION",
            shot_id=shot_id,
            character_id=character_id,
            metadata_json={"source": "direct_upload", **(metadata or {})},
        )
        session.add(asset)
        session.flush()
        return asset, False

    def _dimensions_from_header(self, storage_key: str, mime_type: str) -> tuple[int | None, int | None]:
        if not mime_type.startswith("image/"):
            return None, None
        try:
            header = self.storage.read_prefix(storage_key, MEDIA_HEADER_BYTES)
            with Image.open(io.BytesIO(header)) as image:
                return image.width, image.height
        except Exception:
            return None, None

    def stage_inline_provider_output(
        self,
        content: bytes,
        *,
        key_prefix: str,
        index: int,
        stem: str,
        mime_type: str,
        asset_type: str,
    ) -> StagedProviderOutput:
        """Validate and stage one artefact a provider returned inside its response body.

        Synchronous image APIs answer with bytes, so there is no URL to fetch.
        The bytes still pass the same content validation a downloaded artefact
        does — a provider is not a trusted source of decodable media — and are
        then written to their deterministic staging slot. No database row is
        touched here; adoption belongs to the completion transaction.
        """

        normalized_mime = (mime_type or "").split(";", 1)[0].strip().lower()
        extension = self._INLINE_IMAGE_EXTENSIONS.get(normalized_mime)
        if extension is None:
            raise RemoteMediaSecurityError(f"provider returned an unsupported media type: {mime_type}")
        if len(content) > self.max_download_bytes:
            raise StorageLimitExceeded(self.max_download_bytes)
        with tempfile.SpooledTemporaryFile(max_size=min(self.max_download_bytes, 8 * 1024 * 1024)) as buffer:
            buffer.write(content)
            buffer.seek(0)
            return self._validate_and_stage(
                cast(BinaryIO, buffer),
                key_prefix=key_prefix,
                index=index,
                filename=f"{stem}.{extension}",
                declared_mime=normalized_mime,
                asset_type=asset_type,
            )

    def _validate_and_stage(
        self,
        stream: BinaryIO,
        *,
        key_prefix: str,
        index: int,
        filename: str,
        declared_mime: str,
        asset_type: str,
        source_url: str | None = None,
    ) -> StagedProviderOutput:
        try:
            validated = validate_user_media_upload(
                stream,
                filename=filename,
                declared_mime=declared_mime,
                asset_type=asset_type,
                max_bytes=self.max_download_bytes,
                max_image_pixels=self.max_image_pixels,
            )
        except UnsafeMediaUpload as exc:
            raise RemoteMediaSecurityError(str(exc)) from exc
        stream.seek(0)
        stored = self.storage.put_exact(
            stream,
            key=f"{key_prefix}{index:02d}{validated.extension}",
            mime_type=validated.mime_type,
        )
        width, height = self._image_dimensions(stored.local_path, stored.mime_type)
        return StagedProviderOutput(
            storage_key=stored.key,
            sha256=stored.sha256,
            size_bytes=stored.size,
            mime_type=stored.mime_type,
            local_path=stored.local_path,
            public_url=stored.public_url,
            width=width,
            height=height,
            duration=self._video_duration(stored.local_path, stored.mime_type),
            source_url=source_url,
        )

    def adopt_staged_output_in(
        self,
        session: Session,
        project_id: str,
        asset_type: str,
        staged: StagedProviderOutput,
        *,
        provider: str,
        provider_media_id: str,
        shot_id: str | None = None,
        generation_candidate_id: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[MediaAsset, bool]:
        """Register one staged provider artefact inside the caller's transaction.

        The bytes are already validated and sitting at their staging key; this
        records what they are, in the same transaction that creates the
        candidate the asset belongs to and completes the job that paid for it.
        The staging key is adopted in place — nothing is copied — so a rolled
        back transaction leaves only unreferenced staging objects behind, which
        is exactly what the TTL sweeper is allowed to reclaim.

        Deliberately no SAVEPOINT, for the same reason as
        ``adopt_stored_object_in``: under pysqlite a ``begin_nested()`` insert
        survives a rollback of the enclosing transaction. A read before the
        insert covers the duplicate; a genuine collision rolls the whole
        completion back and the retry finds the winner on this same read.
        """

        lineage = self._lineage_key(
            character_id=None,
            scene_id=None,
            shot_id=shot_id,
            parent_asset_id=None,
            generation_candidate_id=generation_candidate_id,
        )
        existing = session.scalar(
            select(MediaAsset).where(
                MediaAsset.project_id == project_id,
                MediaAsset.sha256 == staged.sha256,
                MediaAsset.asset_type == asset_type,
                MediaAsset.lineage_key == lineage,
            )
        )
        if existing is not None:
            return existing, True
        source_detail = {"source_url": staged.source_url} if staged.source_url else {}
        asset = MediaAsset(
            project_id=project_id,
            asset_type=asset_type,
            sha256=staged.sha256,
            lineage_key=lineage,
            storage_key=staged.storage_key,
            local_path=staged.local_path,
            public_url=staged.public_url,
            mime_type=staged.mime_type,
            size_bytes=staged.size_bytes,
            width=staged.width,
            height=staged.height,
            duration=staged.duration,
            shot_id=shot_id,
            generation_candidate_id=generation_candidate_id,
            provider=provider,
            provider_media_id=provider_media_id,
            metadata_json={
                "source": "provider_output",
                "provider": provider,
                **source_detail,
                **(metadata or {}),
            },
        )
        session.add(asset)
        session.flush([asset])
        return asset, False

    async def _download_provider_media(self, content: BinaryIO, url: str, *, provider: str) -> str:
        """Stream one provider artefact into ``content`` behind the SSRF boundary.

        Returns the response's declared MIME type. Every hop of a redirect
        chain is re-validated against the provider's allowlisted hosts, and the
        byte count is bounded while streaming rather than trusted from a
        header.
        """

        current_url = url
        mime_type = "application/octet-stream"
        # The host the provider's own API named. A credential is presented only
        # here: a redirect commonly lands on a signed CDN that needs no
        # authorization, and forwarding a bearer token across hosts is how
        # credentials leak. Same-host redirects keep it, cross-host drop it --
        # the rule curl applies, made explicit because redirects are followed by
        # hand here.
        origin_host = (urlsplit(url).hostname or "").lower().rstrip(".")
        credential = self.provider_media_credentials.get(provider, "").strip()
        async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
            for redirect_count in range(6):
                await self._validate_remote_url(current_url, provider=provider)
                headers: dict[str, str] = {}
                if credential:
                    hop_host = (urlsplit(current_url).hostname or "").lower().rstrip(".")
                    if hop_host == origin_host:
                        headers["Authorization"] = f"Bearer {credential}"
                async with client.stream("GET", current_url, headers=headers) as response:
                    self._validate_connected_peer(response)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or redirect_count == 5:
                            raise RemoteMediaSecurityError(
                                "provider media redirect is missing or exceeds the redirect limit"
                            )
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    mime_type = response.headers.get("content-type", "application/octet-stream").split(
                        ";", 1
                    )[0]
                    declared_size = response.headers.get("content-length")
                    if declared_size:
                        try:
                            parsed_size = int(declared_size)
                        except ValueError as exc:
                            raise RemoteMediaSecurityError(
                                "provider media returned an invalid Content-Length"
                            ) from exc
                        if parsed_size > self.max_download_bytes:
                            raise StorageLimitExceeded(self.max_download_bytes)
                    downloaded = 0
                    async for chunk in response.aiter_bytes():
                        downloaded += len(chunk)
                        if downloaded > self.max_download_bytes:
                            raise StorageLimitExceeded(self.max_download_bytes)
                        content.write(chunk)
                    break
            else:  # pragma: no cover - loop exits via redirect limit branch.
                raise RemoteMediaSecurityError("provider media redirect limit exceeded")
        return mime_type

    async def download_provider_output_to_staging(
        self,
        url: str,
        *,
        key_prefix: str,
        index: int,
        filename: str,
        provider: str,
        asset_type: str,
    ) -> StagedProviderOutput:
        """Fetch one provider artefact and stage it; no database row is written.

        The download crosses the same SSRF boundary and content validation as
        ``download_and_register``, but lands on the deterministic staging key
        instead of in the media plane — adoption belongs to the completion
        transaction, so a crash after this call leaves only a recyclable
        staging object.
        """

        source_parts = urlsplit(url)
        source_url = urlunsplit((source_parts.scheme, source_parts.netloc, source_parts.path, "", ""))
        with tempfile.SpooledTemporaryFile(max_size=min(self.max_download_bytes, 8 * 1024 * 1024)) as content:
            binary_content = cast(BinaryIO, content)
            mime_type = await self._download_provider_media(binary_content, url, provider=provider)
            binary_content.seek(0)
            return self._validate_and_stage(
                binary_content,
                key_prefix=key_prefix,
                index=index,
                filename=filename,
                declared_mime=mime_type,
                asset_type=asset_type,
                source_url=source_url,
            )

    async def download_and_register(
        self,
        project_id: str,
        asset_type: str,
        url: str,
        *,
        filename: str,
        provider: str,
        provider_media_id: str,
        shot_id: str | None = None,
        generation_candidate_id: str | None = None,
    ) -> MediaAsset:
        current_url = url
        source_parts = urlsplit(url)
        source_url = urlunsplit((source_parts.scheme, source_parts.netloc, source_parts.path, "", ""))
        with tempfile.SpooledTemporaryFile(max_size=min(self.max_download_bytes, 8 * 1024 * 1024)) as content:
            binary_content = cast(BinaryIO, content)
            mime_type = await self._download_provider_media(binary_content, current_url, provider=provider)
            binary_content.seek(0)
            try:
                validated = validate_user_media_upload(
                    binary_content,
                    filename=filename,
                    declared_mime=mime_type,
                    asset_type=asset_type,
                    max_bytes=self.max_download_bytes,
                    max_image_pixels=self.max_image_pixels,
                )
            except UnsafeMediaUpload as exc:
                raise RemoteMediaSecurityError(str(exc)) from exc
            binary_content.seek(0)
            asset, _reused = self.register(
                project_id,
                asset_type,
                binary_content,
                filename=filename,
                mime_type=validated.mime_type,
                shot_id=shot_id,
                generation_candidate_id=generation_candidate_id,
                metadata={"source_url": source_url, "provider": provider},
                provider=provider,
                provider_media_id=provider_media_id,
            )
        # Provider origin is part of the insert itself.  A deduplicated result
        # returns the existing row unchanged, so neither a user upload nor a
        # prior generation origin can be relabelled by identical bytes.
        return asset
