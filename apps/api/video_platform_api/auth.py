from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    AuthLoginThrottle,
    AuthSession,
    LegacyWorkspaceClaim,
    PasswordResetToken,
    PlatformRole,
    Project,
    User,
    Workspace,
    WorkspaceMembership,
)
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import Headers
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000
PASSWORD_SALT_BYTES = 16
PASSWORD_DIGEST_BYTES = 32
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
ROLE_RANK = {"VIEWER": 10, "EDITOR": 20, "ADMIN": 30, "OWNER": 40}
AUTH_COOKIE_NAME = "ai_director_session"
CSRF_COOKIE_NAME = "ai_director_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
UNSAFE_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_EXEMPT_PATHS = frozenset(
    {
        "/api/auth/register",
        "/api/auth/login",
        "/api/auth/password-reset/request",
        "/api/auth/password-reset/confirm",
        # Provider-to-provider callback authenticated by the XunHuPay body
        # signature. It never uses a browser session or CSRF token.
        "/v1/payments/xunhupay/notify",
    }
)
PASSWORD_RESET_TTL = timedelta(minutes=30)
LOGIN_THROTTLE_WINDOW = timedelta(minutes=15)
LOGIN_THROTTLE_BLOCK = timedelta(minutes=15)
LOGIN_THROTTLE_FAILURE_LIMIT = 5


class CookieCSRFMiddleware:
    """Require a double-submit token only when an unsafe request uses the auth cookie.

    Bearer clients remain backwards compatible. A malformed or invalid Bearer token
    cannot fall back to the cookie in ``current_user``, so adding an Authorization
    header cannot turn a cookie-authenticated request into a CSRF bypass.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if method not in UNSAFE_HTTP_METHODS or path in CSRF_EXEMPT_PATHS or path.startswith("/internal/"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        authorization = headers.get("authorization", "")
        scheme, _, bearer_token = authorization.partition(" ")
        has_bearer = scheme.casefold() == "bearer" and bool(bearer_token.strip())
        request = StarletteRequest(scope)
        auth_cookie = request.cookies.get(AUTH_COOKIE_NAME, "")
        if auth_cookie and not has_bearer:
            cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
            header_token = headers.get(CSRF_HEADER_NAME, "")
            if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
                response = JSONResponse(
                    {"detail": "CSRF token is missing or invalid"},
                    status_code=status.HTTP_403_FORBIDDEN,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class RegistrationConflict(RuntimeError):
    pass


class InvalidCredentials(RuntimeError):
    pass


class LoginThrottled(RuntimeError):
    def __init__(self, retry_after_seconds: int):
        super().__init__("登录尝试过多，请稍后重试")
        self.retry_after_seconds = max(1, retry_after_seconds)


class PasswordResetRejected(RuntimeError):
    pass


class LegacyClaimConflict(RuntimeError):
    pass


class LegacyClaimTargetNotFound(LookupError):
    pass


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=1024)
    display_name: str = Field(default="", max_length=160)
    workspace_name: str = Field(default="", max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class PasswordResetConfirmRequest(BaseModel):
    token: str = Field(min_length=20, max_length=500)
    new_password: str = Field(min_length=12, max_length=1024)


class LegacyWorkspaceClaimRequest(BaseModel):
    target_user_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=200)


@dataclass(frozen=True)
class AuthPrincipal:
    user_id: str
    email: str
    display_name: str
    session_id: str
    workspace_roles: dict[str, str]
    platform_role: str = PlatformRole.USER.value
    development_bypass: bool = False


@dataclass(frozen=True)
class IssuedSession:
    token: str
    principal: AuthPrincipal
    expires_at: datetime


@dataclass(frozen=True)
class PasswordResetIssue:
    token: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class LegacyWorkspaceClaimResult:
    claim_id: str
    target_user_id: str
    workspace_ids: list[str]
    project_ids: list[str]
    replayed: bool


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    if len(normalized) > 320 or not EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("invalid email address")
    return normalized


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    encoded = password.encode("utf-8")
    if len(password) < 12 or len(encoded) > 4096:
        raise ValueError("password must contain 12-1024 characters")
    salt = salt or secrets.token_bytes(PASSWORD_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        encoded,
        salt,
        PASSWORD_ITERATIONS,
        dklen=PASSWORD_DIGEST_BYTES,
    )
    return f"{PASSWORD_ALGORITHM}${PASSWORD_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = stored.split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        iterations = int(raw_iterations)
        if iterations < 1 or iterations > 2_000_000:
            return False
        expected = _b64decode(raw_digest)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _b64decode(raw_salt),
            iterations,
            dklen=len(expected),
        )
    except (UnicodeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AuthService:
    def __init__(
        self,
        database: Database,
        *,
        session_ttl_days: int = 30,
        auth_required: bool = True,
        deployment_environment: str = "production",
        login_failure_limit: int = LOGIN_THROTTLE_FAILURE_LIMIT,
    ):
        self.database = database
        self.session_ttl = timedelta(days=max(1, min(session_ttl_days, 90)))
        self.auth_required = auth_required
        self.deployment_environment = deployment_environment.strip().casefold()
        self.secure_cookies = self.deployment_environment == "production"
        self.login_failure_limit = max(2, min(login_failure_limit, 100))
        # Used to make a missing-user login take the same expensive KDF path.
        self._dummy_password_hash = hash_password("not-a-real-password")

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _login_throttle_id(email: str, client_ip: str) -> str:
        normalized_identifier = email.strip().casefold()[:320]
        normalized_ip = client_ip.strip()[:128] or "unknown"
        material = f"login\0{normalized_identifier}\0{normalized_ip}".encode()
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _request_ip_hash(client_ip: str) -> str | None:
        normalized = client_ip.strip()[:128]
        if not normalized:
            return None
        return hashlib.sha256(f"password-reset\0{normalized}".encode()).hexdigest()

    def _check_login_throttle(self, throttle_id: str, *, now: datetime) -> None:
        with self.database.session() as session:
            throttle = session.get(AuthLoginThrottle, throttle_id)
            if throttle is None or throttle.blocked_until is None:
                return
            blocked_until = _utc(throttle.blocked_until)
            if blocked_until > now:
                retry_after = int((blocked_until - now).total_seconds()) + 1
                raise LoginThrottled(retry_after)
            throttle.failure_count = 0
            throttle.window_started_at = now
            throttle.blocked_until = None

    def _record_login_failure(self, throttle_id: str, *, now: datetime) -> None:
        for attempt in range(2):
            try:
                with self.database.session() as session:
                    throttle = session.get(AuthLoginThrottle, throttle_id, with_for_update=True)
                    if throttle is None:
                        throttle = AuthLoginThrottle(
                            id=throttle_id,
                            failure_count=1,
                            window_started_at=now,
                        )
                        session.add(throttle)
                    elif now - _utc(throttle.window_started_at) >= LOGIN_THROTTLE_WINDOW:
                        throttle.failure_count = 1
                        throttle.window_started_at = now
                        throttle.blocked_until = None
                    else:
                        throttle.failure_count += 1
                    if throttle.failure_count >= self.login_failure_limit:
                        throttle.blocked_until = now + LOGIN_THROTTLE_BLOCK
                return
            except IntegrityError:
                # A concurrent first failure can win the primary-key insert.
                # Retry once and increment its durable row under the row lock.
                if attempt == 1:
                    raise

    def _clear_login_throttle(self, throttle_id: str) -> None:
        with self.database.session() as session:
            session.execute(delete(AuthLoginThrottle).where(AuthLoginThrottle.id == throttle_id))

    def register(
        self,
        body: RegisterRequest,
        *,
        user_agent: str = "",
    ) -> IssuedSession:
        email = normalize_email(body.email)
        password_hash = hash_password(body.password)
        display_name = body.display_name.strip() or email.split("@", 1)[0]
        workspace_name = body.workspace_name.strip() or f"{display_name}的工作空间"
        try:
            with self.database.session() as session:
                existing = session.scalar(select(User).where(func.lower(User.email) == email))
                if existing:
                    raise RegistrationConflict("该邮箱已注册")
                user = User(
                    email=email,
                    display_name=display_name,
                    password_hash=password_hash,
                    status="ACTIVE",
                )
                session.add(user)
                session.flush()
                workspace = Workspace(owner_user_id=user.id, name=workspace_name, status="ACTIVE")
                session.add(workspace)
                session.flush()
                session.add(
                    WorkspaceMembership(
                        workspace_id=workspace.id,
                        user_id=user.id,
                        role="OWNER",
                        status="ACTIVE",
                    )
                )
                return self._issue(session, user, user_agent=user_agent)
        except IntegrityError as exc:
            raise RegistrationConflict("该邮箱已注册") from exc

    def login(
        self,
        body: LoginRequest,
        *,
        user_agent: str = "",
        client_ip: str = "",
    ) -> IssuedSession:
        throttle_id = self._login_throttle_id(body.email, client_ip)
        now = datetime.now(UTC)
        self._check_login_throttle(throttle_id, now=now)
        try:
            email = normalize_email(body.email)
        except ValueError as exc:
            verify_password(body.password, self._dummy_password_hash)
            self._record_login_failure(throttle_id, now=now)
            raise InvalidCredentials("邮箱或密码错误") from exc
        issued: IssuedSession | None = None
        with self.database.session() as session:
            user = session.scalar(select(User).where(func.lower(User.email) == email))
            password_hash = user.password_hash if user and user.password_hash else self._dummy_password_hash
            password_ok = verify_password(body.password, password_hash)
            if user and password_ok and user.status == "ACTIVE":
                if not user.password_hash.startswith(f"{PASSWORD_ALGORITHM}${PASSWORD_ITERATIONS}$"):
                    user.password_hash = hash_password(body.password)
                issued = self._issue(session, user, user_agent=user_agent)
        if issued is None:
            self._record_login_failure(throttle_id, now=now)
            raise InvalidCredentials("邮箱或密码错误")
        self._clear_login_throttle(throttle_id)
        return issued

    def request_password_reset(
        self,
        body: PasswordResetRequest,
        *,
        client_ip: str = "",
    ) -> PasswordResetIssue:
        try:
            email = normalize_email(body.email)
        except ValueError:
            return PasswordResetIssue(token=None, expires_at=None)

        now = datetime.now(UTC)
        expires_at = now + PASSWORD_RESET_TTL
        token = secrets.token_urlsafe(32)
        with self.database.session() as session:
            user = session.scalar(select(User).where(func.lower(User.email) == email))
            if user is None or user.status != "ACTIVE":
                return PasswordResetIssue(token=None, expires_at=None)
            session.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.consumed_at.is_(None),
                )
                .values(consumed_at=now)
            )
            session.add(
                PasswordResetToken(
                    user_id=user.id,
                    token_hash=self._token_hash(token),
                    expires_at=expires_at,
                    requested_ip_hash=self._request_ip_hash(client_ip),
                )
            )
        return PasswordResetIssue(token=token, expires_at=expires_at)

    def confirm_password_reset(self, body: PasswordResetConfirmRequest) -> None:
        password_hash = hash_password(body.new_password)
        now = datetime.now(UTC)
        with self.database.session() as session:
            reset_token = session.scalar(
                select(PasswordResetToken)
                .where(PasswordResetToken.token_hash == self._token_hash(body.token))
                .with_for_update()
            )
            if (
                reset_token is None
                or reset_token.consumed_at is not None
                or _utc(reset_token.expires_at) <= now
            ):
                raise PasswordResetRejected("重置链接无效或已过期")
            consumed = session.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.id == reset_token.id,
                    PasswordResetToken.consumed_at.is_(None),
                    PasswordResetToken.expires_at > now,
                )
                .values(consumed_at=now)
                .execution_options(synchronize_session=False)
            )
            if affected_rows(consumed) != 1:
                raise PasswordResetRejected("重置链接无效或已过期")
            user = session.get(User, reset_token.user_id)
            if user is None or user.status != "ACTIVE":
                raise PasswordResetRejected("重置链接无效或已过期")
            user.password_hash = password_hash
            session.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.consumed_at.is_(None),
                )
                .values(consumed_at=now)
            )
            session.execute(
                update(AuthSession)
                .where(
                    AuthSession.user_id == user.id,
                    AuthSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )

    def claim_legacy_workspaces(
        self,
        body: LegacyWorkspaceClaimRequest,
    ) -> LegacyWorkspaceClaimResult:
        """Transfer the isolated V1 tenant only after an authenticated internal request."""

        try:
            return self._claim_legacy_workspaces_once(body)
        except IntegrityError as exc:
            # Concurrent requests can race at either uniqueness constraint after
            # one transaction has already completed. Recover only the exact same
            # target as an idempotent replay; every ambiguous case fails closed.
            with self.database.session() as session:
                existing = session.scalar(
                    select(LegacyWorkspaceClaim).where(
                        LegacyWorkspaceClaim.idempotency_key == body.idempotency_key.strip()
                    )
                )
                if existing and existing.target_user_id == body.target_user_id:
                    return self._legacy_claim_result(existing, replayed=True)
            raise LegacyClaimConflict("legacy workspace claim conflicted with another request") from exc

    def _claim_legacy_workspaces_once(
        self,
        body: LegacyWorkspaceClaimRequest,
    ) -> LegacyWorkspaceClaimResult:

        idempotency_key = body.idempotency_key.strip()
        if len(idempotency_key) < 8:
            raise ValueError("idempotency_key must contain at least 8 non-space characters")
        legacy_email = "local@ai-director.invalid"
        with self.database.session() as session:
            existing_key = session.scalar(
                select(LegacyWorkspaceClaim).where(LegacyWorkspaceClaim.idempotency_key == idempotency_key)
            )
            if existing_key:
                if existing_key.target_user_id != body.target_user_id:
                    raise LegacyClaimConflict("idempotency_key already belongs to another target user")
                return self._legacy_claim_result(existing_key, replayed=True)

            target_user = session.get(User, body.target_user_id)
            if (
                not target_user
                or target_user.status != "ACTIVE"
                or target_user.email.casefold() == legacy_email
            ):
                raise LegacyClaimTargetNotFound("active target user not found")

            previous_claim = session.scalar(
                select(LegacyWorkspaceClaim).order_by(LegacyWorkspaceClaim.created_at)
            )
            if previous_claim:
                if previous_claim.target_user_id != target_user.id:
                    raise LegacyClaimConflict("legacy workspace has already been claimed")
                return self._legacy_claim_result(previous_claim, replayed=True)

            legacy_user = session.scalar(
                select(User).where(func.lower(User.email) == legacy_email).with_for_update()
            )
            if not legacy_user:
                raise LegacyClaimConflict("isolated legacy workspace is not available")

            workspaces = list(
                session.scalars(
                    select(Workspace)
                    .where(Workspace.owner_user_id == legacy_user.id)
                    .order_by(Workspace.created_at, Workspace.id)
                )
            )
            if not workspaces:
                raise LegacyClaimConflict("isolated legacy workspace is not available")
            workspace_ids = [item.id for item in workspaces]
            project_ids = list(
                session.scalars(
                    select(Project.id)
                    .where((Project.workspace_id.in_(workspace_ids)) | (Project.workspace_id.is_(None)))
                    .order_by(Project.created_at, Project.id)
                )
            )

            claimed = session.execute(
                update(Workspace)
                .where(
                    Workspace.id.in_(workspace_ids),
                    Workspace.owner_user_id == legacy_user.id,
                )
                .values(owner_user_id=target_user.id)
            )
            if affected_rows(claimed) != len(workspace_ids):
                raise LegacyClaimConflict("legacy workspace was claimed concurrently")

            session.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id.in_(workspace_ids),
                    WorkspaceMembership.user_id == legacy_user.id,
                )
            )
            for workspace_id in workspace_ids:
                membership = session.scalar(
                    select(WorkspaceMembership).where(
                        WorkspaceMembership.workspace_id == workspace_id,
                        WorkspaceMembership.user_id == target_user.id,
                    )
                )
                if membership:
                    membership.role = "OWNER"
                    membership.status = "ACTIVE"
                else:
                    session.add(
                        WorkspaceMembership(
                            workspace_id=workspace_id,
                            user_id=target_user.id,
                            role="OWNER",
                            status="ACTIVE",
                        )
                    )
            session.execute(
                update(Project).where(Project.workspace_id.is_(None)).values(workspace_id=workspace_ids[0])
            )
            claim = LegacyWorkspaceClaim(
                idempotency_key=idempotency_key,
                legacy_user_id=legacy_user.id,
                target_user_id=target_user.id,
                actor_type="PLATFORM_API_KEY",
                workspace_ids=workspace_ids,
                project_ids=project_ids,
                status="COMPLETED",
            )
            session.add(claim)
            session.flush()
            return self._legacy_claim_result(claim, replayed=False)

    @staticmethod
    def _legacy_claim_result(
        claim: LegacyWorkspaceClaim,
        *,
        replayed: bool,
    ) -> LegacyWorkspaceClaimResult:
        return LegacyWorkspaceClaimResult(
            claim_id=claim.id,
            target_user_id=claim.target_user_id,
            workspace_ids=list(claim.workspace_ids),
            project_ids=list(claim.project_ids),
            replayed=replayed,
        )

    def _issue(self, session, user: User, *, user_agent: str) -> IssuedSession:  # type: ignore[no-untyped-def]
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        auth_session = AuthSession(
            user_id=user.id,
            token_hash=self._token_hash(token),
            expires_at=now + self.session_ttl,
            last_used_at=now,
            user_agent=user_agent[:500],
        )
        session.add(auth_session)
        session.flush()
        return IssuedSession(
            token=token,
            principal=self._principal(session, user, auth_session.id),
            expires_at=auth_session.expires_at,
        )

    @staticmethod
    def _principal(session, user: User, session_id: str) -> AuthPrincipal:  # type: ignore[no-untyped-def]
        memberships = list(
            session.scalars(
                select(WorkspaceMembership)
                .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
                .where(
                    WorkspaceMembership.user_id == user.id,
                    WorkspaceMembership.status == "ACTIVE",
                    Workspace.status == "ACTIVE",
                )
            )
        )
        return AuthPrincipal(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            session_id=session_id,
            workspace_roles={item.workspace_id: item.role for item in memberships},
            platform_role=user.platform_role,
        )

    def authenticate(self, token: str) -> AuthPrincipal:
        now = datetime.now(UTC)
        with self.database.session() as session:
            auth_session = session.scalar(
                select(AuthSession).where(AuthSession.token_hash == self._token_hash(token))
            )
            if (
                not auth_session
                or auth_session.revoked_at is not None
                or _utc(auth_session.expires_at) <= now
            ):
                raise InvalidCredentials("登录已失效，请重新登录")
            user = session.get(User, auth_session.user_id)
            if not user or user.status != "ACTIVE":
                raise InvalidCredentials("登录已失效，请重新登录")
            if now - _utc(auth_session.last_used_at) >= timedelta(minutes=5):
                auth_session.last_used_at = now
            return self._principal(session, user, auth_session.id)

    def revoke(self, principal: AuthPrincipal) -> None:
        if principal.development_bypass:
            return
        with self.database.session() as session:
            auth_session = session.get(AuthSession, principal.session_id)
            if auth_session and auth_session.user_id == principal.user_id:
                auth_session.revoked_at = datetime.now(UTC)

    def set_session_cookies(self, response: Response, issued: IssuedSession) -> None:
        max_age = max(1, int((issued.expires_at - datetime.now(UTC)).total_seconds()))
        response.set_cookie(
            AUTH_COOKIE_NAME,
            issued.token,
            max_age=max_age,
            expires=issued.expires_at,
            path="/",
            secure=self.secure_cookies,
            httponly=True,
            samesite="lax",
        )
        response.set_cookie(
            CSRF_COOKIE_NAME,
            secrets.token_urlsafe(32),
            max_age=max_age,
            expires=issued.expires_at,
            path="/",
            secure=self.secure_cookies,
            httponly=False,
            samesite="lax",
        )

    def clear_session_cookies(self, response: Response) -> None:
        response.delete_cookie(
            AUTH_COOKIE_NAME,
            path="/",
            secure=self.secure_cookies,
            httponly=True,
            samesite="lax",
        )
        response.delete_cookie(
            CSRF_COOKIE_NAME,
            path="/",
            secure=self.secure_cookies,
            httponly=False,
            samesite="lax",
        )

    def current_user(
        self,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> AuthPrincipal:
        if not authorization and not request.cookies.get(AUTH_COOKIE_NAME) and not self.auth_required:
            return AuthPrincipal(
                user_id="development-bypass",
                email="development@local.invalid",
                display_name="Development User",
                session_id="development-bypass",
                workspace_roles={},
                platform_role=PlatformRole.USER.value,
                development_bypass=True,
            )
        token = ""
        if authorization:
            scheme, _, bearer_token = authorization.partition(" ")
            if scheme.casefold() != "bearer" or not bearer_token.strip():
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED,
                    "请先登录",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            token = bearer_token.strip()
        else:
            token = request.cookies.get(AUTH_COOKIE_NAME, "").strip()
        if not token:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "请先登录",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return self.authenticate(token)
        except InvalidCredentials as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    def require_workspace(
        self,
        principal: AuthPrincipal,
        workspace_id: str,
        *,
        write: bool = False,
        admin: bool = False,
    ) -> str:
        if principal.development_bypass:
            return "OWNER"
        with self.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            if not workspace:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "工作空间不存在")
            if workspace.status != "ACTIVE":
                raise HTTPException(status.HTTP_403_FORBIDDEN, "该工作空间已停用")
        role = principal.workspace_roles.get(workspace_id, "")
        required_rank = ROLE_RANK["ADMIN"] if admin else ROLE_RANK["EDITOR"] if write else ROLE_RANK["VIEWER"]
        if ROLE_RANK.get(role, 0) < required_rank:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "你无权访问该工作空间")
        return role

    def require_admin(self, principal: AuthPrincipal, *, super_admin: bool = False) -> str:
        """Enforce platform RBAC independently of workspace membership."""

        role = principal.platform_role
        allowed = {PlatformRole.SUPER_ADMIN.value}
        if not super_admin:
            allowed.add(PlatformRole.ADMIN.value)
        if principal.development_bypass or role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "platform administrator access required")
        return role

    def require_project(
        self,
        principal: AuthPrincipal,
        project_id: str,
        *,
        write: bool = False,
        admin: bool = False,
    ) -> Project:
        with self.database.session() as session:
            project = session.get(Project, project_id)
            if not project:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
            if project.status != "ACTIVE" and not principal.development_bypass:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "该项目已停用")
            if not project.workspace_id and not principal.development_bypass:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "该项目尚未归属可访问的工作空间")
            if project.workspace_id:
                self.require_workspace(principal, project.workspace_id, write=write, admin=admin)
            return project

    def first_workspace_id(
        self,
        principal: AuthPrincipal,
        *,
        write: bool = False,
        admin: bool = False,
    ) -> str | None:
        if principal.development_bypass:
            return None
        required_rank = ROLE_RANK["ADMIN"] if admin else ROLE_RANK["EDITOR"] if write else ROLE_RANK["VIEWER"]
        for workspace_id, role in principal.workspace_roles.items():
            if ROLE_RANK.get(role, 0) < required_rank:
                continue
            try:
                self.require_workspace(principal, workspace_id, write=write, admin=admin)
            except HTTPException:
                continue
            return workspace_id
        return None

    def user_view(self, principal: AuthPrincipal) -> dict[str, Any]:
        workspace_details: dict[str, Workspace] = {}
        if principal.workspace_roles:
            with self.database.session() as session:
                workspace_details = {
                    item.id: item
                    for item in session.scalars(
                        select(Workspace).where(Workspace.id.in_(principal.workspace_roles))
                    )
                }
        return {
            "id": principal.user_id,
            "email": principal.email,
            "display_name": principal.display_name,
            "platform_role": principal.platform_role,
            "workspaces": [
                {
                    "id": workspace_id,
                    "role": role,
                    "plan_tier": (
                        workspace_details[workspace_id].plan_tier
                        if workspace_id in workspace_details
                        else "FREE"
                    ),
                    "storage": (
                        {
                            "max_bytes": workspace_details[workspace_id].max_storage_bytes,
                            "used_bytes": workspace_details[workspace_id].used_storage_bytes,
                            "reserved_bytes": (workspace_details[workspace_id].reserved_storage_bytes),
                        }
                        if workspace_id in workspace_details
                        else None
                    ),
                }
                for workspace_id, role in principal.workspace_roles.items()
            ],
        }

    def register_routes(
        self,
        app,
        verify_internal: Callable[..., Any] | None = None,
    ) -> None:  # type: ignore[no-untyped-def]
        router = APIRouter(prefix="/api/auth", tags=["authentication"])

        @router.post("/register", status_code=status.HTTP_201_CREATED)
        def register(body: RegisterRequest, request: Request, response: Response):
            try:
                issued = self.register(body, user_agent=request.headers.get("user-agent", ""))
            except ValueError as exc:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
            except RegistrationConflict as exc:
                raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
            self.set_session_cookies(response, issued)
            return self._issued_view(issued)

        @router.post("/login")
        def login(body: LoginRequest, request: Request, response: Response):
            try:
                issued = self.login(
                    body,
                    user_agent=request.headers.get("user-agent", ""),
                    client_ip=request.client.host if request.client else "",
                )
            except LoginThrottled as exc:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    str(exc),
                    headers={"Retry-After": str(exc.retry_after_seconds)},
                ) from exc
            except InvalidCredentials as exc:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED,
                    str(exc),
                    headers={"WWW-Authenticate": "Bearer"},
                ) from exc
            self.set_session_cookies(response, issued)
            return self._issued_view(issued)

        @router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
        def logout(
            response: Response,
            principal: AuthPrincipal = Depends(self.current_user),
        ) -> None:
            self.revoke(principal)
            self.clear_session_cookies(response)

        @router.post("/password-reset/request")
        def request_password_reset(body: PasswordResetRequest, request: Request):
            issued = self.request_password_reset(
                body,
                client_ip=request.client.host if request.client else "",
            )
            response: dict[str, Any] = {
                "message": "如果该邮箱存在，重置说明已发送",
            }
            if self.deployment_environment in {"development", "test"} and issued.token:
                response["reset_token"] = issued.token
                response["expires_at"] = issued.expires_at.isoformat() if issued.expires_at else None
            return response

        @router.post("/password-reset/confirm")
        def confirm_password_reset(body: PasswordResetConfirmRequest, response: Response):
            try:
                self.confirm_password_reset(body)
            except (PasswordResetRejected, ValueError) as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
            self.clear_session_cookies(response)
            return {"message": "密码已重置，请重新登录"}

        @router.get("/me")
        def me(principal: AuthPrincipal = Depends(self.current_user)):
            return self.user_view(principal)

        app.include_router(router)
        if verify_internal is not None:
            internal = APIRouter(
                prefix="/internal/auth",
                tags=["internal-auth"],
                dependencies=[Depends(verify_internal)],
            )

            @internal.post("/legacy-workspaces/claim")
            def claim_legacy_workspaces(body: LegacyWorkspaceClaimRequest):
                try:
                    claim = self.claim_legacy_workspaces(body)
                except LegacyClaimTargetNotFound as exc:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
                except LegacyClaimConflict as exc:
                    raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
                except ValueError as exc:
                    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
                return {
                    "claim_id": claim.claim_id,
                    "target_user_id": claim.target_user_id,
                    "workspace_ids": claim.workspace_ids,
                    "project_ids": claim.project_ids,
                    "replayed": claim.replayed,
                }

            app.include_router(internal)

    def _issued_view(self, issued: IssuedSession) -> dict[str, Any]:
        return {
            "access_token": issued.token,
            "token_type": "bearer",
            "expires_at": issued.expires_at.isoformat(),
            "user": self.user_view(issued.principal),
        }
