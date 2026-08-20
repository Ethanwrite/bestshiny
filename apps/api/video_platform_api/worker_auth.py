from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    ProviderAccount,
    WorkerAccessCredential,
    WorkerSocketTicket,
    utcnow,
)
from sqlalchemy import select, update


class WorkerAuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class WorkerPrincipal:
    credential_id: str
    worker_id: str
    provider: str
    account_id: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedWorkerSecret:
    id: str
    token: str
    expires_at: datetime


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class WorkerCredentialService:
    """Opaque worker credentials with DB-backed expiry, binding, and revocation."""

    def __init__(
        self,
        database: Database,
        *,
        default_ttl_seconds: int = 86_400,
        socket_ticket_ttl_seconds: int = 60,
    ):
        self.database = database
        self.default_ttl_seconds = max(60, default_ttl_seconds)
        self.socket_ticket_ttl_seconds = max(10, min(socket_ticket_ttl_seconds, 300))

    def issue(
        self,
        *,
        worker_id: str,
        provider: str,
        account_id: str,
        ttl_seconds: int | None = None,
    ) -> IssuedWorkerSecret:
        worker_id = worker_id.strip()
        provider = provider.strip()
        if not worker_id or not provider or not account_id:
            raise ValueError("worker_id, provider, and account_id are required")
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        if ttl < 60 or ttl > 30 * 24 * 60 * 60:
            raise ValueError("worker credential lifetime must be between 60 seconds and 30 days")
        token = secrets.token_urlsafe(48)
        expires_at = utcnow() + timedelta(seconds=ttl)
        with self.database.session() as session:
            account = session.get(ProviderAccount, account_id)
            if account is None or account.provider != provider:
                raise ValueError("worker account is invalid for provider")
            credential = WorkerAccessCredential(
                worker_id=worker_id,
                provider=provider,
                account_id=account_id,
                token_hash=_digest(token),
                expires_at=expires_at,
            )
            session.add(credential)
            session.flush()
            return IssuedWorkerSecret(credential.id, token, expires_at)

    def revoke(self, credential_id: str) -> bool:
        with self.database.session() as session:
            credential = session.get(WorkerAccessCredential, credential_id)
            if credential is None:
                return False
            if credential.revoked_at is None:
                credential.revoked_at = utcnow()
            return True

    def authenticate_authorization(self, authorization: str | None) -> WorkerPrincipal:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise WorkerAuthenticationError("worker credential is required")
        return self.authenticate_token(token.strip())

    def authenticate_token(self, token: str) -> WorkerPrincipal:
        if len(token) < 32:
            raise WorkerAuthenticationError("invalid worker credential")
        now = utcnow()
        with self.database.session() as session:
            credential = session.scalar(
                select(WorkerAccessCredential).where(WorkerAccessCredential.token_hash == _digest(token))
            )
            if (
                credential is None
                or credential.revoked_at is not None
                or _aware(credential.expires_at) <= now
            ):
                raise WorkerAuthenticationError("invalid or expired worker credential")
            credential.last_used_at = now
            return self._principal(credential)

    @staticmethod
    def _principal(credential: WorkerAccessCredential) -> WorkerPrincipal:
        return WorkerPrincipal(
            credential_id=credential.id,
            worker_id=credential.worker_id,
            provider=credential.provider,
            account_id=credential.account_id,
            expires_at=_aware(credential.expires_at),
        )

    def validate_principal(self, principal: WorkerPrincipal) -> WorkerPrincipal:
        """Revalidate an established channel so revoke/expiry takes effect promptly."""

        now = utcnow()
        with self.database.session() as session:
            credential = session.get(WorkerAccessCredential, principal.credential_id)
            if (
                credential is None
                or credential.revoked_at is not None
                or _aware(credential.expires_at) <= now
                or credential.worker_id != principal.worker_id
                or credential.provider != principal.provider
                or credential.account_id != principal.account_id
            ):
                raise WorkerAuthenticationError("invalid or expired worker credential")
            credential.last_used_at = now
            return self._principal(credential)

    def issue_socket_ticket(self, principal: WorkerPrincipal) -> IssuedWorkerSecret:
        token = secrets.token_urlsafe(48)
        expires_at = utcnow() + timedelta(seconds=self.socket_ticket_ttl_seconds)
        with self.database.session() as session:
            credential = session.get(WorkerAccessCredential, principal.credential_id)
            if (
                credential is None
                or credential.revoked_at is not None
                or _aware(credential.expires_at) <= utcnow()
            ):
                raise WorkerAuthenticationError("invalid or expired worker credential")
            ticket = WorkerSocketTicket(
                credential_id=credential.id,
                worker_id=credential.worker_id,
                token_hash=_digest(token),
                expires_at=expires_at,
            )
            session.add(ticket)
            session.flush()
            return IssuedWorkerSecret(ticket.id, token, expires_at)

    def consume_socket_ticket(self, token: str, *, worker_id: str) -> WorkerPrincipal:
        if len(token) < 32:
            raise WorkerAuthenticationError("invalid WebSocket ticket")
        now = utcnow()
        with self.database.session() as session:
            ticket = session.scalar(
                select(WorkerSocketTicket).where(WorkerSocketTicket.token_hash == _digest(token))
            )
            if (
                ticket is None
                or ticket.worker_id != worker_id
                or ticket.consumed_at is not None
                or _aware(ticket.expires_at) <= now
            ):
                raise WorkerAuthenticationError("invalid or expired WebSocket ticket")
            credential = session.get(WorkerAccessCredential, ticket.credential_id)
            if (
                credential is None
                or credential.revoked_at is not None
                or credential.worker_id != worker_id
                or _aware(credential.expires_at) <= now
            ):
                raise WorkerAuthenticationError("invalid or expired worker credential")
            consumed = session.execute(
                update(WorkerSocketTicket)
                .where(
                    WorkerSocketTicket.id == ticket.id,
                    WorkerSocketTicket.consumed_at.is_(None),
                    WorkerSocketTicket.expires_at > now,
                )
                .values(consumed_at=now, updated_at=now)
                .execution_options(synchronize_session=False)
            )
            if affected_rows(consumed) != 1:
                raise WorkerAuthenticationError("WebSocket ticket has already been used")
            credential.last_used_at = now
            return self._principal(credential)
