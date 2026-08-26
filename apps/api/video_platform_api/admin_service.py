from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    AdminAuditLog,
    AdminCreditAdjustment,
    AuthSession,
    GenerationJob,
    ModelCapabilityProfile,
    ModelDefinition,
    ModelLifecycleStatus,
    ModelVerification,
    PlatformRole,
    ProviderControl,
    User,
    Workspace,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import AuthPrincipal

_SENSITIVE_FRAGMENTS = ("secret", "password", "token", "api_key", "private_key", "ciphertext")


class AdminOperationConflict(RuntimeError):
    pass


class LifecycleTransitionDenied(ValueError):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class CreditAdjustmentResult:
    adjustment: AdminCreditAdjustment
    replayed: bool


def redact(value: Any) -> Any:
    """Recursively remove credential-shaped values before audit persistence or JSON output."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            result[str(key)] = (
                "[REDACTED]"
                if any(fragment in normalized for fragment in _SENSITIVE_FRAGMENTS)
                else redact(item)
            )
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


class AdminOperationService:
    _TRANSITIONS: dict[str, set[str]] = {
        "DISABLED": {"CONFIGURED"},
        "CONFIGURED": {"TESTING", "DISABLED", "BLOCKED"},
        # VERIFIED is evidence-derived by record_model_verification; it is not
        # an operator-selectable state.
        "TESTING": {"CONFIGURED", "BLOCKED"},
        "VERIFIED": {"LIVE", "TESTING", "DISABLED", "BLOCKED"},
        "LIVE": {"DEGRADED", "BLOCKED", "DISABLED"},
        "DEGRADED": {"LIVE", "BLOCKED", "DISABLED"},
        "BLOCKED": {"CONFIGURED", "DISABLED"},
    }

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _audit(
        session: Session,
        *,
        actor: AuthPrincipal,
        action: str,
        entity_type: str,
        entity_id: str,
        before: dict[str, Any],
        after: dict[str, Any],
        reason: str | None,
        request_id: str,
    ) -> AdminAuditLog:
        if not request_id.strip():
            raise ValueError("request_id is required for an admin mutation")
        audit = AdminAuditLog(
            actor_user_id=actor.user_id,
            actor_role=actor.platform_role,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_json=redact(before),
            after_json=redact(after),
            reason=reason[:500] if reason else None,
            request_id=request_id[:160],
        )
        session.add(audit)
        return audit

    def record_external_audit(
        self,
        *,
        actor: AuthPrincipal,
        action: str,
        entity_type: str,
        entity_id: str,
        before: dict[str, Any],
        after: dict[str, Any],
        reason: str | None,
        request_id: str,
    ) -> None:
        with self.database.session() as session:
            self._audit(
                session,
                actor=actor,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                before=before,
                after=after,
                reason=reason,
                request_id=request_id,
            )

    def adjust_credits(
        self,
        *,
        user_id: str,
        workspace_id: str,
        delta: int,
        reason: str,
        reference: str | None,
        idempotency_key: str,
        actor: AuthPrincipal,
        request_id: str,
    ) -> CreditAdjustmentResult:
        key = idempotency_key.strip()
        normalized_reason = reason.strip()
        if not key or len(key) > 200:
            raise ValueError("Idempotency-Key must contain 1 to 200 characters")
        if delta == 0 or abs(delta) > 1_000_000:
            raise ValueError("delta must be non-zero and no larger than 1000000 credits")
        if len(normalized_reason) < 8:
            raise ValueError("reason must contain at least 8 characters")
        try:
            with self.database.session() as session:
                existing = session.scalar(
                    select(AdminCreditAdjustment).where(AdminCreditAdjustment.idempotency_key == key)
                )
                if existing:
                    if (
                        existing.user_id != user_id
                        or existing.workspace_id != workspace_id
                        or existing.delta != delta
                        or existing.reason != normalized_reason
                    ):
                        raise AdminOperationConflict(
                            "Idempotency-Key already belongs to different adjustment facts"
                        )
                    return CreditAdjustmentResult(existing, True)
                user = session.get(User, user_id)
                workspace = session.get(Workspace, workspace_id, with_for_update=True)
                if user is None or workspace is None or workspace.owner_user_id != user.id:
                    raise LookupError("user workspace not found")
                before = workspace.credit_balance
                after = before + delta
                if after < 0:
                    raise ValueError(
                        f"credit adjustment would create a negative balance: before={before}, delta={delta}"
                    )
                changed = session.execute(
                    update(Workspace)
                    .where(Workspace.id == workspace.id, Workspace.credit_balance == before)
                    .values(credit_balance=after)
                )
                if affected_rows(changed) != 1:
                    raise AdminOperationConflict(
                        "workspace balance changed concurrently; retry with a new key"
                    )
                adjustment = AdminCreditAdjustment(
                    workspace_id=workspace.id,
                    user_id=user.id,
                    operator_user_id=actor.user_id,
                    idempotency_key=key,
                    delta=delta,
                    before_balance=before,
                    after_balance=after,
                    reason=normalized_reason,
                    reference=reference.strip()[:240] if reference else None,
                )
                session.add(adjustment)
                session.flush([adjustment])
                self._audit(
                    session,
                    actor=actor,
                    action="CREDITS_ADJUSTED",
                    entity_type="WORKSPACE",
                    entity_id=workspace.id,
                    before={"credit_balance": before},
                    after={"credit_balance": after, "delta": delta, "adjustment_id": adjustment.id},
                    reason=normalized_reason,
                    request_id=request_id,
                )
                return CreditAdjustmentResult(adjustment, False)
        except IntegrityError as exc:
            with self.database.session() as session:
                replay = session.scalar(
                    select(AdminCreditAdjustment).where(AdminCreditAdjustment.idempotency_key == key)
                )
                if (
                    replay
                    and replay.user_id == user_id
                    and replay.workspace_id == workspace_id
                    and replay.delta == delta
                ):
                    return CreditAdjustmentResult(replay, True)
            raise AdminOperationConflict("credit adjustment conflicted with another request") from exc

    def set_user_status(
        self,
        *,
        user_id: str,
        target_status: str,
        reason: str,
        actor: AuthPrincipal,
        request_id: str,
    ) -> User:
        target = target_status.strip().upper()
        if target not in {"ACTIVE", "SUSPENDED"}:
            raise ValueError("status must be ACTIVE or SUSPENDED")
        if user_id == actor.user_id and target != "ACTIVE":
            raise ValueError("an administrator cannot suspend their own account")
        with self.database.session() as session:
            user = session.get(User, user_id, with_for_update=True)
            if user is None:
                raise LookupError("user not found")
            if (
                user.platform_role in {PlatformRole.ADMIN.value, PlatformRole.SUPER_ADMIN.value}
                and actor.platform_role != PlatformRole.SUPER_ADMIN.value
            ):
                raise ValueError("SUPER_ADMIN is required to change an administrator account status")
            before = user.status
            user.status = target
            if target == "SUSPENDED":
                now = datetime.now(UTC)
                session.execute(
                    update(AuthSession)
                    .where(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None))
                    .values(revoked_at=now)
                )
            self._audit(
                session,
                actor=actor,
                action="USER_STATUS_CHANGED",
                entity_type="USER",
                entity_id=user.id,
                before={"status": before},
                after={"status": target},
                reason=reason,
                request_id=request_id,
            )
            return user

    def set_user_role(
        self,
        *,
        user_id: str,
        target_role: str,
        reason: str,
        actor: AuthPrincipal,
        request_id: str,
    ) -> User:
        role = PlatformRole(target_role.strip().upper()).value
        if user_id == actor.user_id and role != PlatformRole.SUPER_ADMIN.value:
            raise ValueError("a super administrator cannot demote their own account")
        with self.database.session() as session:
            user = session.get(User, user_id, with_for_update=True)
            if user is None:
                raise LookupError("user not found")
            before = user.platform_role
            user.platform_role = role
            self._audit(
                session,
                actor=actor,
                action="PLATFORM_ROLE_CHANGED",
                entity_type="USER",
                entity_id=user.id,
                before={"platform_role": before},
                after={"platform_role": role},
                reason=reason,
                request_id=request_id,
            )
            return user

    def set_plan(
        self,
        *,
        user_id: str,
        workspace_id: str,
        target_plan: str,
        reason: str,
        actor: AuthPrincipal,
        request_id: str,
    ) -> Workspace:
        plan = target_plan.strip().upper()
        if plan not in {"FREE", "PRO", "ENTERPRISE"}:
            raise ValueError("plan must be FREE, PRO, or ENTERPRISE")
        with self.database.session() as session:
            workspace = session.get(Workspace, workspace_id, with_for_update=True)
            if workspace is None or workspace.owner_user_id != user_id:
                raise LookupError("user workspace not found")
            before = workspace.plan_tier
            workspace.plan_tier = plan
            self._audit(
                session,
                actor=actor,
                action="PLAN_CHANGED",
                entity_type="WORKSPACE",
                entity_id=workspace.id,
                before={"plan_tier": before},
                after={"plan_tier": plan},
                reason=reason,
                request_id=request_id,
            )
            return workspace

    def record_model_verification(
        self,
        *,
        model_id: str,
        protocol_version: str,
        result: str,
        evidence_reference: str,
        billable: bool,
        latency_ms: float | None,
        detail: str | None,
        idempotency_key: str,
        actor: AuthPrincipal,
        request_id: str,
    ) -> tuple[ModelVerification, bool]:
        normalized_result = result.strip().upper()
        if normalized_result not in {"SUCCESS", "FAILED"}:
            raise ValueError("verification result must be SUCCESS or FAILED")
        if not protocol_version.strip() or not evidence_reference.strip() or not idempotency_key.strip():
            raise ValueError("protocol_version, evidence_reference and Idempotency-Key are required")
        with self.database.session() as session:
            model = session.get(ModelDefinition, model_id, with_for_update=True)
            if model is None:
                raise LookupError("model not found")
            existing = session.scalar(
                select(ModelVerification).where(
                    ModelVerification.model_definition_id == model.id,
                    ModelVerification.idempotency_key == idempotency_key.strip(),
                )
            )
            if existing:
                return existing, True
            if normalized_result == "SUCCESS" and billable:
                prefix = "generation-job:"
                if not evidence_reference.strip().startswith(prefix):
                    raise ValueError(
                        "billable SUCCESS evidence must reference a completed generation-job:<id>"
                    )
                job_id = evidence_reference.strip()[len(prefix) :]
                job = session.get(GenerationJob, job_id)
                if (
                    job is None
                    or job.status != "COMPLETED"
                    or job.provider != model.provider
                    or job.model not in {model.provider_model_id, model.logical_name}
                ):
                    raise ValueError(
                        "verification job must be COMPLETED and match the model provider mapping"
                    )
            verification = ModelVerification(
                model_definition_id=model.id,
                operator_user_id=actor.user_id,
                idempotency_key=idempotency_key.strip()[:200],
                protocol_version=protocol_version.strip()[:120],
                result=normalized_result,
                evidence_reference=evidence_reference.strip()[:500],
                billable=billable,
                latency_ms=latency_ms,
                detail=detail.strip()[:500] if detail else None,
            )
            session.add(verification)
            session.flush([verification])
            before = {"lifecycle_status": model.lifecycle_status, "last_verified_at": model.last_verified_at}
            if normalized_result == "SUCCESS":
                model.last_verified_at = verification.created_at
                if billable:
                    model.last_live_test_at = verification.created_at
                if model.lifecycle_status == ModelLifecycleStatus.TESTING.value:
                    model.lifecycle_status = ModelLifecycleStatus.VERIFIED.value
            self._audit(
                session,
                actor=actor,
                action="MODEL_VERIFICATION_RECORDED",
                entity_type="MODEL",
                entity_id=model.id,
                before=before,
                after={
                    "lifecycle_status": model.lifecycle_status,
                    "last_verified_at": model.last_verified_at.isoformat()
                    if model.last_verified_at
                    else None,
                    "verification_id": verification.id,
                    "result": normalized_result,
                    "billable": billable,
                },
                reason=detail,
                request_id=request_id,
            )
            return verification, False

    def transition_model(
        self,
        *,
        model_id: str,
        target_status: str,
        reason: str,
        actor: AuthPrincipal,
        request_id: str,
        provider_configured: bool,
        provider_enabled: bool,
    ) -> ModelDefinition:
        target = ModelLifecycleStatus(target_status.strip().upper()).value
        with self.database.session() as session:
            model = session.get(ModelDefinition, model_id, with_for_update=True)
            if model is None:
                raise LookupError("model not found")
            current = model.lifecycle_status
            reasons: list[str] = []
            if target not in self._TRANSITIONS.get(current, set()):
                reasons.append(f"transition {current} -> {target} is not allowed")
            profile = session.get(ModelCapabilityProfile, model.id)
            if target == ModelLifecycleStatus.LIVE.value:
                if actor.platform_role != PlatformRole.SUPER_ADMIN.value:
                    reasons.append("SUPER_ADMIN is required to move a model to LIVE")
                if not provider_configured:
                    reasons.append("provider is not configured")
                if not provider_enabled:
                    reasons.append("provider is disabled")
                if model.provider_model_id.startswith("CONFIGURE_"):
                    reasons.append("provider model mapping is a placeholder")
                if profile is None or not profile.supported_operations:
                    reasons.append("capability mapping and parameter profile are incomplete")
                elif model.modality == "video" and (
                    not profile.supports_video_generation
                    or not (profile.supports_t2v or profile.supports_i2v or profile.supports_v2v)
                ):
                    reasons.append("video generation modes are incomplete")
                elif model.modality == "image" and not profile.supports_image_generation:
                    reasons.append("image generation capability is incomplete")
                successful = session.scalar(
                    select(ModelVerification.id).where(
                        ModelVerification.model_definition_id == model.id,
                        ModelVerification.result == "SUCCESS",
                        ModelVerification.billable.is_(True),
                        ModelVerification.created_at >= profile.updated_at
                        if profile is not None
                        else ModelVerification.id.is_(None),
                    )
                )
                if successful is None:
                    reasons.append("no successful billable production-protocol verification exists")
            if reasons:
                raise LifecycleTransitionDenied(reasons)
            before = self._model_control_snapshot(model)
            model.lifecycle_status = target
            model.enabled = target not in {"DISABLED", "BLOCKED"}
            model.live_enabled = target in {"LIVE", "DEGRADED"}
            if target in {"DISABLED", "BLOCKED"}:
                model.router_enabled = False
            after = self._model_control_snapshot(model)
            self._audit(
                session,
                actor=actor,
                action="MODEL_LIFECYCLE_CHANGED",
                entity_type="MODEL",
                entity_id=model.id,
                before=before,
                after=after,
                reason=reason,
                request_id=request_id,
            )
            return model

    def set_model_capabilities(
        self,
        *,
        model_id: str,
        capabilities: dict[str, Any],
        reason: str,
        actor: AuthPrincipal,
        request_id: str,
    ) -> ModelCapabilityProfile:
        allowed = {
            "supported_operations",
            "supports_image_generation",
            "supports_video_generation",
            "supports_t2v",
            "supports_i2v",
            "supports_v2v",
            "supports_reference_image",
            "supports_multi_reference",
            "supports_start_frame",
            "supports_end_frame",
            "supports_audio",
            "max_reference_images",
            "min_duration",
            "max_duration",
            "supported_aspect_ratios",
            "supported_resolutions",
        }
        if set(capabilities) != allowed:
            raise ValueError("capability command does not match the supported schema")
        with self.database.session() as session:
            model = session.get(ModelDefinition, model_id, with_for_update=True)
            profile = session.get(ModelCapabilityProfile, model_id, with_for_update=True)
            if model is None or profile is None:
                raise LookupError("model capability profile not found")
            before = self._capability_snapshot(profile)
            for name in allowed:
                setattr(profile, name, capabilities[name])
            profile.source = "ADMIN_REVIEWED"
            profile.profile_version = (
                str(int(profile.profile_version) + 1) if profile.profile_version.isdigit() else "2"
            )
            profile.updated_at = datetime.now(UTC)

            # A changed capability contract invalidates prior production proof.
            model.lifecycle_status = ModelLifecycleStatus.CONFIGURED.value
            model.live_enabled = False
            model.router_enabled = False
            model.last_verified_at = None
            model.last_live_test_at = None
            after = self._capability_snapshot(profile)
            self._audit(
                session,
                actor=actor,
                action="MODEL_CAPABILITIES_CHANGED",
                entity_type="MODEL",
                entity_id=model.id,
                before={"capabilities": before},
                after={
                    "capabilities": after,
                    "lifecycle_status": model.lifecycle_status,
                    "router_enabled": model.router_enabled,
                },
                reason=reason,
                request_id=request_id,
            )
            return profile

    def set_model_router(
        self,
        *,
        model_id: str,
        enabled: bool,
        reason: str,
        actor: AuthPrincipal,
        request_id: str,
    ) -> ModelDefinition:
        with self.database.session() as session:
            model = session.get(ModelDefinition, model_id, with_for_update=True)
            if model is None:
                raise LookupError("model not found")
            if enabled and (not model.enabled or model.lifecycle_status not in {"LIVE", "DEGRADED"}):
                raise ValueError("router can only enable an enabled LIVE or DEGRADED model")
            before = {"router_enabled": model.router_enabled}
            model.router_enabled = enabled
            self._audit(
                session,
                actor=actor,
                action="MODEL_ROUTER_CHANGED",
                entity_type="MODEL",
                entity_id=model.id,
                before=before,
                after={"router_enabled": enabled},
                reason=reason,
                request_id=request_id,
            )
            return model

    def set_model_metadata(
        self,
        *,
        model_id: str,
        display_name: str,
        user_visible: bool,
        pricing_metadata: dict[str, Any],
        reason: str,
        actor: AuthPrincipal,
        request_id: str,
    ) -> ModelDefinition:
        with self.database.session() as session:
            model = session.get(ModelDefinition, model_id, with_for_update=True)
            if model is None:
                raise LookupError("model not found")
            before = {
                "display_name": model.display_name,
                "user_visible": model.user_visible,
                "pricing_metadata": model.pricing_metadata,
            }
            model.display_name = display_name.strip()[:200]
            model.user_visible = user_visible
            model.pricing_metadata = redact(pricing_metadata)
            self._audit(
                session,
                actor=actor,
                action="MODEL_METADATA_CHANGED",
                entity_type="MODEL",
                entity_id=model.id,
                before=before,
                after={
                    "display_name": model.display_name,
                    "user_visible": model.user_visible,
                    "pricing_metadata": model.pricing_metadata,
                },
                reason=reason,
                request_id=request_id,
            )
            return model

    @staticmethod
    def _capability_snapshot(profile: ModelCapabilityProfile) -> dict[str, Any]:
        return {
            "profile_version": profile.profile_version,
            "supported_operations": list(profile.supported_operations),
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
            "supported_aspect_ratios": list(profile.supported_aspect_ratios),
            "supported_resolutions": list(profile.supported_resolutions),
            "source": profile.source,
        }

    def set_provider_enabled(
        self,
        *,
        provider: str,
        enabled: bool,
        reason: str,
        actor: AuthPrincipal,
        request_id: str,
    ) -> ProviderControl:
        with self.database.session() as session:
            control = session.get(ProviderControl, provider, with_for_update=True)
            before = {"enabled": True if control is None else control.enabled}
            if control is None:
                control = ProviderControl(provider=provider, enabled=enabled)
                session.add(control)
            control.enabled = enabled
            control.disabled_reason = None if enabled else reason.strip()[:500]
            control.changed_by_user_id = actor.user_id
            self._audit(
                session,
                actor=actor,
                action="PROVIDER_ENABLEMENT_CHANGED",
                entity_type="PROVIDER",
                entity_id=provider,
                before=before,
                after={"enabled": enabled},
                reason=reason,
                request_id=request_id,
            )
            return control

    @staticmethod
    def _model_control_snapshot(model: ModelDefinition) -> dict[str, Any]:
        return {
            "enabled": model.enabled,
            "live_enabled": model.live_enabled,
            "router_enabled": model.router_enabled,
            "lifecycle_status": model.lifecycle_status,
        }
