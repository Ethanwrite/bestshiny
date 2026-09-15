"""Workspace connections: create, check, use and remove a workspace's own provider keys.

Ownership and exposure rules this service holds:

- A connection belongs to one workspace. Every read and every use names the
  workspace, and a connection of another workspace is indistinguishable from
  one that does not exist.
- The key is written once as Fernet ciphertext whose plaintext is prefixed with
  the row id, so ciphertext copied onto another row does not decrypt as that
  row's key. It is decrypted only to build a transport, never returned, and
  erased when the connection is deleted.
- A generation on a connection runs through the platform's generation gateway
  on a per-connection capacity resource (``byok://<id>``); this service builds
  the provider the gateway calls for it and answers whether a job may use it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from openrouter_provider import OpenRouterProvider
from platform_database import Database
from platform_shared import CredentialVault
from production_domain.models import (
    AccountStatus,
    BrowserWorker,
    Project,
    ProviderAccount,
    UserConnection,
    UserConnectionStatus,
    WorkerStatus,
    utcnow,
)
from provider_sdk import (
    GenerationProvider,
    ProviderJsonClient,
    RemoteMediaFetchPolicy,
)
from seedance_provider import ArkProvider
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from wan_provider import WanProvider

from .chat import (
    AnthropicChatClient,
    ChatClient,
    ChatResult,
    MockChatClient,
    OpenAICompatibleChatClient,
    RemoteModel,
)
from .egress import Resolver, host_of, normalize_base_url
from .errors import (
    ConnectionInvalid,
    ConnectionNotFound,
    ConnectionUnavailable,
    ProviderCallFailed,
)
from .generation import ConnectionGenerationProvider, MockGenerationProvider
from .protocols import (
    PROTOCOLS,
    USER_CONNECTION_PROVIDER,
    AuthStyle,
    ConnectionCapability,
    ConnectionProtocol,
)
from .transport import ClientFactory, ConnectionTransport

_RESOURCE_NAMESPACE = uuid.UUID("5d0b9a7e-31c4-5f7d-9a1b-6c2f0e8d4b37")
_MAX_MODELS = 100
#: `generation_jobs.model` is 120 characters, and a job carries the model id as given.
_MAX_MODEL_ID = 120
_ALL_CAPABILITIES = frozenset(capability.value for capability in ConnectionCapability)
#: Provider answers that are a verdict on the key itself (HTTP 401 and 403).
_KEY_REFUSED_CODES = frozenset({"CREDENTIAL_EXPIRED", "CONTENT_REJECTED"})


@dataclass(frozen=True)
class ConnectionView:
    id: str
    workspace_id: str
    name: str
    protocol: str
    protocol_label: str
    base_url: str
    capabilities: list[str]
    models: list[dict[str, str]]
    secret_hint: str
    status: str
    last_verified_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "protocol": self.protocol,
            "protocol_label": self.protocol_label,
            "base_url": self.base_url,
            "capabilities": self.capabilities,
            "models": self.models,
            "secret_hint": self.secret_hint,
            "status": self.status,
            "last_verified_at": self.last_verified_at.isoformat() if self.last_verified_at else None,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class ConnectionModelInput:
    id: str
    capability: str


def secret_hint(secret: str) -> str:
    value = secret.strip()
    if len(value) <= 8:
        return "••••"
    prefix = value[:3] if value[:3].isascii() else ""
    return f"{prefix}…{value[-4:]}"


class UserConnectionService:
    def __init__(
        self,
        database: Database,
        vault: CredentialVault,
        *,
        enabled: bool = True,
        allow_private_network: bool = False,
        http_timeout_seconds: float = 120,
        max_response_bytes: int = 64 * 1024 * 1024,
        max_concurrent_jobs: int = 4,
        mock_protocol_enabled: bool = False,
        resolver: Resolver | None = None,
        client_factory: ClientFactory | None = None,
        clock: Callable[[], datetime] = utcnow,
    ):
        if max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be positive")
        self.database = database
        self.vault = vault
        self.enabled = enabled
        self.allow_private_network = allow_private_network
        self.http_timeout_seconds = http_timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_concurrent_jobs = max_concurrent_jobs
        self.mock_protocol_enabled = mock_protocol_enabled
        self._resolver = resolver
        self._client_factory = client_factory
        self._clock = clock

    # ------------------------------------------------------------------ catalogue

    def available_protocols(self) -> list[ConnectionProtocol]:
        return [
            protocol
            for protocol in PROTOCOLS.values()
            if not protocol.development_only or self.mock_protocol_enabled
        ]

    def protocols_view(self) -> list[dict[str, Any]]:
        return [protocol.view() for protocol in self.available_protocols()]

    def _protocol(self, protocol_id: str) -> ConnectionProtocol:
        protocol = PROTOCOLS.get((protocol_id or "").strip())
        if protocol is None or (protocol.development_only and not self.mock_protocol_enabled):
            raise ConnectionInvalid(f"unknown connection protocol: {str(protocol_id)[:40]!r}")
        return protocol

    # ------------------------------------------------------------------ views

    @staticmethod
    def _view(row: UserConnection) -> ConnectionView:
        protocol = PROTOCOLS.get(row.protocol)
        return ConnectionView(
            id=row.id,
            workspace_id=row.workspace_id,
            name=row.name,
            protocol=row.protocol,
            protocol_label=protocol.label if protocol else row.protocol,
            base_url=row.base_url,
            capabilities=list(row.capabilities_json or []),
            models=[dict(model) for model in row.models_json or []],
            secret_hint=row.secret_hint,
            status=row.status,
            last_verified_at=row.last_verified_at,
            last_error=row.last_error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _live_row(session: Session, workspace_id: str, connection_id: str, *, for_update: bool = False):
        statement = select(UserConnection).where(
            UserConnection.id == connection_id,
            UserConnection.workspace_id == workspace_id,
            UserConnection.deleted_at.is_(None),
        )
        if for_update:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise ConnectionNotFound("connection not found")
        return row

    def list_connections(self, workspace_id: str) -> list[ConnectionView]:
        with self.database.session() as session:
            rows = session.scalars(
                select(UserConnection)
                .where(UserConnection.workspace_id == workspace_id, UserConnection.deleted_at.is_(None))
                .order_by(UserConnection.created_at, UserConnection.id)
            ).all()
            return [self._view(row) for row in rows]

    def get_connection(self, workspace_id: str, connection_id: str) -> ConnectionView:
        with self.database.session() as session:
            return self._view(self._live_row(session, workspace_id, connection_id))

    # ------------------------------------------------------------------ validation

    def _normalize_capabilities(
        self, protocol: ConnectionProtocol, requested: Sequence[str] | None
    ) -> list[str]:
        if requested is None:
            return sorted(protocol.capabilities)
        values = {str(item).strip().lower() for item in requested if str(item).strip()}
        unknown = values - _ALL_CAPABILITIES
        if unknown:
            raise ConnectionInvalid(f"unknown capability: {sorted(unknown)[0]}")
        unsupported = values - protocol.capabilities
        if unsupported:
            raise ConnectionInvalid(f"{protocol.label} connections do not support {sorted(unsupported)[0]}")
        if not values:
            raise ConnectionInvalid("a connection needs at least one capability")
        return sorted(values)

    @staticmethod
    def _normalize_models(
        capabilities: list[str], models: Sequence[ConnectionModelInput] | None
    ) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for model in models or []:
            model_id = " ".join(str(model.id or "").split())
            capability = str(model.capability or "").strip().lower()
            if not model_id:
                continue
            if len(model_id) > _MAX_MODEL_ID or any(ord(char) < 32 for char in model_id):
                raise ConnectionInvalid(f"a model id must be at most {_MAX_MODEL_ID} printable characters")
            if capability not in capabilities:
                raise ConnectionInvalid(f"model {model_id[:60]!r} names a capability this connection lacks")
            key = (model_id, capability)
            if key in seen:
                continue
            seen.add(key)
            result.append({"id": model_id, "capability": capability})
        if len(result) > _MAX_MODELS:
            raise ConnectionInvalid(f"a connection can list at most {_MAX_MODELS} models")
        return result

    @staticmethod
    def _normalize_name(name: str) -> str:
        value = " ".join(str(name or "").split())
        if not value:
            raise ConnectionInvalid("a connection name is required")
        if len(value) > 120:
            raise ConnectionInvalid("a connection name is at most 120 characters")
        return value

    @staticmethod
    def _normalize_secret(secret: str) -> str:
        value = str(secret or "").strip()
        if not value:
            raise ConnectionInvalid("an API key is required")
        if len(value) > 4096 or any(ord(char) < 33 or ord(char) == 127 for char in value):
            raise ConnectionInvalid("the API key contains characters an HTTP header cannot carry")
        return value

    def _encrypt(self, connection_id: str, secret: str) -> str:
        return self.vault.encrypt(f"{connection_id}:{secret}")

    def _decrypt(self, row: UserConnection) -> str:
        if not row.secret_ciphertext:
            raise ConnectionUnavailable("this connection has no key", code="CONNECTION_KEY_MISSING")
        try:
            plaintext = self.vault.decrypt(row.secret_ciphertext)
        except ValueError as exc:
            raise ConnectionUnavailable(
                "this connection's key cannot be decrypted by this server; enter the key again",
                code="CONNECTION_KEY_UNREADABLE",
            ) from exc
        prefix = f"{row.id}:"
        if not plaintext.startswith(prefix):
            raise ConnectionUnavailable(
                "this connection's key does not belong to it; enter the key again",
                code="CONNECTION_KEY_UNREADABLE",
            )
        return plaintext[len(prefix) :]

    # ------------------------------------------------------------------ mutations

    def create_connection(
        self,
        workspace_id: str,
        *,
        user_id: str | None,
        name: str,
        protocol: str,
        api_key: str,
        base_url: str | None = None,
        capabilities: Sequence[str] | None = None,
        models: Sequence[ConnectionModelInput] | None = None,
    ) -> ConnectionView:
        spec = self._protocol(protocol)
        clean_name = self._normalize_name(name)
        url = normalize_base_url(base_url or spec.default_base_url, allow_private=self.allow_private_network)
        clean_capabilities = self._normalize_capabilities(spec, capabilities)
        clean_models = self._normalize_models(clean_capabilities, models)
        secret = (
            self._normalize_secret(api_key) if spec.id != "mock" else (str(api_key or "").strip() or "mock")
        )
        connection_id = str(uuid.uuid4())
        with self.database.session() as session:
            row = UserConnection(
                id=connection_id,
                workspace_id=workspace_id,
                created_by=user_id,
                name=clean_name,
                protocol=spec.id,
                base_url=url,
                capabilities_json=clean_capabilities,
                models_json=clean_models,
                secret_ciphertext=self._encrypt(connection_id, secret),
                secret_hint=secret_hint(secret),
                status=UserConnectionStatus.ACTIVE.value,
                metadata_json={},
            )
            session.add(row)
            session.flush()
            return self._view(row)

    def update_connection(
        self,
        workspace_id: str,
        connection_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        capabilities: Sequence[str] | None = None,
        models: Sequence[ConnectionModelInput] | None = None,
    ) -> ConnectionView:
        with self.database.session() as session:
            row = self._live_row(session, workspace_id, connection_id, for_update=True)
            spec = self._protocol(row.protocol)
            if name is not None:
                row.name = self._normalize_name(name)
            if base_url is not None:
                row.base_url = normalize_base_url(base_url, allow_private=self.allow_private_network)
            current_capabilities = list(row.capabilities_json or [])
            if capabilities is not None:
                current_capabilities = self._normalize_capabilities(spec, capabilities)
                row.capabilities_json = current_capabilities
            if models is not None:
                row.models_json = self._normalize_models(current_capabilities, models)
            elif capabilities is not None:
                row.models_json = [
                    dict(model)
                    for model in row.models_json or []
                    if model.get("capability") in current_capabilities
                ]
            if api_key is not None and api_key.strip():
                secret = self._normalize_secret(api_key)
                row.secret_ciphertext = self._encrypt(row.id, secret)
                row.secret_hint = secret_hint(secret)
                # A new key has not been checked yet; the old verdict was about
                # a different key.
                row.status = UserConnectionStatus.ACTIVE.value
                row.last_verified_at = None
                row.last_error = None
            row.updated_at = self._clock()
            session.flush()
            return self._view(row)

    def delete_connection(self, workspace_id: str, connection_id: str, *, user_id: str | None) -> None:
        with self.database.session() as session:
            row = self._live_row(session, workspace_id, connection_id, for_update=True)
            now = self._clock()
            row.status = UserConnectionStatus.DELETED.value
            row.deleted_at = now
            row.deleted_by = user_id
            # The key goes with the connection. Jobs still pointing at it fail
            # at their next poll with CONNECTION_UNAVAILABLE.
            row.secret_ciphertext = ""
            account = session.get(ProviderAccount, self.resource_account_id(row.id))
            if account is not None:
                account.status = AccountStatus.DISABLED.value
            worker = session.get(BrowserWorker, self.resource_worker_id(row.id))
            if worker is not None:
                worker.status = WorkerStatus.OFFLINE.value
            session.flush()

    # ------------------------------------------------------------------ clients

    def _transport(
        self, row: UserConnection, secret: str, *, base_url: str | None = None
    ) -> ConnectionTransport:
        return self._transport_for(row.protocol, base_url or row.base_url, secret)

    def _chat_client(self, row: UserConnection, secret: str) -> ChatClient:
        if row.protocol == "mock":
            models = tuple(
                model["id"] for model in row.models_json or [] if model.get("capability") == "chat"
            ) or ("mock-chat",)
            return MockChatClient(models)
        if row.protocol == "anthropic":
            transport = self._transport(row, secret)
            return AnthropicChatClient(
                ProviderJsonClient(row.name[:40] or "anthropic", transport, api_key_configured=True),
                base_url=row.base_url,
            )
        base_url = row.base_url
        if row.protocol == "alibaba_dashscope":
            base_url = f"{row.base_url}/compatible-mode/v1"
        transport = self._transport(row, secret, base_url=base_url)
        return OpenAICompatibleChatClient(
            ProviderJsonClient(row.name[:40] or row.protocol, transport, api_key_configured=True)
        )

    def _usable_row(
        self,
        session: Session,
        *,
        connection_id: str,
        workspace_id: str | None,
        capability: str,
    ) -> UserConnection:
        if not self.enabled:
            raise ConnectionUnavailable(
                "workspace connections are switched off on this server", code="CONNECTIONS_DISABLED"
            )
        row = session.get(UserConnection, connection_id)
        if row is None or row.deleted_at is not None or row.status == UserConnectionStatus.DELETED.value:
            raise ConnectionUnavailable("the connection no longer exists", code="CONNECTION_UNAVAILABLE")
        if workspace_id is None or row.workspace_id != workspace_id:
            raise ConnectionUnavailable(
                "the connection belongs to a different workspace", code="CONNECTION_WORKSPACE_MISMATCH"
            )
        if capability not in (row.capabilities_json or []):
            raise ConnectionUnavailable(
                f"the connection {row.name!r} is not enabled for {capability}",
                code="CONNECTION_CAPABILITY_MISSING",
            )
        spec = PROTOCOLS.get(row.protocol)
        if spec is None or capability not in spec.capabilities:
            raise ConnectionUnavailable("the connection's protocol cannot serve this call")
        if spec.development_only and not self.mock_protocol_enabled:
            raise ConnectionUnavailable("mock connections are available in development only")
        return row

    async def chat(
        self,
        *,
        connection_id: str,
        workspace_id: str | None,
        model: str,
        messages: list[dict[str, str]],
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        model_id = " ".join(str(model or "").split())
        if not model_id:
            raise ConnectionInvalid("a model is required")
        with self.database.session() as session:
            row = self._usable_row(
                session, connection_id=connection_id, workspace_id=workspace_id, capability="chat"
            )
            secret = self._decrypt(row)
            client = self._chat_client(row, secret)
        return await client.complete(
            model=model_id,
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    async def list_remote_models(self, workspace_id: str, connection_id: str) -> list[RemoteModel]:
        with self.database.session() as session:
            row = self._live_row(session, workspace_id, connection_id)
            spec = self._protocol(row.protocol)
            if not spec.lists_models:
                return [
                    RemoteModel(model_id) for values in spec.suggested_models.values() for model_id in values
                ]
            secret = self._decrypt(row)
            client = self._chat_client(row, secret)
        return await client.list_models()

    async def test_connection(self, workspace_id: str, connection_id: str) -> dict[str, Any]:
        """Check the key against the provider without generating anything billable.

        A protocol whose model listing requires the key is checked by listing,
        and only that verdict moves the connection's status. OpenRouter's list
        is public, so listing shows the service answers but proves nothing
        about the key; Ark and DashScope expose no key-check endpoint this
        platform has reviewed. Both say so rather than spending a request that
        would.
        """

        with self.database.session() as session:
            row = self._live_row(session, workspace_id, connection_id)
            spec = self._protocol(row.protocol)
            secret = self._decrypt(row)
            client = self._chat_client(row, secret) if spec.lists_models else None
        verifies_key = client is not None and spec.listing_checks_key
        started = asyncio.get_running_loop().time()
        ok = True
        message = ""
        code: str | None = None
        models_found: int | None = None
        if client is not None:
            try:
                models = await client.list_models()
                models_found = len(models)
                listed = f"{models_found} model{'' if models_found == 1 else 's'}"
                message = (
                    f"The provider accepted the key and lists {listed}."
                    if verifies_key
                    else f"{spec.label} answered and lists {listed}. That list is public, "
                    "so the key itself is checked on first use."
                )
            except ProviderCallFailed as exc:
                ok = False
                code = exc.code
                message = str(exc)[:500]
            except (httpx.HTTPError, OSError) as exc:
                ok = False
                code = "PROVIDER_NETWORK_ERROR"
                message = f"the provider could not be reached: {type(exc).__name__}"
        else:
            message = "Saved. This provider has no free key check, so the key is verified on first use."
        latency_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        with self.database.session() as session:
            row = self._live_row(session, workspace_id, connection_id, for_update=True)
            # Only the provider's answer about the key moves the status: an
            # unreachable host or a refused address says nothing about the key.
            key_verdict = ok or code in _KEY_REFUSED_CODES
            if verifies_key and key_verdict:
                row.last_verified_at = self._clock()
                row.status = UserConnectionStatus.ACTIVE.value if ok else UserConnectionStatus.INVALID.value
                row.last_error = None if ok else message
            session.flush()
            view = self._view(row)
        return {
            "ok": ok,
            "checked_remotely": verifies_key,
            "code": code,
            "message": message,
            "latency_ms": latency_ms,
            "models_found": models_found,
            "connection": view.as_dict(),
        }

    # ------------------------------------------------------------------ gateway seam

    @staticmethod
    def resource_account_id(connection_id: str) -> str:
        return str(uuid.uuid5(_RESOURCE_NAMESPACE, f"account:{connection_id}"))

    @staticmethod
    def resource_worker_id(connection_id: str) -> str:
        # A UUID, not a readable "byok:<id>": `provider_accounts.worker_id` is
        # 36 characters, which PostgreSQL enforces and SQLite does not.
        return str(uuid.uuid5(_RESOURCE_NAMESPACE, f"worker:{connection_id}"))

    def assert_generation_target_in_session(
        self,
        session: Session,
        *,
        connection_id: str,
        project_id: str,
        media_type: str,
    ) -> None:
        project = session.get(Project, project_id)
        if project is None:
            raise ConnectionUnavailable("project not found")
        self._usable_row(
            session,
            connection_id=connection_id,
            workspace_id=project.workspace_id,
            capability=media_type,
        )

    def ensure_resources(self, connection_id: str) -> str:
        """Create or refresh the connection's scheduler capacity; return its account id.

        The scheduler owns concurrency and release accounting for every job, so
        a connection gets a capacity resource of its own, pinned by id: no
        other workspace's job can ever be scheduled onto it, and no platform
        provider shares its provider name.
        """

        account_id = self.resource_account_id(connection_id)
        worker_id = self.resource_worker_id(connection_id)
        marker = {
            "resource_kind": "USER_CONNECTION",
            "connection_id": connection_id,
            "stores_provider_secret": False,
        }
        for attempt in range(2):
            try:
                with self.database.session() as session:
                    connection = session.get(UserConnection, connection_id)
                    if connection is None:
                        raise ConnectionUnavailable("the connection no longer exists")
                    live = connection.deleted_at is None
                    account = session.get(ProviderAccount, account_id)
                    if account is None:
                        account = ProviderAccount(
                            id=account_id,
                            provider=USER_CONNECTION_PROVIDER,
                            account_identifier=f"{USER_CONNECTION_PROVIDER}://{connection_id}",
                        )
                        session.add(account)
                    elif account.metadata_json.get("resource_kind") != "USER_CONNECTION":
                        raise RuntimeError("connection resource id collides with another account")
                    if live and account.status not in {AccountStatus.READY.value, AccountStatus.BUSY.value}:
                        account.status = AccountStatus.READY.value
                    elif not live:
                        account.status = AccountStatus.DISABLED.value
                    account.credits = 1
                    account.image_capacity = self.max_concurrent_jobs
                    account.video_capacity = self.max_concurrent_jobs
                    account.supported_models = []
                    account.metadata_json = {**marker, "workspace_id": connection.workspace_id}
                    session.flush()
                    worker = session.get(BrowserWorker, worker_id)
                    if worker is None:
                        worker = BrowserWorker(
                            id=worker_id,
                            provider=USER_CONNECTION_PROVIDER,
                            account_id=account.id,
                            connection_id=f"{USER_CONNECTION_PROVIDER}://{connection_id}",
                            status=WorkerStatus.READY.value,
                        )
                        session.add(worker)
                    elif worker.metadata_json.get("resource_kind") != "USER_CONNECTION":
                        raise RuntimeError("connection resource id collides with another worker")
                    if live and worker.status == WorkerStatus.OFFLINE.value:
                        worker.status = WorkerStatus.READY.value
                    worker.capabilities = ["image", "video"]
                    worker.max_jobs = self.max_concurrent_jobs
                    worker.metadata_json = marker
                    account.worker_id = worker.id
                    session.flush()
                return account_id
            except IntegrityError:
                # A concurrent worker created the same deterministic rows.
                if attempt == 1:
                    raise
        return account_id

    def generation_provider(
        self, connection_id: str, *, project_id: str, media_type: str
    ) -> GenerationProvider:
        with self.database.session() as session:
            project = session.get(Project, project_id)
            row = self._usable_row(
                session,
                connection_id=connection_id,
                workspace_id=project.workspace_id if project else None,
                capability=media_type,
            )
            secret = self._decrypt(row)
            protocol = row.protocol
            base_url = row.base_url
            name = row.name
        if protocol == "mock":
            return MockGenerationProvider(connection_id=connection_id)
        # OpenRouter serves a finished video from its own API host and wants the
        # key there; the key is presented to that host only. Every other
        # protocol returns signed URLs that need no credential at all.
        credential = ("Authorization", f"Bearer {secret}") if protocol == "openrouter" else None
        policy = RemoteMediaFetchPolicy(
            allowed_host_patterns=None,
            credential_header=credential,
            credential_host=host_of(base_url) if credential else None,
            allow_non_public_addresses=self.allow_private_network,
        )
        inner: GenerationProvider
        if protocol == "openrouter":
            inner = OpenRouterProvider(
                api_key=secret,
                base_url=base_url,
                transport=self._transport_for(protocol, base_url, secret),
            )
        elif protocol == "volcengine_ark":
            inner = ArkProvider(
                api_key=secret,
                base_url=base_url,
                transport=self._transport_for(protocol, base_url, secret),
            )
        elif protocol == "alibaba_dashscope":
            inner = _ConnectionWanProvider(
                api_key=secret,
                dashscope_base_url=f"{base_url}/api/v1",
                video_transport=self._transport_for(protocol, f"{base_url}/api/v1", secret),
                chat_transport=self._transport_for(protocol, f"{base_url}/compatible-mode/v1", secret),
            )
        else:
            raise ConnectionUnavailable(f"the connection {name!r} cannot generate {media_type}")
        return ConnectionGenerationProvider(inner, connection_id=connection_id, media_fetch_policy=policy)

    def _transport_for(self, protocol: str, base_url: str, secret: str) -> ConnectionTransport:
        spec = self._protocol(protocol)
        return ConnectionTransport(
            base_url=base_url,
            api_key=secret,
            auth=AuthStyle(spec.auth),
            timeout_seconds=self.http_timeout_seconds,
            max_response_bytes=self.max_response_bytes,
            allow_private=self.allow_private_network,
            default_headers=spec.default_headers,
            resolver=self._resolver,
            client_factory=self._client_factory,
        )


class _ConnectionWanProvider(WanProvider):
    """DashScope Wan video with the workspace's own model ids passed straight through.

    The platform adapter maps a *logical* model name (``wan-2.7``) onto a
    reviewed DashScope id per mode and refuses anything unmapped, because an
    operator's registry must never post a guessed id. A connection's model id
    is already the DashScope id its owner chose, so it maps to itself; the
    mode checks, the media roles and the request shape stay the adapter's.
    """

    video_model_keys: dict[str, str]

    def _video_payload(self, request: dict[str, Any]) -> dict[str, Any]:
        model = str(request.get("model") or "").strip()
        if model and model not in self.video_model_keys:
            self.video_model_keys = {**self.video_model_keys, model: model}
        return super()._video_payload(request)


__all__ = [
    "ConnectionModelInput",
    "ConnectionView",
    "UserConnectionService",
    "secret_hint",
]
