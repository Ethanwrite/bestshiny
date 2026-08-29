from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from production_domain.models import (
    AdminAuditLog,
    AdminCreditAdjustment,
    AuthSession,
    BrowserWorker,
    Episode,
    GenerationEvent,
    GenerationJob,
    MediaAsset,
    ModelCapabilityProfile,
    ModelDefinition,
    ModelMetric,
    ModelRoleBinding,
    ModelVerification,
    OnchainPayment,
    PlatformRole,
    Project,
    ProviderAccount,
    ProviderControl,
    ProviderCredential,
    Scene,
    Shot,
    User,
    Workspace,
    WorkspaceCreditEntry,
    WorkspaceCreditEvent,
    WorkspaceCreditLedgerEntry,
)
from provider_sdk import ProviderHealth
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import case, desc, func, literal, or_, select, text, union_all

from .admin_service import (
    AdminOperationConflict,
    AdminOperationService,
    LifecycleTransitionDenied,
    redact,
)
from .auth import AuthPrincipal, AuthService
from .container import Container

_SECRET_TEXT = re.compile(r"(?i)(bearer\s+|api[_ -]?key[=: ]+|secret[=: ]+|token[=: ]+)([^\s,;]{6,})")


def _safe_text(value: str | None, limit: int = 800) -> str | None:
    if value is None:
        return None
    return _SECRET_TEXT.sub(r"\1[REDACTED]", value)[:limit]


_NOT_CONFIGURED_DETAIL = "No generation transport is configured"


def _probe_status(health: ProviderHealth) -> str:
    """What a finished probe means, without collapsing absence into failure.

    Every adapter reports a missing transport the same way — `ProviderHealth(
    False, "NOT_CONFIGURED", {"status": "NOT_CONFIGURED", ...})` — so `ok=False`
    covers two different facts: "this provider is broken" and "this provider was
    never wired up". Mapping both to `DOWN` badges an unconfigured provider red
    on Providers and System Health, and red is reserved here for real failure.
    The probe knows which of the two it is, so its own verdict wins over the
    registry's cheaper `is_configured()` guess.
    """

    if health.ok:
        return "HEALTHY"
    reported = str(health.metadata.get("status") or health.detail or "").strip().upper()
    return "NOT_CONFIGURED" if reported == "NOT_CONFIGURED" else "DOWN"


def _probe_detail(health: ProviderHealth, status: str) -> str | None:
    """The probe's own words, except when they only repeat the badge."""

    if status == "NOT_CONFIGURED" and health.detail.strip().upper() == "NOT_CONFIGURED":
        return _NOT_CONFIGURED_DETAIL
    return _safe_text(health.detail)


def _page(items: list[Any], *, total: int, limit: int, offset: int) -> dict[str, Any]:
    return {"items": items, "pagination": {"total": total, "limit": limit, "offset": offset}}


def _request_id(request: Request) -> str:
    return (request.headers.get("X-Request-ID") or str(uuid.uuid4()))[:160]


class ReasonCommand(BaseModel):
    reason: str = Field(min_length=8, max_length=500)


class CreditAdjustmentCommand(ReasonCommand):
    workspace_id: str
    delta: int = Field(ge=-1_000_000, le=1_000_000)
    reference: str | None = Field(default=None, max_length=240)


class UserStatusCommand(ReasonCommand):
    status: Literal["ACTIVE", "SUSPENDED"]


class UserRoleCommand(ReasonCommand):
    role: Literal["USER", "ADMIN", "SUPER_ADMIN"]


class PlanCommand(ReasonCommand):
    workspace_id: str
    plan_tier: Literal["FREE", "PRO", "ENTERPRISE"]


class LifecycleCommand(ReasonCommand):
    target_status: Literal["DISABLED", "CONFIGURED", "TESTING", "VERIFIED", "LIVE", "DEGRADED", "BLOCKED"]


class RouterCommand(ReasonCommand):
    enabled: bool


class PricingMetadataCommand(BaseModel):
    billing_unit: Literal["GENERATION", "SECOND", "IMAGE", "1K_TOKENS"]
    credits: int = Field(ge=0, le=1_000_000)
    currency: Literal["USD", "USDC", "CREDITS"]
    amount: float = Field(ge=0, le=1_000_000)


class ModelMetadataCommand(ReasonCommand):
    display_name: str = Field(min_length=1, max_length=200)
    user_visible: bool
    pricing_metadata: PricingMetadataCommand


class ModelCapabilitiesCommand(ReasonCommand):
    supported_operations: list[str] = Field(min_length=1, max_length=50)
    supports_image_generation: bool
    supports_video_generation: bool
    supports_t2v: bool
    supports_i2v: bool
    supports_v2v: bool
    supports_reference_image: bool
    supports_multi_reference: bool
    supports_start_frame: bool
    supports_end_frame: bool
    supports_audio: bool
    max_reference_images: int = Field(ge=0, le=16)
    min_duration: float | None = Field(default=None, gt=0)
    max_duration: float | None = Field(default=None, gt=0)
    supported_aspect_ratios: list[str] = Field(default_factory=list, max_length=30)
    supported_resolutions: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_capability_contract(self) -> Self:
        self.supported_operations = _normalized_tokens(self.supported_operations, "supported_operations")
        self.supported_aspect_ratios = _normalized_tokens(
            self.supported_aspect_ratios, "supported_aspect_ratios"
        )
        self.supported_resolutions = _normalized_tokens(self.supported_resolutions, "supported_resolutions")
        if self.min_duration and self.max_duration and self.min_duration > self.max_duration:
            raise ValueError("min_duration cannot be greater than max_duration")
        if (
            self.supports_t2v or self.supports_i2v or self.supports_v2v
        ) and not self.supports_video_generation:
            raise ValueError("video modes require supports_video_generation")
        if (
            self.supports_multi_reference or self.supports_start_frame or self.supports_end_frame
        ) and not self.supports_reference_image:
            raise ValueError("reference-dependent modes require supports_reference_image")
        if not self.supports_reference_image and self.max_reference_images:
            raise ValueError("max_reference_images must be zero when references are unsupported")
        if self.supports_multi_reference and self.max_reference_images < 2:
            raise ValueError("multi-reference support requires max_reference_images >= 2")
        return self


class VerificationCommand(BaseModel):
    protocol_version: str = Field(min_length=1, max_length=120)
    result: Literal["SUCCESS", "FAILED"]
    evidence_reference: str = Field(min_length=1, max_length=500)
    billable: bool = False
    latency_ms: float | None = Field(default=None, ge=0)
    detail: str | None = Field(default=None, max_length=500)


class ProviderEnablementCommand(ReasonCommand):
    enabled: bool


def _normalized_tokens(values: list[str], field_name: str) -> list[str]:
    normalized = list(dict.fromkeys(item.strip() for item in values if item.strip()))
    if field_name == "supported_operations" and not normalized:
        raise ValueError("supported_operations cannot be empty")
    if any(len(item) > 80 for item in normalized):
        raise ValueError(f"{field_name} values cannot exceed 80 characters")
    return normalized


def _model_view(model: ModelDefinition, profile: ModelCapabilityProfile | None) -> dict[str, Any]:
    operations = list(profile.supported_operations) if profile else []
    return {
        "id": model.id,
        "internal_key": model.logical_name,
        "display_name": model.display_name or model.logical_name,
        "provider": model.provider,
        "provider_model_id": model.provider_model_id,
        "capability": model.modality.upper(),
        "generation_modes": operations,
        "configured": not model.provider_model_id.startswith("CONFIGURE_"),
        "verified": model.last_verified_at is not None,
        "lifecycle_status": model.lifecycle_status,
        "router_enabled": model.router_enabled,
        "router_eligible": bool(
            model.enabled and model.router_enabled and model.lifecycle_status in {"LIVE", "DEGRADED"}
        ),
        "enabled": model.enabled,
        "live_enabled": model.live_enabled,
        "user_visible": model.user_visible,
        "quality_tier": model.quality_tier,
        "cost_class": model.cost_class,
        "cost_metadata": redact(dict(profile.provider_metadata)) if profile else {},
        "user_pricing": redact(dict(model.pricing_metadata)),
        "last_verified_at": model.last_verified_at,
        "last_live_test_at": model.last_live_test_at,
        # Deliberately separate from `verified` above. That flag follows
        # `last_verified_at`, which an admin moves by recording a manual
        # verification; this is the canary's own verdict, written only by a
        # closed live loop. Conflating them would let a reviewed model read as
        # production-proven, which is the one thing this column exists to stop.
        "live_canary_status": model.live_canary_status,
        "live_canary_detail": model.live_canary_detail,
        "health": "UNKNOWN",
    }


def register_admin_routes(
    app: FastAPI,
    container: Container,
    auth: AuthService,
    verify_api_key,
) -> None:  # type: ignore[no-untyped-def]
    service = AdminOperationService(container.database)
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    def admin_principal(principal: AuthPrincipal = Depends(auth.current_user)) -> AuthPrincipal:
        auth.require_admin(principal)
        return principal

    def super_principal(principal: AuthPrincipal = Depends(auth.current_user)) -> AuthPrincipal:
        auth.require_admin(principal, super_admin=True)
        return principal

    @router.get("/session")
    def admin_session(principal: AuthPrincipal = Depends(admin_principal)):
        return {
            "user_id": principal.user_id,
            "email": principal.email,
            "display_name": principal.display_name,
            "platform_role": principal.platform_role,
        }

    @router.get("/dashboard")
    async def dashboard(_principal: AuthPrincipal = Depends(admin_principal)):
        now = datetime.now(UTC)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week = now - timedelta(days=7)
        with container.database.session() as session:
            total_users = int(session.scalar(select(func.count(User.id))) or 0)
            account_active_users = int(
                session.scalar(select(func.count(User.id)).where(User.status == "ACTIVE")) or 0
            )
            active_users = int(
                session.scalar(
                    select(func.count(func.distinct(AuthSession.user_id))).where(
                        AuthSession.last_used_at >= week,
                        AuthSession.revoked_at.is_(None),
                        AuthSession.expires_at > now,
                    )
                )
                or 0
            )
            plan_rows: dict[str, int] = {
                str(key): int(value)
                for key, value in session.execute(
                    select(Workspace.plan_tier, func.count(Workspace.id)).group_by(Workspace.plan_tier)
                ).all()
            }

            def job_window(start: datetime) -> dict[str, Any]:
                rows: dict[str, int] = {
                    str(key): int(value)
                    for key, value in session.execute(
                        select(GenerationJob.status, func.count(GenerationJob.id))
                        .where(GenerationJob.created_at >= start)
                        .group_by(GenerationJob.status)
                    ).all()
                }
                completed = int(rows.get("COMPLETED", 0))
                failed = int(rows.get("FAILED", 0))
                terminal = completed + failed
                return {
                    "window_start": start,
                    "total": sum(rows.values()),
                    "statuses": {str(key): int(value) for key, value in rows.items()},
                    "success_rate": round(completed / terminal, 4) if terminal else None,
                    "failure_rate": round(failed / terminal, 4) if terminal else None,
                    "rate_coverage": "COMPLETE" if terminal else "NO_TERMINAL_JOBS",
                }

            credits_7d = int(
                session.scalar(
                    select(func.coalesce(func.sum(WorkspaceCreditEvent.credits), 0)).where(
                        WorkspaceCreditEvent.created_at >= week,
                        WorkspaceCreditEvent.event_type.in_(("SETTLED", "RECONCILED_SETTLED")),
                    )
                )
                or 0
            )
            revenue_micro = int(
                session.scalar(
                    select(func.coalesce(func.sum(OnchainPayment.raw_amount_microunits), 0)).where(
                        OnchainPayment.created_at >= week,
                        OnchainPayment.status == "CREDITED",
                    )
                )
                or 0
            )
            recent_errors = list(
                session.scalars(
                    select(GenerationJob)
                    .where(GenerationJob.status == "FAILED")
                    .order_by(desc(GenerationJob.created_at))
                    .limit(10)
                )
            )
            live_models = int(
                session.scalar(
                    select(func.count(ModelDefinition.id)).where(ModelDefinition.lifecycle_status == "LIVE")
                )
                or 0
            )
            disabled_providers = set(
                session.scalars(select(ProviderControl.provider).where(ProviderControl.enabled.is_(False)))
            )
            # Called here, not in the response literal below. `Database.session()`
            # closes the session on exit, and a query issued afterwards silently
            # opens a *new* transaction on a *new* connection that nothing ever
            # commits or closes — one PostgreSQL backend stranded `idle in
            # transaction`, holding its locks, for every dashboard load.
            jobs_today = job_window(today)
            jobs_7d = job_window(week)
        provider_counts = {"HEALTHY": 0, "DEGRADED": 0, "DOWN": 0, "NOT_CONFIGURED": 0}
        for name in container.providers.list():
            if not container.providers.is_configured(name):
                provider_counts["NOT_CONFIGURED"] += 1
                continue
            if name in disabled_providers:
                provider_counts["DEGRADED"] += 1
                continue
            try:
                probe = await container.providers.get(name).health()
                provider_counts[_probe_status(probe)] += 1
            except Exception:  # A failed probe is data for this aggregation.
                provider_counts["DOWN"] += 1
        return {
            "as_of": now,
            "users": {
                "total": total_users,
                "active": active_users,
                "active_window": "7d",
                "account_status_active": account_active_users,
                "plans": plan_rows,
            },
            "jobs_today": jobs_today,
            "jobs_7d": jobs_7d,
            "credits_consumed_7d": credits_7d,
            "revenue_7d": {
                "amount_usdc": revenue_micro / 1_000_000,
                "coverage": "AUTHENTICATED_CREDITED_ONCHAIN_PAYMENTS_ONLY",
            },
            "providers": provider_counts,
            "live_models": live_models,
            "recent_errors": [
                {
                    "id": job.id,
                    "provider": job.provider,
                    "model": job.model,
                    "error_code": job.error_code,
                    "error_message": _safe_text(job.error_message),
                    "created_at": job.created_at,
                }
                for job in recent_errors
            ],
        }

    @router.get("/users")
    def users(
        q: Annotated[str | None, Query(max_length=320)] = None,
        status: Annotated[str | None, Query(max_length=40)] = None,
        plan: Annotated[str | None, Query(max_length=40)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        _principal: AuthPrincipal = Depends(admin_principal),
    ):
        job_counts = (
            select(Project.workspace_id.label("workspace_id"), func.count(GenerationJob.id).label("jobs"))
            .join(GenerationJob, GenerationJob.project_id == Project.id)
            .group_by(Project.workspace_id)
            .subquery()
        )
        statement = (
            select(User, Workspace, func.coalesce(job_counts.c.jobs, 0))
            .outerjoin(Workspace, Workspace.owner_user_id == User.id)
            .outerjoin(job_counts, job_counts.c.workspace_id == Workspace.id)
        )
        filters = []
        if q:
            pattern = f"%{q.strip()}%"
            filters.append(or_(User.email.ilike(pattern), User.id.ilike(pattern)))
        if status:
            filters.append(User.status == status.strip().upper())
        if plan:
            filters.append(Workspace.plan_tier == plan.strip().upper())
        if filters:
            statement = statement.where(*filters)
        with container.database.session() as session:
            total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            rows = session.execute(
                statement.order_by(desc(User.created_at), User.id).offset(offset).limit(limit)
            ).all()
            last_activity: dict[str, datetime] = {
                str(key): value
                for key, value in session.execute(
                    select(AuthSession.user_id, func.max(AuthSession.last_used_at))
                    .where(AuthSession.user_id.in_([row[0].id for row in rows]))
                    .group_by(AuthSession.user_id)
                ).all()
            }
            reserved: dict[str, int] = {
                str(key): int(value)
                for key, value in session.execute(
                    select(WorkspaceCreditEntry.workspace_id, func.sum(WorkspaceCreditEntry.credits))
                    .where(
                        WorkspaceCreditEntry.workspace_id.in_([row[1].id for row in rows if row[1]]),
                        WorkspaceCreditEntry.status.in_(("RESERVED", "RECONCILIATION_REQUIRED")),
                    )
                    .group_by(WorkspaceCreditEntry.workspace_id)
                ).all()
            }
        items = []
        for user, workspace, generations in rows:
            items.append(
                {
                    "id": user.id,
                    "email": user.email,
                    "display_name": user.display_name,
                    "status": user.status,
                    "platform_role": user.platform_role,
                    "plan": workspace.plan_tier if workspace else None,
                    "workspace_id": workspace.id if workspace else None,
                    "credits_balance": workspace.credit_balance if workspace else None,
                    "reserved_credits": int(reserved.get(workspace.id, 0)) if workspace else 0,
                    "generation_count": int(generations),
                    "created_at": user.created_at,
                    "last_activity": last_activity.get(user.id),
                }
            )
        return _page(items, total=total, limit=limit, offset=offset)

    @router.get("/users/{user_id}")
    def user_detail(user_id: str, _principal: AuthPrincipal = Depends(admin_principal)):
        with container.database.session() as session:
            user = session.get(User, user_id)
            if user is None:
                raise HTTPException(404, "user not found")
            workspaces = list(
                session.scalars(
                    select(Workspace).where(Workspace.owner_user_id == user.id).order_by(Workspace.created_at)
                )
            )
            workspace_ids = [item.id for item in workspaces]
            projects = list(
                session.scalars(
                    select(Project)
                    .where(Project.workspace_id.in_(workspace_ids))
                    .order_by(desc(Project.created_at))
                    .limit(100)
                )
            )
            project_ids = [item.id for item in projects]
            jobs = list(
                session.scalars(
                    select(GenerationJob)
                    .where(GenerationJob.project_id.in_(project_ids))
                    .order_by(desc(GenerationJob.created_at))
                    .limit(100)
                )
            )
            credit_entries = list(
                session.scalars(
                    select(WorkspaceCreditEntry)
                    .where(WorkspaceCreditEntry.workspace_id.in_(workspace_ids))
                    .order_by(desc(WorkspaceCreditEntry.created_at))
                    .limit(200)
                )
            )
            adjustments = list(
                session.scalars(
                    select(AdminCreditAdjustment)
                    .where(AdminCreditAdjustment.user_id == user.id)
                    .order_by(desc(AdminCreditAdjustment.created_at))
                    .limit(200)
                )
            )
            billing = list(
                session.scalars(
                    select(WorkspaceCreditLedgerEntry)
                    .where(WorkspaceCreditLedgerEntry.workspace_id.in_(workspace_ids))
                    .order_by(desc(WorkspaceCreditLedgerEntry.created_at))
                    .limit(200)
                )
            )
        return {
            "account": {
                "id": user.id,
                "email": user.email,
                "display_name": user.display_name,
                "status": user.status,
                "platform_role": user.platform_role,
                "created_at": user.created_at,
            },
            "plan": [{"workspace_id": w.id, "name": w.name, "plan_tier": w.plan_tier} for w in workspaces],
            "credits": [
                {
                    "workspace_id": w.id,
                    "available": w.credit_balance,
                    "reserved": sum(
                        e.credits
                        for e in credit_entries
                        if e.workspace_id == w.id and e.status in {"RESERVED", "RECONCILIATION_REQUIRED"}
                    ),
                    "charged": sum(e.settled_credits for e in credit_entries if e.workspace_id == w.id),
                    "released": sum(e.refunded_credits for e in credit_entries if e.workspace_id == w.id),
                }
                for w in workspaces
            ],
            "usage": {"generation_count": len(jobs), "coverage": "LATEST_100_GENERATIONS"},
            "projects": [
                {"id": p.id, "title": p.title, "status": p.status, "created_at": p.created_at}
                for p in projects
            ],
            "generations": [_job_summary(job) for job in jobs],
            "billing_events": [
                {
                    "id": e.id,
                    "type": e.entry_type,
                    "direction": e.direction,
                    "credits": e.credits,
                    "before": e.balance_before,
                    "after": e.balance_after,
                    "created_at": e.created_at,
                }
                for e in billing
            ]
            + [
                {
                    "id": a.id,
                    "type": "MANUAL_ADJUSTMENT",
                    "direction": "CREDIT" if a.delta > 0 else "DEBIT",
                    "credits": abs(a.delta),
                    "before": a.before_balance,
                    "after": a.after_balance,
                    "reason": a.reason,
                    "created_at": a.created_at,
                }
                for a in adjustments
            ],
        }

    @router.post("/users/{user_id}/credit-adjustments", status_code=201)
    def adjust_user_credits(
        user_id: str,
        body: CreditAdjustmentCommand,
        request: Request,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        principal: AuthPrincipal = Depends(admin_principal),
    ):
        if principal.platform_role != PlatformRole.SUPER_ADMIN.value:
            raise HTTPException(403, "SUPER_ADMIN is required for manual credit adjustments")
        try:
            result = service.adjust_credits(
                user_id=user_id,
                workspace_id=body.workspace_id,
                delta=body.delta,
                reason=body.reason,
                reference=body.reference,
                idempotency_key=idempotency_key or "",
                actor=principal,
                request_id=_request_id(request),
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except AdminOperationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        item = result.adjustment
        return {
            "id": item.id,
            "workspace_id": item.workspace_id,
            "delta": item.delta,
            "before_balance": item.before_balance,
            "after_balance": item.after_balance,
            "reason": item.reason,
            "reference": item.reference,
            "created_at": item.created_at,
            "replayed": result.replayed,
        }

    @router.post("/users/{user_id}/status")
    def change_user_status(
        user_id: str,
        body: UserStatusCommand,
        request: Request,
        principal: AuthPrincipal = Depends(admin_principal),
    ):
        try:
            user = service.set_user_status(
                user_id=user_id,
                target_status=body.status,
                reason=body.reason,
                actor=principal,
                request_id=_request_id(request),
            )
            return {"id": user.id, "status": user.status}
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/users/{user_id}/platform-role")
    def change_platform_role(
        user_id: str,
        body: UserRoleCommand,
        request: Request,
        principal: AuthPrincipal = Depends(super_principal),
    ):
        try:
            user = service.set_user_role(
                user_id=user_id,
                target_role=body.role,
                reason=body.reason,
                actor=principal,
                request_id=_request_id(request),
            )
            return {"id": user.id, "platform_role": user.platform_role}
        except (LookupError, ValueError) as exc:
            raise HTTPException(422 if isinstance(exc, ValueError) else 404, str(exc)) from exc

    @router.post("/users/{user_id}/plan")
    def change_plan(
        user_id: str,
        body: PlanCommand,
        request: Request,
        principal: AuthPrincipal = Depends(super_principal),
    ):
        try:
            workspace = service.set_plan(
                user_id=user_id,
                workspace_id=body.workspace_id,
                target_plan=body.plan_tier,
                reason=body.reason,
                actor=principal,
                request_id=_request_id(request),
            )
            return {"workspace_id": workspace.id, "plan_tier": workspace.plan_tier}
        except (LookupError, ValueError) as exc:
            raise HTTPException(422 if isinstance(exc, ValueError) else 404, str(exc)) from exc

    @router.get("/credits")
    def credits(
        user_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
        generation_id: str | None = None,
        event_type: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        _principal: AuthPrincipal = Depends(admin_principal),
    ):
        with container.database.session() as session:
            workspace_ids: list[str] | None = None
            if user_id:
                workspace_ids = list(
                    session.scalars(select(Workspace.id).where(Workspace.owner_user_id == user_id))
                )
            lifecycle_events = select(
                WorkspaceCreditEvent.id.label("id"),
                WorkspaceCreditEvent.workspace_id.label("workspace_id"),
                WorkspaceCreditEvent.project_id.label("project_id"),
                WorkspaceCreditEvent.generation_job_id.label("generation_job_id"),
                literal("GENERATION_LIFECYCLE").label("source"),
                WorkspaceCreditEvent.event_type.label("event_type"),
                WorkspaceCreditEvent.credits.label("credits"),
                WorkspaceCreditEvent.balance_delta.label("balance_delta"),
                WorkspaceCreditEvent.balance_after.label("balance_after"),
                WorkspaceCreditEvent.reason.label("reason"),
                WorkspaceCreditEvent.actor_type.label("actor_type"),
                WorkspaceCreditEvent.created_at.label("created_at"),
            )
            adjustments = select(
                AdminCreditAdjustment.id,
                AdminCreditAdjustment.workspace_id,
                literal(None).label("project_id"),
                literal(None).label("generation_job_id"),
                literal("ADMIN_ADJUSTMENT").label("source"),
                literal("MANUAL_ADJUSTMENT").label("event_type"),
                func.abs(AdminCreditAdjustment.delta).label("credits"),
                AdminCreditAdjustment.delta.label("balance_delta"),
                AdminCreditAdjustment.after_balance.label("balance_after"),
                AdminCreditAdjustment.reason.label("reason"),
                literal("ADMIN").label("actor_type"),
                AdminCreditAdjustment.created_at.label("created_at"),
            )
            purchases = select(
                WorkspaceCreditLedgerEntry.id,
                WorkspaceCreditLedgerEntry.workspace_id,
                literal(None).label("project_id"),
                literal(None).label("generation_job_id"),
                literal("PURCHASE_LEDGER").label("source"),
                WorkspaceCreditLedgerEntry.entry_type.label("event_type"),
                WorkspaceCreditLedgerEntry.credits.label("credits"),
                case(
                    (WorkspaceCreditLedgerEntry.direction == "CREDIT", WorkspaceCreditLedgerEntry.credits),
                    else_=-WorkspaceCreditLedgerEntry.credits,
                ).label("balance_delta"),
                WorkspaceCreditLedgerEntry.balance_after.label("balance_after"),
                WorkspaceCreditLedgerEntry.external_reference.label("reason"),
                literal("PAYMENT").label("actor_type"),
                WorkspaceCreditLedgerEntry.created_at.label("created_at"),
            )
            combined = union_all(lifecycle_events, adjustments, purchases).subquery()
            statement = select(combined)
            if workspace_ids is not None:
                statement = statement.where(combined.c.workspace_id.in_(workspace_ids))
            if workspace_id:
                statement = statement.where(combined.c.workspace_id == workspace_id)
            if project_id:
                statement = statement.where(combined.c.project_id == project_id)
            if generation_id:
                statement = statement.where(combined.c.generation_job_id == generation_id)
            if event_type:
                statement = statement.where(combined.c.event_type == event_type.upper())
            if created_from:
                statement = statement.where(combined.c.created_at >= created_from)
            if created_to:
                statement = statement.where(combined.c.created_at <= created_to)
            total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            events = (
                session.execute(
                    statement.order_by(desc(combined.c.created_at), combined.c.id).offset(offset).limit(limit)
                )
                .mappings()
                .all()
            )

            summary_workspaces = select(Workspace.id)
            if workspace_ids is not None:
                summary_workspaces = summary_workspaces.where(Workspace.id.in_(workspace_ids))
            if workspace_id:
                summary_workspaces = summary_workspaces.where(Workspace.id == workspace_id)
            scoped_ids = summary_workspaces.subquery()
            available = int(
                session.scalar(
                    select(func.coalesce(func.sum(Workspace.credit_balance), 0)).where(
                        Workspace.id.in_(select(scoped_ids.c.id))
                    )
                )
                or 0
            )
            held = int(
                session.scalar(
                    select(func.coalesce(func.sum(WorkspaceCreditEntry.credits), 0)).where(
                        WorkspaceCreditEntry.workspace_id.in_(select(scoped_ids.c.id)),
                        WorkspaceCreditEntry.status.in_(("RESERVED", "RECONCILIATION_REQUIRED")),
                    )
                )
                or 0
            )
            deducted = int(
                session.scalar(
                    select(func.coalesce(func.sum(WorkspaceCreditEntry.settled_credits), 0)).where(
                        WorkspaceCreditEntry.workspace_id.in_(select(scoped_ids.c.id))
                    )
                )
                or 0
            )
            released = int(
                session.scalar(
                    select(func.coalesce(func.sum(WorkspaceCreditEntry.refunded_credits), 0)).where(
                        WorkspaceCreditEntry.workspace_id.in_(select(scoped_ids.c.id))
                    )
                )
                or 0
            )
        response = _page(
            [
                {
                    "id": event["id"],
                    "workspace_id": event["workspace_id"],
                    "project_id": event["project_id"],
                    "generation_job_id": event["generation_job_id"],
                    "source": event["source"],
                    "event_type": event["event_type"],
                    "credits": event["credits"],
                    "balance_delta": event["balance_delta"],
                    "balance_after": event["balance_after"],
                    "reason": event["reason"],
                    "actor_type": event["actor_type"],
                    "created_at": event["created_at"],
                }
                for event in events
            ],
            total=total,
            limit=limit,
            offset=offset,
        )
        response["summary"] = {
            "available": available,
            "held": held,
            "deducted": deducted,
            "released": released,
        }
        return response

    @router.get("/models")
    def models(
        q: Annotated[str | None, Query(max_length=320)] = None,
        provider: str | None = None,
        lifecycle: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        _principal: AuthPrincipal = Depends(admin_principal),
    ):
        statement = select(ModelDefinition, ModelCapabilityProfile).outerjoin(
            ModelCapabilityProfile,
            ModelCapabilityProfile.model_definition_id == ModelDefinition.id,
        )
        if provider:
            statement = statement.where(ModelDefinition.provider == provider)
        if q:
            pattern = f"%{q.strip()}%"
            statement = statement.where(
                or_(
                    ModelDefinition.logical_name.ilike(pattern),
                    ModelDefinition.display_name.ilike(pattern),
                    ModelDefinition.provider_model_id.ilike(pattern),
                    ModelDefinition.provider.ilike(pattern),
                )
            )
        if lifecycle:
            statement = statement.where(ModelDefinition.lifecycle_status == lifecycle.upper())
        with container.database.session() as session:
            total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            rows = session.execute(
                statement.order_by(ModelDefinition.provider, ModelDefinition.logical_name)
                .offset(offset)
                .limit(limit)
            ).all()
        return _page([_model_view(*row) for row in rows], total=total, limit=limit, offset=offset)

    @router.get("/models/{model_id}")
    def model_detail(model_id: str, _principal: AuthPrincipal = Depends(admin_principal)):
        with container.database.session() as session:
            row = session.execute(
                select(ModelDefinition, ModelCapabilityProfile)
                .outerjoin(
                    ModelCapabilityProfile, ModelCapabilityProfile.model_definition_id == ModelDefinition.id
                )
                .where(ModelDefinition.id == model_id)
            ).one_or_none()
            if row is None:
                raise HTTPException(404, "model not found")
            model, profile = row
            bindings = list(
                session.scalars(
                    select(ModelRoleBinding).where(ModelRoleBinding.model_definition_id == model.id)
                )
            )
            verifications = list(
                session.scalars(
                    select(ModelVerification)
                    .where(ModelVerification.model_definition_id == model.id)
                    .order_by(desc(ModelVerification.created_at))
                    .limit(100)
                )
            )
            audit = list(
                session.scalars(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.entity_type == "MODEL", AdminAuditLog.entity_id == model.id)
                    .order_by(desc(AdminAuditLog.created_at))
                    .limit(100)
                )
            )
        return {
            **_model_view(model, profile),
            "capabilities": _profile_view(profile),
            "provider_mapping": {"provider": model.provider, "provider_model_id": model.provider_model_id},
            "router_bindings": [
                {
                    "role": b.role,
                    "plan_tier": b.plan_tier,
                    "kind": b.binding_kind,
                    "priority": b.priority,
                    "enabled": b.enabled,
                }
                for b in bindings
            ],
            "verifications": [_verification_view(v) for v in verifications],
            "health_history": {"items": [], "coverage": "NOT_RECORDED"},
            "audit_history": [_audit_view(item) for item in audit],
        }

    @router.post("/models/{model_id}/verifications", status_code=201)
    def verify_model(
        model_id: str,
        body: VerificationCommand,
        request: Request,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        principal: AuthPrincipal = Depends(super_principal),
    ):
        try:
            item, replayed = service.record_model_verification(
                model_id=model_id,
                protocol_version=body.protocol_version,
                result=body.result,
                evidence_reference=body.evidence_reference,
                billable=body.billable,
                latency_ms=body.latency_ms,
                detail=body.detail,
                idempotency_key=idempotency_key or "",
                actor=principal,
                request_id=_request_id(request),
            )
            return {**_verification_view(item), "replayed": replayed}
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/models/{model_id}/lifecycle-transition")
    def transition_model(
        model_id: str,
        body: LifecycleCommand,
        request: Request,
        principal: AuthPrincipal = Depends(admin_principal),
    ):
        with container.database.session() as session:
            model = session.get(ModelDefinition, model_id)
            if model is None:
                raise HTTPException(404, "model not found")
            control = session.get(ProviderControl, model.provider)
            provider_enabled = control is None or control.enabled
        try:
            updated = service.transition_model(
                model_id=model_id,
                target_status=body.target_status,
                reason=body.reason,
                actor=principal,
                request_id=_request_id(request),
                provider_configured=container.providers.is_configured(model.provider),
                provider_enabled=provider_enabled,
            )
            return _model_view(updated, None)
        except LifecycleTransitionDenied as exc:
            raise HTTPException(409, {"code": "LIFECYCLE_GATE_DENIED", "reasons": exc.reasons}) from exc
        except (LookupError, ValueError) as exc:
            raise HTTPException(422 if isinstance(exc, ValueError) else 404, str(exc)) from exc

    @router.post("/models/{model_id}/router")
    def set_model_router(
        model_id: str,
        body: RouterCommand,
        request: Request,
        principal: AuthPrincipal = Depends(admin_principal),
    ):
        try:
            model = service.set_model_router(
                model_id=model_id,
                enabled=body.enabled,
                reason=body.reason,
                actor=principal,
                request_id=_request_id(request),
            )
            return {"id": model.id, "router_enabled": model.router_enabled}
        except (LookupError, ValueError) as exc:
            raise HTTPException(422 if isinstance(exc, ValueError) else 404, str(exc)) from exc

    @router.post("/models/{model_id}/metadata")
    def update_model_metadata(
        model_id: str,
        body: ModelMetadataCommand,
        request: Request,
        principal: AuthPrincipal = Depends(super_principal),
    ):
        try:
            model = service.set_model_metadata(
                model_id=model_id,
                display_name=body.display_name,
                user_visible=body.user_visible,
                pricing_metadata=body.pricing_metadata.model_dump(),
                reason=body.reason,
                actor=principal,
                request_id=_request_id(request),
            )
            return _model_view(model, None)
        except (LookupError, ValueError) as exc:
            raise HTTPException(422 if isinstance(exc, ValueError) else 404, str(exc)) from exc

    @router.post("/models/{model_id}/capabilities")
    def update_model_capabilities(
        model_id: str,
        body: ModelCapabilitiesCommand,
        request: Request,
        principal: AuthPrincipal = Depends(super_principal),
    ):
        try:
            profile = service.set_model_capabilities(
                model_id=model_id,
                capabilities=body.model_dump(exclude={"reason"}),
                reason=body.reason,
                actor=principal,
                request_id=_request_id(request),
            )
            return {
                "model_id": model_id,
                "capabilities": _profile_view(profile),
                "lifecycle_status": "CONFIGURED",
                "router_enabled": False,
                "verification_invalidated": True,
            }
        except (LookupError, ValueError) as exc:
            raise HTTPException(422 if isinstance(exc, ValueError) else 404, str(exc)) from exc

    @router.get("/providers")
    async def providers(_principal: AuthPrincipal = Depends(admin_principal)):
        with container.database.session() as session:
            controls = {item.provider: item for item in session.scalars(select(ProviderControl))}
            definitions = list(session.scalars(select(ModelDefinition)))
            credentials = list(session.scalars(select(ProviderCredential)))
            accounts = list(session.scalars(select(ProviderAccount)))
        result = []
        for name in container.providers.list():
            configured = container.providers.is_configured(name)
            control = controls.get(name)
            matching_credentials = [
                cred for cred in credentials if cred.provider == name and cred.status != "REVOKED"
            ]
            health_status = "NOT_CONFIGURED"
            detail: str | None = _NOT_CONFIGURED_DETAIL
            if configured and control is not None and not control.enabled:
                health_status, detail = (
                    "BLOCKED",
                    control.disabled_reason or "Disabled by platform operations",
                )
            elif configured:
                try:
                    health = await container.providers.get(name).health()
                    health_status = _probe_status(health)
                    detail = _probe_detail(health, health_status)
                except Exception as exc:
                    health_status, detail = "DOWN", _safe_text(str(exc)) or "probe failed"
            matching_accounts = [account for account in accounts if account.provider == name]
            attempts = sum(account.success_count + account.error_count for account in matching_accounts)
            errors = sum(account.error_count for account in matching_accounts)
            result.append(
                {
                    "name": name,
                    "enabled": True if control is None else control.enabled,
                    "configured": configured,
                    "credential_present": bool(matching_credentials),
                    "credential_status": [
                        {
                            "status": item.status,
                            "masked_identifier": item.redacted_fingerprint,
                            "last_validated_at": item.last_validated_at,
                            "expires_at": item.expires_at,
                        }
                        for item in matching_credentials
                    ],
                    "environment": container.settings.deployment_environment,
                    "health": health_status,
                    "detail": _safe_text(detail),
                    "error_rate": round(errors / attempts, 4) if attempts else None,
                    "latency_ms": None,
                    "quota": None,
                    "last_successful_probe": max(
                        (a.last_success_at for a in matching_accounts if a.last_success_at), default=None
                    ),
                    "last_failed_probe": max(
                        (a.last_error_at for a in matching_accounts if a.last_error_at), default=None
                    ),
                    "supported_capabilities": sorted(
                        {cap for model in definitions if model.provider == name for cap in model.capabilities}
                    ),
                    "registered_models": [
                        {
                            "id": model.id,
                            "internal_key": model.logical_name,
                            "lifecycle": model.lifecycle_status,
                        }
                        for model in definitions
                        if model.provider == name
                    ],
                }
            )
        return {"as_of": datetime.now(UTC), "items": result}

    @router.post("/providers/{provider}/enablement")
    def provider_enablement(
        provider: str,
        body: ProviderEnablementCommand,
        request: Request,
        principal: AuthPrincipal = Depends(super_principal),
    ):
        if provider not in container.providers.list():
            raise HTTPException(404, "provider not found in the runtime registry")
        control = service.set_provider_enabled(
            provider=provider,
            enabled=body.enabled,
            reason=body.reason,
            actor=principal,
            request_id=_request_id(request),
        )
        return {"provider": control.provider, "enabled": control.enabled}

    @router.post("/providers/{provider}/probe")
    async def provider_probe(provider: str, _principal: AuthPrincipal = Depends(admin_principal)):
        if provider not in container.providers.list():
            raise HTTPException(404, "provider not found")
        if not container.providers.is_configured(provider):
            return {
                "provider": provider,
                "status": "NOT_CONFIGURED",
                "billable": False,
                "checked_at": datetime.now(UTC),
                "detail": "No metadata/configuration probe can run without a configured transport",
            }
        try:
            health = await container.providers.get(provider).health()
            probe_status = _probe_status(health)
            return {
                "provider": provider,
                "status": probe_status,
                "billable": False,
                "checked_at": datetime.now(UTC),
                "detail": _probe_detail(health, probe_status),
            }
        except Exception as exc:
            return {
                "provider": provider,
                "status": "DOWN",
                "billable": False,
                "checked_at": datetime.now(UTC),
                "detail": _safe_text(str(exc)),
            }

    @router.get("/routing")
    def routing(_principal: AuthPrincipal = Depends(admin_principal)):
        with container.database.session() as session:
            rows = session.execute(
                select(ModelDefinition, ModelCapabilityProfile).outerjoin(
                    ModelCapabilityProfile,
                    ModelCapabilityProfile.model_definition_id == ModelDefinition.id,
                )
            ).all()
            metrics = list(
                session.scalars(select(ModelMetric).order_by(desc(ModelMetric.created_at)).limit(500))
            )
        metric_map: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for metric in metrics:
            metric_map.setdefault((metric.provider, metric.model_id), []).append(
                {"name": metric.metric_name, "value": metric.value, "created_at": metric.created_at}
            )
        return {
            "router_version": container.video_router.version,
            "items": [
                {
                    **_model_view(model, profile),
                    "priority": None,
                    "fallback": None,
                    "evidence": metric_map.get((model.provider, model.provider_model_id), []),
                    "score": None,
                    "score_coverage": "REQUEST_DEPENDENT; use explain endpoint with real requirements",
                }
                for model, profile in rows
                if model.modality == "video"
            ],
        }

    @router.get("/jobs")
    def jobs(
        status: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        provider: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        _principal: AuthPrincipal = Depends(admin_principal),
    ):
        statement = (
            select(GenerationJob, Project, Workspace, User)
            .join(Project, Project.id == GenerationJob.project_id)
            .outerjoin(Workspace, Workspace.id == Project.workspace_id)
            .outerjoin(User, User.id == Workspace.owner_user_id)
        )
        if status:
            statement = statement.where(GenerationJob.status == status.upper())
        if user_id:
            statement = statement.where(User.id == user_id)
        if project_id:
            statement = statement.where(Project.id == project_id)
        if provider:
            statement = statement.where(GenerationJob.provider == provider)
        if created_from:
            statement = statement.where(GenerationJob.created_at >= created_from)
        if created_to:
            statement = statement.where(GenerationJob.created_at <= created_to)
        with container.database.session() as session:
            total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            rows = session.execute(
                statement.order_by(desc(GenerationJob.created_at)).offset(offset).limit(limit)
            ).all()
        return _page(
            [
                {
                    **_job_summary(job),
                    "user": {"id": user.id, "email": user.email} if user else None,
                    "project": {"id": project.id, "title": project.title},
                }
                for job, project, _workspace, user in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get("/jobs/{job_id}")
    def job_detail(job_id: str, _principal: AuthPrincipal = Depends(admin_principal)):
        with container.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise HTTPException(404, "job not found")
            events = list(
                session.scalars(
                    select(GenerationEvent)
                    .where(GenerationEvent.generation_job_id == job.id)
                    .order_by(GenerationEvent.created_at, GenerationEvent.id)
                )
            )
            credit = session.scalar(
                select(WorkspaceCreditEntry).where(WorkspaceCreditEntry.generation_job_id == job.id)
            )
            credit_events = list(
                session.scalars(
                    select(WorkspaceCreditEvent)
                    .where(WorkspaceCreditEvent.generation_job_id == job.id)
                    .order_by(WorkspaceCreditEvent.created_at, WorkspaceCreditEvent.id)
                )
            )
        return {
            **_job_summary(job),
            "canonical_request": redact(job.request_json),
            "provider_request": redact(job.provider_request_json),
            "provider_task_id": job.provider_job_id,
            "events": [
                {"type": event.event_type, "detail": redact(event.detail), "created_at": event.created_at}
                for event in events
            ],
            "credits": {
                "reservation": (
                    {
                        "id": credit.id,
                        "status": credit.status,
                        "reserved": credit.credits,
                        "charged": credit.settled_credits,
                        "released": credit.refunded_credits,
                    }
                    if credit
                    else None
                ),
                "events": [
                    {
                        "type": event.event_type,
                        "credits": event.credits,
                        "balance_delta": event.balance_delta,
                        "reason": event.reason,
                        "created_at": event.created_at,
                    }
                    for event in credit_events
                ],
            },
            "allowed_actions": {
                "retry": job.status in {"FAILED", "RETRY_WAIT"} and job.safe_to_retry,
                "cancel": job.status in {"NEW", "RESERVED", "QUEUED", "RETRY_WAIT"},
            },
        }

    @router.post("/jobs/{job_id}/retry")
    def retry_job(
        job_id: str,
        body: ReasonCommand,
        request: Request,
        principal: AuthPrincipal = Depends(admin_principal),
    ):
        before = container.gateway.get(job_id)
        if before is None:
            raise HTTPException(404, "job not found")
        before_view = _job_summary(before)
        try:
            job = container.gateway.retry(job_id)
        except (LookupError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        service.record_external_audit(
            actor=principal,
            action="JOB_RETRIED",
            entity_type="GENERATION_JOB",
            entity_id=job.id,
            before=before_view,
            after=_job_summary(job),
            reason=body.reason,
            request_id=_request_id(request),
        )
        return _job_summary(job)

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(
        job_id: str,
        body: ReasonCommand,
        request: Request,
        principal: AuthPrincipal = Depends(admin_principal),
    ):
        before = container.gateway.get(job_id)
        if before is None:
            raise HTTPException(404, "job not found")
        before_view = _job_summary(before)
        try:
            job = await container.gateway.cancel(job_id)
        except (LookupError, ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
        service.record_external_audit(
            actor=principal,
            action="JOB_CANCELLED",
            entity_type="GENERATION_JOB",
            entity_id=job.id,
            before=before_view,
            after=_job_summary(job),
            reason=body.reason,
            request_id=_request_id(request),
        )
        return _job_summary(job)

    @router.get("/projects")
    def projects(
        q: str | None = None,
        user_id: str | None = None,
        status: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        _principal: AuthPrincipal = Depends(admin_principal),
    ):
        statement = (
            select(Project, Workspace, User)
            .outerjoin(Workspace, Workspace.id == Project.workspace_id)
            .outerjoin(User, User.id == Workspace.owner_user_id)
        )
        if q:
            statement = statement.where(or_(Project.title.ilike(f"%{q}%"), Project.id.ilike(f"%{q}%")))
        if user_id:
            statement = statement.where(User.id == user_id)
        if status:
            statement = statement.where(Project.status == status.upper())
        with container.database.session() as session:
            total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            rows = session.execute(
                statement.order_by(desc(Project.created_at)).offset(offset).limit(limit)
            ).all()
        return _page(
            [
                {
                    "id": project.id,
                    "title": project.title,
                    "status": project.status,
                    "owner": {"id": user.id, "email": user.email} if user else None,
                    "workspace_id": workspace.id if workspace else None,
                    "created_at": project.created_at,
                }
                for project, workspace, user in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )

    @router.get("/projects/{project_id}")
    def project_detail(project_id: str, _principal: AuthPrincipal = Depends(admin_principal)):
        with container.database.session() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise HTTPException(404, "project not found")
            workspace = session.get(Workspace, project.workspace_id) if project.workspace_id else None
            owner = session.get(User, workspace.owner_user_id) if workspace else None
            episodes = list(session.scalars(select(Episode).where(Episode.project_id == project.id)))
            scenes = list(
                session.scalars(
                    select(Scene).where(Scene.episode_id.in_([episode.id for episode in episodes]))
                )
            )
            shots = list(
                session.scalars(select(Shot).where(Shot.scene_id.in_([scene.id for scene in scenes])))
            )
            jobs = list(
                session.scalars(
                    select(GenerationJob)
                    .where(GenerationJob.project_id == project.id)
                    .order_by(desc(GenerationJob.created_at))
                    .limit(200)
                )
            )
            assets = int(
                session.scalar(select(func.count(MediaAsset.id)).where(MediaAsset.project_id == project.id))
                or 0
            )
        return {
            "id": project.id,
            "title": project.title,
            "status": project.status,
            "owner": {"id": owner.id, "email": owner.email} if owner else None,
            "counts": {
                "episodes": len(episodes),
                "scenes": len(scenes),
                "shots": len(shots),
                "assets": assets,
            },
            "shots": [
                {"id": shot.id, "status": shot.status, "sequence": shot.sequence} for shot in shots[:200]
            ],
            "generation_history": [_job_summary(job) for job in jobs],
            "failed_jobs": [_job_summary(job) for job in jobs if job.status == "FAILED"],
            "read_only": True,
        }

    @router.get("/system")
    async def system_health(_principal: AuthPrincipal = Depends(admin_principal)):
        checked_at = datetime.now(UTC)
        components: list[dict[str, Any]] = []
        try:
            with container.database.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            components.append(_health_component("database", "HEALTHY", checked_at))
        except Exception as exc:
            components.append(_health_component("database", "DOWN", checked_at, str(exc)))
        with container.database.session() as session:
            worker_counts: dict[str, int] = {
                str(key): int(value)
                for key, value in session.execute(
                    select(BrowserWorker.status, func.count(BrowserWorker.id)).group_by(BrowserWorker.status)
                ).all()
            }
            disabled_providers = set(
                session.scalars(select(ProviderControl.provider).where(ProviderControl.enabled.is_(False)))
            )
        if not worker_counts:
            components.append(_health_component("workers", "UNKNOWN", checked_at, "No worker heartbeat data"))
        else:
            status = "HEALTHY" if worker_counts.get("READY", 0) else "DEGRADED"
            components.append(_health_component("workers", status, checked_at, str(worker_counts)))
        components.append(_health_component("api", "HEALTHY", checked_at))
        components.append(
            _health_component(
                "storage",
                "HEALTHY" if container.settings.storage_backend.lower() == "s3" else "DEGRADED",
                checked_at,
                "S3-compatible object storage"
                if container.settings.storage_backend.lower() == "s3"
                else "Local storage has no independent availability probe",
            )
        )
        components.append(
            _health_component("queue", "UNKNOWN", checked_at, "No separate queue service is configured")
        )
        components.append(
            _health_component(
                "billing_webhook",
                "HEALTHY" if container.settings.depay_callback_public_key else "NOT_CONFIGURED",
                checked_at,
                "Configuration presence only; delivery freshness is reported from billing records",
            )
        )
        for provider in container.providers.list():
            if not container.providers.is_configured(provider):
                components.append(_health_component(f"provider:{provider}", "NOT_CONFIGURED", checked_at))
                continue
            if provider in disabled_providers:
                components.append(
                    _health_component(
                        f"provider:{provider}",
                        "DEGRADED",
                        checked_at,
                        "Disabled by the platform provider control; no new routing traffic",
                    )
                )
                continue
            try:
                probe = await container.providers.get(provider).health()
                components.append(
                    _health_component(
                        f"provider:{provider}", "HEALTHY" if probe.ok else "DOWN", checked_at, probe.detail
                    )
                )
            except Exception as exc:
                components.append(_health_component(f"provider:{provider}", "DOWN", checked_at, str(exc)))
        with container.database.session() as session:
            routable = int(
                session.scalar(
                    select(func.count(ModelDefinition.id)).where(
                        ModelDefinition.enabled.is_(True),
                        ModelDefinition.router_enabled.is_(True),
                        ModelDefinition.lifecycle_status.in_(("LIVE", "DEGRADED")),
                    )
                )
                or 0
            )
        components.append(
            _health_component(
                "router", "HEALTHY" if routable else "DEGRADED", checked_at, f"{routable} routable models"
            )
        )
        return {"checked_at": checked_at, "components": components}

    @router.get("/audit")
    def audit_log(
        q: Annotated[str | None, Query(max_length=320)] = None,
        actor_user_id: str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        _principal: AuthPrincipal = Depends(admin_principal),
    ):
        statement = select(AdminAuditLog)
        if actor_user_id:
            statement = statement.where(AdminAuditLog.actor_user_id == actor_user_id)
        if action:
            statement = statement.where(AdminAuditLog.action == action)
        if entity_type:
            statement = statement.where(AdminAuditLog.entity_type == entity_type)
        if entity_id:
            statement = statement.where(AdminAuditLog.entity_id == entity_id)
        if q:
            pattern = f"%{q.strip()}%"
            statement = statement.where(
                or_(
                    AdminAuditLog.action.ilike(pattern),
                    AdminAuditLog.actor_user_id.ilike(pattern),
                    AdminAuditLog.entity_type.ilike(pattern),
                    AdminAuditLog.entity_id.ilike(pattern),
                    AdminAuditLog.request_id.ilike(pattern),
                )
            )
        if created_from:
            statement = statement.where(AdminAuditLog.created_at >= created_from)
        if created_to:
            statement = statement.where(AdminAuditLog.created_at <= created_to)
        with container.database.session() as session:
            total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            items = list(
                session.scalars(
                    statement.order_by(desc(AdminAuditLog.created_at)).offset(offset).limit(limit)
                )
            )
        return _page([_audit_view(item) for item in items], total=total, limit=limit, offset=offset)

    app.include_router(router)

    @app.post(
        "/internal/admin/bootstrap-super-admin/{user_id}",
        dependencies=[Depends(verify_api_key)],
        tags=["internal-admin"],
    )
    def bootstrap_super_admin(user_id: str):
        """One-time platform-key bootstrap; normal role changes use SUPER_ADMIN RBAC."""

        with container.database.session() as session:
            existing = session.scalar(
                select(User.id).where(User.platform_role == PlatformRole.SUPER_ADMIN.value)
            )
            if existing is not None:
                raise HTTPException(409, "a SUPER_ADMIN already exists; use the Admin Console")
            user = session.get(User, user_id, with_for_update=True)
            if user is None or user.status != "ACTIVE":
                raise HTTPException(404, "active user not found")
            user.platform_role = PlatformRole.SUPER_ADMIN.value
            session.add(
                AdminAuditLog(
                    actor_user_id=user.id,
                    actor_role=PlatformRole.SUPER_ADMIN.value,
                    action="SUPER_ADMIN_BOOTSTRAPPED",
                    entity_type="USER",
                    entity_id=user.id,
                    before_json={"platform_role": PlatformRole.USER.value},
                    after_json={"platform_role": PlatformRole.SUPER_ADMIN.value},
                    reason="One-time platform API key bootstrap",
                    request_id=f"bootstrap:{uuid.uuid4()}",
                )
            )
        return {"user_id": user_id, "platform_role": PlatformRole.SUPER_ADMIN.value}


def _job_summary(job: GenerationJob) -> dict[str, Any]:
    duration = None
    if job.started_at and job.completed_at:
        duration = max(0.0, (job.completed_at - job.started_at).total_seconds())
    return {
        "id": job.id,
        "project_id": job.project_id,
        "shot_id": job.shot_id,
        "provider": job.provider,
        "model": job.model,
        "capability": job.generation_type,
        "status": job.status,
        "created_at": job.created_at,
        "duration_seconds": duration,
        "credits": job.quoted_credits,
        "provider_task_id": job.provider_job_id,
        "retry_count": job.attempt_count,
        "error_class": job.error_code,
        "error_message": _safe_text(job.error_message),
    }


def _profile_view(profile: ModelCapabilityProfile | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {
        "profile_version": profile.profile_version,
        "confidence_level": profile.confidence_level,
        "supported_operations": profile.supported_operations,
        "supports_image_generation": profile.supports_image_generation,
        "supports_video_generation": profile.supports_video_generation,
        "supports_t2v": profile.supports_t2v,
        "supports_i2v": profile.supports_i2v,
        "supports_v2v": profile.supports_v2v,
        "supports_reference_image": profile.supports_reference_image,
        "supports_multi_reference": profile.supports_multi_reference,
        "supports_start_frame": profile.supports_start_frame,
        "supports_end_frame": profile.supports_end_frame,
        "supports_audio": profile.supports_audio,
        "max_reference_images": profile.max_reference_images,
        "min_duration": profile.min_duration,
        "max_duration": profile.max_duration,
        "supported_aspect_ratios": profile.supported_aspect_ratios,
        "supported_resolutions": profile.supported_resolutions,
        "source": profile.source,
    }


def _verification_view(item: ModelVerification) -> dict[str, Any]:
    return {
        "id": item.id,
        "protocol_version": item.protocol_version,
        "result": item.result,
        "evidence_reference": item.evidence_reference,
        "billable": item.billable,
        "latency_ms": item.latency_ms,
        "detail": item.detail,
        "created_at": item.created_at,
    }


def _audit_view(item: AdminAuditLog) -> dict[str, Any]:
    return {
        "id": item.id,
        "actor_user_id": item.actor_user_id,
        "actor_role": item.actor_role,
        "action": item.action,
        "entity_type": item.entity_type,
        "entity_id": item.entity_id,
        "before": redact(item.before_json),
        "after": redact(item.after_json),
        "reason": item.reason,
        "request_id": item.request_id,
        "created_at": item.created_at,
    }


def _health_component(
    name: str,
    status: str,
    checked_at: datetime,
    detail: str | None = None,
) -> dict[str, Any]:
    return {"name": name, "status": status, "last_checked_at": checked_at, "detail": _safe_text(detail)}
