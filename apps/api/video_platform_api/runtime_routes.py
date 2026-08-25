from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from asset_registry_core import (
    AssetVersionNotPromotable,
    CanonicalVersionNotSet,
    VersionMediaInput,
)
from entitlement_core import (
    InsufficientWorkspaceCredits,
    LiveCanaryConflict,
    PlanEntitlementDenied,
    WorkspaceCreditConflict,
)
from evaluation_core import (
    EvaluationEvidence,
    EvaluationExpectation,
    EvaluationResult,
)
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from generation_gateway import IdempotencyConflict
from memory_core import MemoryLayer, MemoryQuery, MultimodalContent, ShotMemoryInput
from model_registry_core import ModelRole
from platform_contracts import GenerationRequest, PassengerGenerationCommand
from production_domain.models import (
    Asset,
    AssetVersion,
    CostRecord,
    DecisionOutcomeRecord,
    Episode,
    GenerationCandidate,
    GenerationJob,
    LiveCanaryPermit,
    LiveCanaryUsage,
    MediaAsset,
    ModelExecutionRecord,
    ProductionTrace,
    Project,
    ProjectStyleLock,
    ProviderBillingEvidence,
    ProviderProjectBinding,
    QAResult,
    Scene,
    Shot,
    StyleEmbedding,
    TimelineTransition,
    Workspace,
    WorkspaceCreditLedgerEntry,
)
from provider_budget_core import (
    DatabaseProviderBudgetRepository,
    reservation_dict,
    snapshot_dict,
)
from provider_sdk import (
    AssetCriticality,
    ProviderBudgetConflict,
    ProviderTrustLevel,
    ProviderTrustViolation,
    assert_provider_can_handle,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import or_, select
from style_core import SemanticStyleLayerRequired, StyleLockConflict

from .auth import AuthPrincipal, AuthService
from .container import Container


class LogicalAssetCreate(BaseModel):
    project_id: str
    asset_type: str
    name: str = Field(min_length=1, max_length=240)
    description: str = ""
    canonical_metadata: dict[str, Any] = Field(default_factory=dict)


class AssetVersionMediaBody(BaseModel):
    media_asset_id: str
    role: str = "REFERENCE"
    sort_order: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class LogicalAssetVersionCreate(BaseModel):
    primary_media_asset_id: str | None = None
    references: list[AssetVersionMediaBody] = Field(default_factory=list)
    label: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    continuity_state: dict[str, Any] = Field(default_factory=dict)
    source: Literal["USER_UPLOAD"] = "USER_UPLOAD"
    status: str = "READY"
    parent_version_id: str | None = None


class AssetPromoteBody(BaseModel):
    reason: str = "user approved"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectStyleLockBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style_version_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(min_length=1, max_length=2000)
    explicit_confirmation: Literal[True]
    similarity_threshold: float = Field(default=0.72, ge=0, le=1)
    minimum_similarity_threshold: float = Field(default=0.55, ge=0, le=1)
    drift_limit: float = Field(default=0.06, ge=0, le=1)
    max_low_score_fraction: float = Field(default=0.5, ge=0, le=1)


class GenerationPromoteBody(BaseModel):
    asset_id: str | None = None
    asset_type: str = "REFERENCE"
    name: str = "生成素材"
    label: str = ""
    promote_to_canonical: bool = False
    reason: str = ""


class EvaluationRequestBody(BaseModel):
    expectation: EvaluationExpectation
    evidence: EvaluationEvidence | None = None
    attempt_number: int = Field(default=0, ge=0, le=20)
    provider: str = ""
    model_id: str = ""


class RetryPlanRequestBody(BaseModel):
    evaluation: EvaluationResult
    attempt_number: int = Field(default=0, ge=0, le=20)
    current_provider: str
    current_model: str
    alternatives: list[tuple[str, str]] = Field(default_factory=list)
    references_already_strengthened: bool = False


class MetricCreate(BaseModel):
    provider: str
    model_id: str
    metric: str
    value: float = 1.0
    project_id: str | None = None
    shot_id: str | None = None
    generation_job_id: str | None = None
    model_version: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class BenchmarkResultCreate(BaseModel):
    provider: str
    model_id: str
    model_version: str
    case_key: str
    scores: dict[str, float]
    passed: bool
    evidence_asset_ids: list[str] = Field(default_factory=list)


class FeatureFlagUpdate(BaseModel):
    enabled: bool
    project_id: str | None = None


class PricingEstimateRequest(BaseModel):
    provider: str
    model: str
    media_type: Literal["image", "video"]
    duration: float = Field(default=1, ge=1, le=60)
    resolution: str = "720p"
    reference_count: int = Field(default=0, ge=0, le=20)


class CreditReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["CONFIRM_PROVIDER_ACCEPTED", "CONFIRM_PROVIDER_NOT_CREATED"]
    reason: str = Field(min_length=3, max_length=240)
    explicit_confirmation: Literal[True]
    evidence_reference: str | None = Field(default=None, max_length=500)


class ProviderBudgetReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["SETTLE_ACTUAL_COST", "RELEASE_NO_REMOTE_CHARGE"]
    actual_cost_usd: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=14,
        decimal_places=6,
    )
    reason: str = Field(min_length=3, max_length=240)
    evidence_reference: str = Field(min_length=3, max_length=500)
    explicit_confirmation: Literal[True]

    @model_validator(mode="after")
    def validate_action_cost(self) -> ProviderBudgetReconcileRequest:
        if self.action == "SETTLE_ACTUAL_COST" and self.actual_cost_usd is None:
            raise ValueError("SETTLE_ACTUAL_COST requires actual_cost_usd")
        if self.action == "RELEASE_NO_REMOTE_CHARGE" and self.actual_cost_usd is not None:
            raise ValueError("RELEASE_NO_REMOTE_CHARGE forbids actual_cost_usd")
        return self


class LiveCanaryPermitCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=255)
    max_requests: int = Field(strict=True, ge=1, le=10_000)
    max_cost_usd: Decimal = Field(
        gt=0,
        le=Decimal("99999999.999999"),
        max_digits=14,
        decimal_places=6,
    )
    expires_at: datetime
    purpose: str = Field(min_length=3, max_length=500)
    explicit_confirmation: Literal[True]

    @field_validator("provider", "model", "purpose", mode="before")
    @classmethod
    def strip_required_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("expires_at")
    @classmethod
    def require_future_aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        normalized = value.astimezone(UTC)
        if normalized <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return normalized

    @field_validator("explicit_confirmation", mode="before")
    @classmethod
    def require_literal_boolean_confirmation(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("explicit_confirmation must be the boolean true")
        return value


def _asset_view(asset: Asset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "project_id": asset.project_id,
        "asset_type": asset.asset_type,
        "name": asset.name,
        "description": asset.description,
        "canonical_metadata": asset.canonical_metadata,
        "canonical_version_id": asset.canonical_version_id,
        "status": asset.status,
    }


def _version_view(version: AssetVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "asset_id": version.asset_id,
        "version": version.version,
        "label": version.label,
        "primary_media_asset_id": version.primary_media_asset_id,
        "parent_version_id": version.parent_version_id,
        "metadata": version.metadata_json,
        "continuity_state": version.continuity_state,
        "source": version.source,
        "status": version.status,
    }


def _style_lock_view(style_lock: ProjectStyleLock, embedding: StyleEmbedding) -> dict[str, Any]:
    return {
        "id": style_lock.id,
        "project_id": style_lock.project_id,
        "style_asset_id": style_lock.style_asset_id,
        "style_version_id": style_lock.style_version_id,
        "style_embedding": {
            "id": embedding.id,
            "provider": embedding.provider,
            "model": embedding.model,
            "dimension": embedding.dimension,
            "embedding_hash": embedding.embedding_hash,
            "evidence_kind": embedding.evidence_kind,
            "source_media_ids": embedding.source_media_ids,
        },
        "thresholds": {
            "average": style_lock.similarity_threshold,
            "minimum": style_lock.minimum_similarity_threshold,
            "drift_limit": style_lock.drift_limit,
            "max_low_score_fraction": style_lock.max_low_score_fraction,
        },
        "reason": style_lock.reason,
        "locked_at": style_lock.created_at,
    }


def _money_view(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _live_canary_permit_view(
    permit: LiveCanaryPermit,
    usages: list[LiveCanaryUsage],
) -> dict[str, Any]:
    def money(value: Decimal) -> str:
        return format(value.quantize(Decimal("0.000001")), "f")

    usage_statuses = {
        status: sum(usage.status == status for usage in usages)
        for status in ("RESERVED", "UNCERTAIN", "SETTLED", "RELEASED")
    }
    if usage_statuses["UNCERTAIN"]:
        usage_status = "RECONCILIATION_REQUIRED"
    elif usage_statuses["RESERVED"]:
        usage_status = "RESERVED"
    elif usage_statuses["SETTLED"] and usage_statuses["RELEASED"]:
        usage_status = "MIXED_FINAL"
    elif usage_statuses["SETTLED"]:
        usage_status = "SETTLED"
    elif usage_statuses["RELEASED"]:
        usage_status = "RELEASED"
    else:
        usage_status = "UNUSED"
    expires_at = permit.expires_at
    aware_expiry = expires_at.replace(tzinfo=UTC) if expires_at.tzinfo is None else expires_at.astimezone(UTC)
    effective_status = (
        "EXPIRED" if permit.status == "ACTIVE" and aware_expiry <= datetime.now(UTC) else permit.status
    )
    return {
        "id": permit.id,
        "provider": permit.provider,
        "model": permit.model,
        "max_requests": permit.max_requests,
        "used_requests": permit.used_requests,
        "remaining_requests": max(0, permit.max_requests - permit.used_requests),
        "max_cost_usd": money(permit.max_cost_usd),
        "reserved_cost_usd": money(permit.reserved_cost_usd),
        "actual_cost_usd": money(permit.actual_cost_usd),
        "remaining_cost_usd": money(
            max(
                Decimal("0"),
                permit.max_cost_usd - permit.reserved_cost_usd - permit.actual_cost_usd,
            )
        ),
        "expires_at": permit.expires_at,
        "purpose": permit.purpose,
        "status": effective_status,
        "usage_status": usage_status,
        "usage_statuses": usage_statuses,
        "requires_reconciliation": usage_statuses["UNCERTAIN"] > 0,
        "created_at": permit.created_at,
        "updated_at": permit.updated_at,
    }


def _reference_fingerprint(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest() if value else None


def _safe_code_list(value: Any, *, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:160] for item in value if isinstance(item, str)][:limit]


def _timeline_metadata_view(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed_values = {
        "propagation_semantics": frozenset({"FULL", "RESET_BOUNDARY"}),
        "spatial_state": frozenset({"RESET"}),
        "character_state": frozenset({"MAY_PROPAGATE_WITH_EXPLICIT_OPT_IN"}),
        "timeline_branch": frozenset({"NEW_BRANCH"}),
        "inferred_from": frozenset({"legacy_state_hint", "scene_boundary", "linked_shot_default"}),
    }
    result = {key: item for key, permitted in allowed_values.items() if (item := value.get(key)) in permitted}
    for key in ("propagate_character_state", "reconciled"):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    return result


def _safe_scalar_mapping(
    value: Any,
    *,
    allowed_keys: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in allowed_keys:
        if key not in value:
            continue
        item = value.get(key)
        if item is None or isinstance(item, str | int | float | bool):
            result[key] = item
        elif (
            isinstance(item, list)
            and len(item) <= 20
            and all(entry is None or isinstance(entry, str | int | float | bool) for entry in item)
        ):
            result[key] = item
    return result


def _model_execution_evidence_view(item: ModelExecutionRecord) -> dict[str, Any]:
    return {
        "id": item.id,
        "project_id": item.project_id,
        "role": item.role,
        "model_definition_id": item.model_definition_id,
        "provider": item.provider,
        "provider_model_id": item.provider_model_id,
        "request_hash": item.request_hash,
        "latency_ms": item.latency_ms,
        "token_usage": _safe_scalar_mapping(
            item.token_usage_json,
            allowed_keys=frozenset(
                {
                    "input_tokens",
                    "output_tokens",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                }
            ),
        ),
        "estimated_cost_usd": _money_view(item.estimated_cost_usd),
        "actual_cost_usd": _money_view(item.actual_cost_usd),
        "cost_source": item.cost_source,
        "status": item.status,
        "error_code": item.error_code,
        "execution_context": _safe_scalar_mapping(
            item.metadata_json,
            allowed_keys=frozenset({"capability", "asset_criticality", "input_count"}),
        ),
        "created_at": item.created_at,
    }


def _qa_evidence_view(item: QAResult) -> dict[str, Any]:
    metrics = item.metrics_json if isinstance(item.metrics_json, dict) else {}
    character = metrics.get("character_evidence")
    character_summary: dict[str, Any] | None = None
    if isinstance(character, dict):
        samples = character.get("samples")
        character_summary = {
            "producer_run_id": character.get("producer_run_id"),
            "producer_version": character.get("producer_version"),
            "character_id": character.get("character_id"),
            "tracking_status": character.get("tracking_status"),
            "tracking_reason_codes": _safe_code_list(character.get("tracking_reason_codes")),
            "review_requirements": _safe_code_list(character.get("review_requirements")),
            "sample_count": len(samples) if isinstance(samples, list) else 0,
            "aggregate": _safe_scalar_mapping(
                character.get("aggregate"),
                allowed_keys=frozenset(
                    {
                        "average_identity",
                        "minimum_identity",
                        "identity_p10",
                        "drift_slope",
                        "low_score_duration",
                        "appearance_similarity",
                        "hair_similarity",
                        "costume_similarity",
                        "reacquisition_score",
                        "usable_samples",
                        "total_samples",
                        "dominant_face_view",
                        "average_face_visibility",
                    }
                ),
            ),
            "threshold_profile": _safe_scalar_mapping(
                character.get("threshold_profile"),
                allowed_keys=frozenset(
                    {
                        "profile_id",
                        "version",
                        "shot_type",
                        "face_view",
                        "visibility_range",
                        "identity_pass",
                        "identity_hard_fail",
                        "drift_limit",
                        "minimum_required_samples",
                    }
                ),
            ),
        }
    return {
        "id": item.id,
        "candidate_id": item.candidate_id,
        "profile": item.profile,
        "level_reached": item.level_reached,
        "decision": item.decision,
        "overall_score": item.overall_score,
        "scores": {
            "character": item.character_score,
            "scene": item.scene_score,
            "composition": item.composition_score,
            "action": item.action_score,
            "camera": item.camera_score,
            "lighting": item.lighting_score,
            "narrative": item.narrative_score,
        },
        "hard_failures": _safe_code_list(item.hard_failures),
        "evidence": {
            "source": metrics.get("evidence_source"),
            "complete": metrics.get("evidence_complete"),
            "missing_dimensions": _safe_code_list(metrics.get("missing_dimensions")),
            "semantic_review_required": metrics.get("semantic_review_required"),
            "semantic_review_reason": metrics.get("semantic_review_reason"),
            "identity": _safe_scalar_mapping(
                metrics.get("identity"),
                allowed_keys=frozenset(
                    {
                        "average_identity",
                        "minimum_identity",
                        "identity_p10",
                        "drift_slope",
                        "low_score_duration",
                        "appearance_similarity",
                        "reacquisition_score",
                        "usable_samples",
                    }
                ),
            ),
            "character": character_summary,
        },
        "created_at": item.created_at,
    }


def register_runtime_routes(
    app: FastAPI,
    container: Container,
    verify_api_key,
    auth: AuthService,
) -> None:  # type: ignore[no-untyped-def]
    user_router = APIRouter(dependencies=[Depends(auth.current_user)])
    internal_router = APIRouter(dependencies=[Depends(verify_api_key)])

    @user_router.post("/api/passenger/generate", status_code=202)
    def passenger_generate(
        body: PassengerGenerationCommand,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, body.project_id, write=True)
        try:
            admitted = container.generation_admission.admit_passenger(
                GenerationRequest(
                    project_id=body.project_id,
                    type=body.media_type,
                    provider=body.provider or "google_flow",
                    model=body.model or ("veo" if body.media_type == "video" else "NARWHAL"),
                    prompt=body.prompt,
                    negative_prompt=body.negative_prompt,
                    duration=body.duration,
                    aspect_ratio=body.aspect_ratio,
                    start_frame_asset_id=body.start_frame_asset_id,
                    end_frame_asset_id=body.end_frame_asset_id,
                    reference_asset_ids=body.reference_asset_ids,
                    idempotency_key=body.idempotency_key,
                    asset_criticality=body.asset_criticality,
                ),
                requested_role=body.model_role,
                resolution=body.resolution,
                enforce_plan=not principal.development_bypass,
            )
            estimate = admitted.estimate
            body = body.model_copy(
                update={
                    "provider": admitted.request.provider,
                    "model": admitted.request.model,
                    "model_role": admitted.model_role,
                    "asset_criticality": admitted.request.asset_criticality,
                    "duration": admitted.request.duration,
                    "estimated_cost": estimate.estimated_total_usd,
                    "estimated_credits": estimate.credits,
                    "pricing_version": container.credit_pricing.version,
                }
            )
            job, replayed = container.visual_runtime.submit_passenger(
                body,
                estimated_credits=estimate.credits,
                pricing_version=container.credit_pricing.version,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except InsufficientWorkspaceCredits as exc:
            # 402, not 403. Now that every plan is charged, "your plan does not
            # allow this" and "you are allowed and out of credits" are different
            # answers with different fixes — upgrade versus top up — and only
            # the caller can act on the difference.
            raise HTTPException(402, str(exc)) from exc
        except PlanEntitlementDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except WorkspaceCreditConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LookupError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "id": job.id,
            "status": job.status,
            "provider": job.provider,
            "model": job.model,
            "output_asset_id": job.output_asset_id,
            "submission_state": getattr(job, "submission_state", None),
            "credit_status": container.gateway.credit_status(job.id),
            "estimated_cost": job.cost_estimate,
            "estimated_credits": estimate.credits,
            "credit_pricing_version": container.credit_pricing.version,
            "replayed": replayed,
        }

    @user_router.get("/api/projects/{project_id}/model-roles")
    def available_model_roles(
        project_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, project_id)
        resolver = container.workspace_models
        context = resolver.context_for_project(project_id)
        roles: list[dict[str, Any]] = []
        for role in ModelRole:
            try:
                selected, _capability, _implementation = container.model_roles.resolve(
                    project_id,
                    role,
                    require_live=container.settings.provider_mode == "live",
                )
            except (LookupError, PlanEntitlementDenied):
                continue
            roles.append(
                {
                    "role": role.value,
                    "label": role.value.replace("_", " ").title(),
                    "modality": selected.modality,
                }
            )
        return {"plan_tier": context.plan_tier.value, "roles": roles}

    @user_router.get("/api/workspaces/{workspace_id}/credits")
    def workspace_credits(
        workspace_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_workspace(principal, workspace_id)
        with container.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None:
                raise HTTPException(404, "workspace not found")
            entries = container.workspace_credits.entries_in_session(session, workspace_id)
            events = container.workspace_credits.events_in_session(session, workspace_id)
            purchase_ledger = list(
                session.scalars(
                    select(WorkspaceCreditLedgerEntry)
                    .where(WorkspaceCreditLedgerEntry.workspace_id == workspace_id)
                    .order_by(
                        WorkspaceCreditLedgerEntry.created_at,
                        WorkspaceCreditLedgerEntry.id,
                    )
                )
            )
            return {
                "workspace_id": workspace.id,
                "plan_tier": workspace.plan_tier,
                "balance": workspace.credit_balance,
                "starter_grant": container.workspace_credits.starter_grant,
                "pricing_version": container.credit_pricing.version,
                "reserved_credits": sum(
                    item.credits for item in entries if item.status in {"RESERVED", "RECONCILIATION_REQUIRED"}
                ),
                "purchased_credits": sum(
                    item.credits if item.direction == "CREDIT" else -item.credits for item in purchase_ledger
                ),
                "entries": [
                    {
                        "id": item.id,
                        "project_id": item.project_id,
                        "generation_job_id": item.generation_job_id,
                        "credits": item.credits,
                        "settled_credits": item.settled_credits,
                        "refunded_credits": item.refunded_credits,
                        "balance_after": item.balance_after,
                        "status": item.status,
                        "reason": item.reason,
                        "reserved_at": item.reserved_at,
                        "settled_at": item.settled_at,
                        "refunded_at": item.refunded_at,
                        "reconciliation_required_at": item.reconciliation_required_at,
                        "reconciled_at": item.reconciled_at,
                        "reconciliation_reason": item.reconciliation_reason,
                        "created_at": item.created_at,
                    }
                    for item in entries[-100:]
                ],
                "events": [
                    {
                        "id": event.id,
                        "credit_entry_id": event.credit_entry_id,
                        "generation_job_id": event.generation_job_id,
                        "event_type": event.event_type,
                        "credits": event.credits,
                        "balance_delta": event.balance_delta,
                        "balance_after": event.balance_after,
                        "reason": event.reason,
                        "actor_type": event.actor_type,
                        "created_at": event.created_at,
                    }
                    for event in events[-200:]
                ],
                "purchase_ledger": [
                    {
                        "id": item.id,
                        "payment_id": item.payment_id,
                        "entry_type": item.entry_type,
                        "direction": item.direction,
                        "credits": item.credits,
                        "balance_before": item.balance_before,
                        "balance_after": item.balance_after,
                        "currency": item.currency,
                        "raw_amount_microunits": item.raw_amount_microunits,
                        "chain_id": item.chain_id,
                        "created_at": item.created_at,
                    }
                    for item in purchase_ledger[-200:]
                ],
            }

    @user_router.post("/api/pricing/estimate")
    def estimate_pricing(
        body: PricingEstimateRequest,
        _principal: AuthPrincipal = Depends(auth.current_user),
    ):
        try:
            value = container.credit_pricing.estimate(**body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "provider_cost_usd": value.provider_cost_usd,
            "resolution_multiplier": value.resolution_multiplier,
            "reference_multiplier": value.reference_multiplier,
            "service_multiplier": value.service_multiplier,
            "estimated_total_usd": value.estimated_total_usd,
            "credits": value.credits,
            "usd_per_credit": value.usd_per_credit,
            "version": container.credit_pricing.version,
        }

    @user_router.post("/api/assets")
    def create_logical_asset(
        body: LogicalAssetCreate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, body.project_id, write=True)
        try:
            return _asset_view(
                container.asset_registry.create(
                    body.project_id,
                    body.asset_type,
                    body.name,
                    description=body.description,
                    canonical_metadata=body.canonical_metadata,
                    created_by_user_id=(None if principal.development_bypass else principal.user_id),
                )
            )
        except (LookupError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @user_router.get("/api/projects/{project_id}/assets")
    def list_logical_assets(
        project_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
        asset_type: str | None = None,
    ):
        auth.require_project(principal, project_id)
        try:
            return [
                _asset_view(item) for item in container.asset_registry.list(project_id, asset_type=asset_type)
            ]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @user_router.get("/api/projects/{project_id}/style-lock")
    def get_project_style_lock(
        project_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, project_id)
        with container.database.session() as session:
            style_lock = session.scalar(
                select(ProjectStyleLock).where(ProjectStyleLock.project_id == project_id)
            )
            if not style_lock:
                return {"locked": False, "project_id": project_id}
            embedding = session.get(StyleEmbedding, style_lock.style_embedding_id)
            if not embedding:
                raise HTTPException(409, "project style lock embedding is missing")
            return {"locked": True, **_style_lock_view(style_lock, embedding)}

    @user_router.post("/api/projects/{project_id}/style-lock")
    def lock_project_style(
        project_id: str,
        body: ProjectStyleLockBody,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, project_id, write=True)
        if principal.development_bypass:
            raise HTTPException(403, "锁定整部作品画风需要真实登录用户明确确认")
        try:
            style_lock = container.styles.lock(
                project_id,
                body.style_version_id,
                locked_by_user_id=principal.user_id,
                reason=body.reason,
                explicit_confirmation=body.explicit_confirmation,
                similarity_threshold=body.similarity_threshold,
                minimum_similarity_threshold=body.minimum_similarity_threshold,
                drift_limit=body.drift_limit,
                max_low_score_fraction=body.max_low_score_fraction,
            )
            with container.database.session() as session:
                persisted = session.get(ProjectStyleLock, style_lock.id)
                embedding = session.get(StyleEmbedding, style_lock.style_embedding_id)
                if not persisted or not embedding:
                    raise HTTPException(409, "project style lock provenance is incomplete")
                return {"locked": True, **_style_lock_view(persisted, embedding)}
        except SemanticStyleLayerRequired as exc:
            # 503 only when waiting could actually help. Media that cannot be
            # read will not become readable, and telling the user to retry
            # would be a lie; that is a conflict with the version they chose.
            raise HTTPException(503 if exc.retryable else 409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, StyleLockConflict) as exc:
            raise HTTPException(409, str(exc)) from exc

    @user_router.get("/api/assets/{asset_id}")
    def get_logical_asset(
        asset_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            asset = session.get(Asset, asset_id)
            if not asset:
                raise HTTPException(404, "asset not found")
            auth.require_project(principal, asset.project_id)
            versions = list(
                session.scalars(
                    select(AssetVersion)
                    .where(AssetVersion.asset_id == asset.id)
                    .order_by(AssetVersion.version)
                )
            )
            return {**_asset_view(asset), "versions": [_version_view(item) for item in versions]}

    @user_router.post("/api/assets/{asset_id}/versions")
    def add_logical_asset_version(
        asset_id: str,
        body: LogicalAssetVersionCreate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            asset = session.get(Asset, asset_id)
            if not asset:
                raise HTTPException(404, "asset not found")
            auth.require_project(principal, asset.project_id, write=True)
        try:
            version = container.asset_registry.add_version(
                asset_id,
                primary_media_asset_id=body.primary_media_asset_id,
                references=[
                    VersionMediaInput(
                        media_asset_id=item.media_asset_id,
                        role=item.role,
                        sort_order=item.sort_order,
                        metadata=item.metadata,
                    )
                    for item in body.references
                ],
                label=body.label,
                metadata=body.metadata,
                continuity_state=body.continuity_state,
                source="USER_UPLOAD",
                status=body.status,
                parent_version_id=body.parent_version_id,
                created_by_user_id=(None if principal.development_bypass else principal.user_id),
            )
            return _version_view(version)
        except (LookupError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @user_router.post("/api/assets/{asset_id}/versions/{version_id}/promote")
    def promote_logical_asset(
        asset_id: str,
        version_id: str,
        body: AssetPromoteBody,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            asset = session.get(Asset, asset_id)
            if not asset:
                raise HTTPException(404, "asset not found")
            auth.require_project(principal, asset.project_id, write=True)
        try:
            promoted = container.asset_registry.promote(
                asset_id,
                version_id,
                promoted_by_user_id=(None if principal.development_bypass else principal.user_id),
                reason=body.reason,
                metadata=body.metadata,
            )
            result = _asset_view(promoted)
            if promoted.asset_type == "STYLE":
                embedding = container.styles.ensure_embedding(version_id)
                result["style_embedding"] = {
                    "id": embedding.id,
                    "model": embedding.model,
                    "dimension": embedding.dimension,
                    "embedding_hash": embedding.embedding_hash,
                    "evidence_kind": embedding.evidence_kind,
                }
            return result
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, AssetVersionNotPromotable) as exc:
            raise HTTPException(409, str(exc)) from exc

    @user_router.post("/api/generations/{job_id}/promote")
    def promote_generation_result(
        job_id: str,
        body: GenerationPromoteBody,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job or not job.output_asset_id:
                raise HTTPException(409, "generation has no completed output")
            auth.require_project(principal, job.project_id, write=True)
            media = session.get(MediaAsset, job.output_asset_id)
            if not media:
                raise HTTPException(404, "generated media asset not found")
            project_id = job.project_id
        try:
            provider = container.providers.get(job.provider)
            provider_trust = ProviderTrustLevel(
                getattr(provider, "trust_level", ProviderTrustLevel.PRODUCTION)
            )
            if body.promote_to_canonical:
                assert_provider_can_handle(provider_trust, AssetCriticality.CANONICAL)
        except (LookupError, ValueError, ProviderTrustViolation) as exc:
            raise HTTPException(
                409,
                "this generated result is not eligible to become a canonical asset",
            ) from exc
        try:
            logical = (
                container.asset_registry.create(
                    project_id,
                    body.asset_type,
                    body.name,
                    canonical_metadata={"created_from_generation": job_id},
                    created_by_user_id=(None if principal.development_bypass else principal.user_id),
                )
                if body.asset_id is None
                else None
            )
            asset_id = logical.id if logical else body.asset_id
            version = container.asset_registry.add_version(
                asset_id,
                primary_media_asset_id=media.id,
                label=body.label or f"Generation {job_id[:8]}",
                source="PASSENGER_GENERATION",
                metadata={
                    "generation_job_id": job_id,
                    "provider_trust_level": provider_trust.value,
                    "source_asset_criticality": str(
                        job.request_json.get("asset_criticality") or AssetCriticality.STANDARD.value
                    ),
                },
                created_by_user_id=(None if principal.development_bypass else principal.user_id),
            )
            asset = None
            if body.promote_to_canonical:
                asset = container.asset_registry.promote(
                    asset_id,
                    version.id,
                    promoted_by_user_id=(None if principal.development_bypass else principal.user_id),
                    reason=body.reason or "explicit Passenger Seat approval",
                )
            memory_id = None
            if container.feature_flags.enabled("voyage_memory", project_id=project_id):
                indexed = container.memory.index(
                    ShotMemoryInput(
                        project_id=project_id,
                        layer=(MemoryLayer.CANONICAL if body.promote_to_canonical else MemoryLayer.EPISODIC),
                        memory_type="ASSET_VERSION",
                        content=MultimodalContent(
                            text=f"{body.asset_type} {body.name} generation {job_id}",
                            image_urls=(
                                [media.public_url]
                                if media.public_url
                                and media.public_url.startswith("https://")
                                and media.mime_type.startswith("image/")
                                else []
                            ),
                            video_urls=(
                                [media.public_url]
                                if media.public_url
                                and media.public_url.startswith("https://")
                                and media.mime_type.startswith("video/")
                                else []
                            ),
                        ),
                        entity_ids=[asset_id],
                        asset_version_ids=[version.id],
                        canonical=body.promote_to_canonical,
                        metadata={"generation_job_id": job_id},
                    )
                )
                memory_id = indexed.id
            return {
                "asset": _asset_view(asset or logical) if asset or logical else {"id": asset_id},
                "version": _version_view(version),
                "canonical": body.promote_to_canonical,
                "memory_id": memory_id,
            }
        except (LookupError, ValueError, CanonicalVersionNotSet) as exc:
            raise HTTPException(400, str(exc)) from exc

    @internal_router.post("/internal/memory/index")
    def index_memory(body: ShotMemoryInput):
        memory = container.memory.index(body)
        return {
            "id": memory.id,
            "layer": memory.layer,
            "embedding_provider": memory.embedding_provider,
            "embedding_model": memory.embedding_model,
            "embedding_dimension": memory.embedding_dimension,
        }

    @internal_router.post("/internal/memory/search")
    def search_memory(body: MemoryQuery):
        return [item.model_dump(mode="json") for item in container.memory.search(body)]

    @internal_router.get("/internal/memory/characters/{entity_id}")
    def character_memory(entity_id: str, project_id: str, top_k: int = 8):
        query = MemoryQuery(
            project_id=project_id,
            text="character identity appearance wardrobe and continuity history",
            entity_ids=[entity_id],
            top_k=top_k,
        )
        return [item.model_dump(mode="json") for item in container.memory.search(query)]

    @internal_router.get("/internal/memory/scenes/{scene_id}")
    def scene_memory(scene_id: str, project_id: str, top_k: int = 8):
        query = MemoryQuery(
            project_id=project_id,
            text="scene layout lighting props camera axis and temporal state",
            scene_id=scene_id,
            top_k=top_k,
        )
        return [item.model_dump(mode="json") for item in container.memory.search(query)]

    @internal_router.get("/internal/memory/state")
    def current_memory_state(project_id: str, scene_id: str | None = None):
        value = container.memory.current_state(project_id, scene_id=scene_id)
        return value.model_dump(mode="json") if value else None

    @internal_router.post("/internal/evaluate/video")
    def evaluate_video(body: EvaluationRequestBody):
        try:
            return container.evaluator.evaluate(
                body.expectation,
                body.evidence,
                attempt_number=body.attempt_number,
                provider=body.provider,
                model_id=body.model_id,
            ).model_dump(mode="json")
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @internal_router.post("/internal/generations/{job_id}/evaluate")
    def evaluate_generation_job(job_id: str, evidence: EvaluationEvidence):
        try:
            result, retry_plan, retry_job = container.visual_runtime.evaluate_job(job_id, evidence)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "evaluation": result.model_dump(mode="json"),
            "retry_plan": retry_plan.model_dump(mode="json") if retry_plan else None,
            "retry_job_id": retry_job.id if retry_job else None,
        }

    @internal_router.post("/internal/generations/{job_id}/credit-reconcile")
    def reconcile_generation_credit(
        job_id: str,
        body: CreditReconcileRequest,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
    ):
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key is required")
        ledger_action = "SETTLE_RESERVED" if body.action == "CONFIRM_PROVIDER_ACCEPTED" else "REFUND_RESERVED"
        try:
            transition = container.gateway.reconcile_credits(
                job_id,
                action=ledger_action,
                idempotency_key=idempotency_key,
                reason=body.reason,
                evidence_reference=body.evidence_reference,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except WorkspaceCreditConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        job = container.gateway.get(job_id)
        return {
            "generation_job_id": job_id,
            "job_status": job.status if job else None,
            "credit_entry_id": transition.entry_id,
            "previous_credit_status": transition.previous_status,
            "credit_status": transition.status,
            "reserved_credits": transition.reserved_credits,
            "settled_credits": transition.settled_credits,
            "refunded_credits": transition.refunded_credits,
            "balance_after": transition.balance_after,
            "replayed": transition.replayed,
        }

    @internal_router.post("/internal/retry/plan")
    def plan_retry(body: RetryPlanRequestBody):
        return container.retry_engine.plan(
            body.evaluation,
            attempt_number=body.attempt_number,
            current_provider=body.current_provider,
            current_model=body.current_model,
            alternatives=body.alternatives,
            references_already_strengthened=body.references_already_strengthened,
        ).model_dump(mode="json")

    @internal_router.post("/internal/models/metrics")
    def record_model_metric(body: MetricCreate):
        try:
            metric = container.model_metrics.record(**body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": metric.id, "metric": metric.metric_name, "value": metric.value}

    @internal_router.get("/internal/provider-budgets/{provider}")
    def provider_budget(provider: str):
        repository = DatabaseProviderBudgetRepository(container.database)
        try:
            return snapshot_dict(repository.get(provider))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @internal_router.get("/internal/provider-budgets/{provider}/records")
    def provider_budget_records(provider: str):
        repository = DatabaseProviderBudgetRepository(container.database)
        return [reservation_dict(item) for item in repository.records(provider)]

    @internal_router.post("/internal/provider-budget-reservations/{reservation_id}/reconcile")
    def reconcile_provider_budget(
        reservation_id: str,
        body: ProviderBudgetReconcileRequest,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
    ):
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key is required")
        repository = DatabaseProviderBudgetRepository(container.database)
        try:
            result = repository.reconcile_uncertain(
                reservation_id,
                action=body.action,
                actual_cost_usd=body.actual_cost_usd,
                idempotency_key=idempotency_key,
                reason=body.reason,
                evidence_reference=body.evidence_reference,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ProviderBudgetConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "reservation": reservation_dict(result.reservation),
            "provider_budget": snapshot_dict(result.budget),
            "previous_status": result.previous_status,
            "action": result.action,
            "audit_decision_id": result.audit_decision_id,
            "replayed": result.replayed,
        }

    @internal_router.get("/internal/benchmarks")
    def benchmark_manifest():
        return {"suite": container.benchmarks.version, "cases": container.benchmarks.manifest()}

    @internal_router.post("/internal/benchmarks/results")
    def record_benchmark(body: BenchmarkResultCreate):
        try:
            record = container.benchmarks.record(**body.model_dump())
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"id": record.id, "case_key": record.case_key, "passed": record.passed}

    @internal_router.get("/internal/feature-flags")
    def get_feature_flags(project_id: str | None = None):
        return container.feature_flags.snapshot(project_id=project_id)

    @internal_router.put("/internal/feature-flags/{name}")
    def update_feature_flag(name: str, body: FeatureFlagUpdate):
        try:
            value = container.feature_flags.set(name, body.enabled, project_id=body.project_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"name": value.name, "project_id": value.project_id, "enabled": value.enabled}

    @internal_router.post("/internal/live-canary-permits", status_code=201)
    def create_live_canary_permit(
        body: LiveCanaryPermitCreate,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", max_length=200),
        ] = None,
    ):
        """Persist a bounded authorization; this endpoint never executes a provider call."""

        if not idempotency_key or not idempotency_key.strip():
            raise HTTPException(400, "Idempotency-Key is required")
        try:
            permit, audit_decision_id, replayed = container.live_canary.create_authorized(
                provider=body.provider,
                model=body.model,
                max_requests=body.max_requests,
                max_cost_usd=body.max_cost_usd,
                expires_at=body.expires_at,
                purpose=body.purpose,
                explicit_confirmation=body.explicit_confirmation,
                actor_type="PLATFORM_API_KEY",
                idempotency_key=idempotency_key,
            )
        except LiveCanaryConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        with container.database.session() as session:
            stored_permit = session.get(LiveCanaryPermit, permit.id)
            if stored_permit is None:  # pragma: no cover - transaction invariant.
                raise RuntimeError("live canary permit disappeared after authorization")
            usages = list(
                session.scalars(
                    select(LiveCanaryUsage)
                    .where(LiveCanaryUsage.permit_id == permit.id)
                    .order_by(LiveCanaryUsage.created_at, LiveCanaryUsage.id)
                )
            )
        return {
            **_live_canary_permit_view(stored_permit, usages),
            "audit_decision_id": audit_decision_id,
            "replayed": replayed,
        }

    @internal_router.get("/internal/live-canary-permits")
    def list_live_canary_permits(
        permit_id: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
        provider: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
        model: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
        status: Annotated[
            Literal["ACTIVE", "EXHAUSTED", "EXPIRED"] | None,
            Query(),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        statement = select(LiveCanaryPermit)
        if permit_id:
            statement = statement.where(LiveCanaryPermit.id == permit_id)
        if provider:
            statement = statement.where(LiveCanaryPermit.provider == provider)
        if model:
            statement = statement.where(LiveCanaryPermit.model == model)
        if status == "ACTIVE":
            statement = statement.where(
                LiveCanaryPermit.status == "ACTIVE",
                LiveCanaryPermit.expires_at > datetime.now(UTC),
            )
        elif status == "EXPIRED":
            statement = statement.where(
                or_(
                    LiveCanaryPermit.status == "EXPIRED",
                    LiveCanaryPermit.expires_at <= datetime.now(UTC),
                )
            )
        elif status is not None:
            statement = statement.where(LiveCanaryPermit.status == status)

        with container.database.session() as session:
            permits = list(
                session.scalars(
                    statement.order_by(
                        LiveCanaryPermit.created_at.desc(),
                        LiveCanaryPermit.id.desc(),
                    ).limit(limit)
                )
            )
            permit_ids = [permit.id for permit in permits]
            usage_rows = (
                list(
                    session.scalars(
                        select(LiveCanaryUsage)
                        .where(LiveCanaryUsage.permit_id.in_(permit_ids))
                        .order_by(LiveCanaryUsage.created_at, LiveCanaryUsage.id)
                    )
                )
                if permit_ids
                else []
            )
            usages_by_permit = {
                permit.id: [usage for usage in usage_rows if usage.permit_id == permit.id]
                for permit in permits
            }
            return {
                "limit": limit,
                "permits": [
                    _live_canary_permit_view(permit, usages_by_permit[permit.id]) for permit in permits
                ],
            }

    @internal_router.get("/internal/production-evidence")
    def production_evidence(
        project_id: Annotated[str, Query(min_length=1, max_length=100)],
        job_id: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
        shot_id: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        """Read a redacted production evidence join under an exact project scope."""

        with container.database.session() as session:
            if session.get(Project, project_id) is None:
                raise HTTPException(404, "scoped project not found")

            scoped_job = session.get(GenerationJob, job_id) if job_id else None
            if job_id and (scoped_job is None or scoped_job.project_id != project_id):
                raise HTTPException(404, "scoped generation job not found")

            if shot_id:
                scoped_shot_id = session.scalar(
                    select(Shot.id)
                    .join(Scene, Scene.id == Shot.scene_id)
                    .join(Episode, Episode.id == Scene.episode_id)
                    .where(Shot.id == shot_id, Episode.project_id == project_id)
                )
                if scoped_shot_id is None:
                    raise HTTPException(404, "scoped shot not found")
            if scoped_job is not None and shot_id and scoped_job.shot_id != shot_id:
                raise HTTPException(404, "generation job is not associated with the scoped shot")

            effective_shot_id = shot_id or (scoped_job.shot_id if scoped_job is not None else None)
            effective_shot = None
            if effective_shot_id:
                effective_shot = session.scalar(
                    select(Shot)
                    .join(Scene, Scene.id == Shot.scene_id)
                    .join(Episode, Episode.id == Scene.episode_id)
                    .where(Shot.id == effective_shot_id, Episode.project_id == project_id)
                )
                if effective_shot is None:
                    raise HTTPException(404, "effective scoped shot not found")

            execution_rows = list(
                session.scalars(
                    select(ModelExecutionRecord)
                    .where(ModelExecutionRecord.project_id == project_id)
                    .order_by(ModelExecutionRecord.created_at.desc(), ModelExecutionRecord.id.desc())
                    .limit(limit)
                )
            )

            job_statement = select(GenerationJob).where(GenerationJob.project_id == project_id)
            if job_id:
                job_statement = job_statement.where(GenerationJob.id == job_id)
            if shot_id:
                job_statement = job_statement.where(GenerationJob.shot_id == shot_id)
            job_rows = list(
                session.scalars(
                    job_statement.order_by(GenerationJob.created_at.desc(), GenerationJob.id.desc()).limit(
                        limit
                    )
                )
            )

            billing_statement = (
                select(ProviderBillingEvidence)
                .join(
                    GenerationJob,
                    GenerationJob.id == ProviderBillingEvidence.generation_job_id,
                )
                .where(GenerationJob.project_id == project_id)
            )
            if job_id:
                billing_statement = billing_statement.where(GenerationJob.id == job_id)
            if shot_id:
                billing_statement = billing_statement.where(GenerationJob.shot_id == shot_id)
            billing_rows = list(
                session.scalars(
                    billing_statement.order_by(
                        ProviderBillingEvidence.created_at.desc(),
                        ProviderBillingEvidence.id.desc(),
                    ).limit(limit)
                )
            )

            cost_statement = select(CostRecord).where(CostRecord.project_id == project_id)
            if job_id:
                cost_statement = cost_statement.where(CostRecord.generation_job_id == job_id)
            if shot_id:
                cost_statement = cost_statement.where(CostRecord.shot_id == shot_id)
            cost_rows = list(
                session.scalars(
                    cost_statement.order_by(CostRecord.created_at.desc(), CostRecord.id.desc()).limit(limit)
                )
            )

            binding_rows = list(
                session.scalars(
                    select(ProviderProjectBinding)
                    .where(
                        ProviderProjectBinding.local_project_id == project_id,
                        ProviderProjectBinding.provider == "google_flow",
                    )
                    .order_by(
                        ProviderProjectBinding.created_at.desc(),
                        ProviderProjectBinding.id.desc(),
                    )
                    .limit(limit)
                )
            )

            qa_statement = (
                select(QAResult)
                .join(
                    GenerationCandidate,
                    GenerationCandidate.id == QAResult.candidate_id,
                )
                .join(Shot, Shot.id == GenerationCandidate.shot_id)
                .join(Scene, Scene.id == Shot.scene_id)
                .join(Episode, Episode.id == Scene.episode_id)
                .where(Episode.project_id == project_id)
            )
            if scoped_job is not None:
                job_candidate_conditions = [
                    GenerationCandidate.generation_job_id == scoped_job.id,
                ]
                if scoped_job.candidate_id:
                    job_candidate_conditions.append(GenerationCandidate.id == scoped_job.candidate_id)
                qa_statement = qa_statement.where(or_(*job_candidate_conditions))
            if effective_shot_id:
                qa_statement = qa_statement.where(GenerationCandidate.shot_id == effective_shot_id)
            qa_rows = list(
                session.scalars(
                    qa_statement.order_by(QAResult.created_at.desc(), QAResult.id.desc()).limit(limit)
                )
            )

            outcome_statement = select(DecisionOutcomeRecord).where(
                DecisionOutcomeRecord.project_id == project_id
            )
            if job_id:
                outcome_statement = outcome_statement.where(DecisionOutcomeRecord.generation_job_id == job_id)
            if effective_shot_id:
                outcome_statement = outcome_statement.where(
                    DecisionOutcomeRecord.shot_id == effective_shot_id
                )
            outcome_rows = list(
                session.scalars(
                    outcome_statement.order_by(
                        DecisionOutcomeRecord.created_at.desc(),
                        DecisionOutcomeRecord.id.desc(),
                    ).limit(limit)
                )
            )

            transition_statement = select(TimelineTransition).where(
                TimelineTransition.project_id == project_id
            )
            if effective_shot_id:
                transition_statement = transition_statement.where(
                    or_(
                        TimelineTransition.source_shot_id == effective_shot_id,
                        TimelineTransition.target_shot_id == effective_shot_id,
                    )
                )
            transition_rows = list(
                session.scalars(
                    transition_statement.order_by(
                        TimelineTransition.created_at.desc(),
                        TimelineTransition.id.desc(),
                    ).limit(limit)
                )
            )

            return {
                "scope": {
                    "project_id": project_id,
                    "job_id": job_id,
                    "shot_id": shot_id,
                    "effective_shot_id": effective_shot_id,
                    "limit_per_collection": limit,
                    "model_execution_linkage": "PROJECT_ONLY",
                },
                "shot_state": (
                    {
                        "id": effective_shot.id,
                        "downstream_state_stale": effective_shot.downstream_state_stale,
                        "stale_reason": effective_shot.stale_reason,
                        "stale_from_shot_id": effective_shot.stale_from_shot_id,
                    }
                    if effective_shot is not None
                    else None
                ),
                "model_executions": [_model_execution_evidence_view(item) for item in execution_rows],
                "provider_jobs": [
                    {
                        "id": item.id,
                        "project_id": item.project_id,
                        "shot_id": item.shot_id,
                        "candidate_id": item.candidate_id,
                        "generation_type": item.generation_type,
                        "provider": item.provider,
                        "model": item.model,
                        "status": item.status,
                        "provider_job_id": item.provider_job_id,
                        "provider_project_id": item.provider_project_id,
                        "output_asset_id": item.output_asset_id,
                        "attempt_count": item.attempt_count,
                        "max_attempts": item.max_attempts,
                        "retry_category": item.retry_category,
                        "submission_state": item.submission_state,
                        "safe_to_retry": item.safe_to_retry,
                        "error_code": item.error_code,
                        "cost_estimate": item.cost_estimate,
                        "actual_cost": item.actual_cost,
                        "created_at": item.created_at,
                        "submitted_at": item.submitted_at,
                        "completed_at": item.completed_at,
                    }
                    for item in job_rows
                ],
                "provider_billing_evidence": [
                    {
                        "id": item.id,
                        "generation_job_id": item.generation_job_id,
                        "cost_record_id": item.cost_record_id,
                        "evidence_key": item.evidence_key,
                        "provider": item.provider,
                        "model": item.model,
                        "source": item.source,
                        "provider_reference_fingerprint": _reference_fingerprint(item.provider_reference),
                        "actual_cost_usd": _money_view(item.actual_cost_usd),
                        "estimated_cost_usd": _money_view(item.estimated_cost_usd),
                        "provider_credits": _money_view(item.provider_credits),
                        "verified_at": item.verified_at,
                        "created_at": item.created_at,
                    }
                    for item in billing_rows
                ],
                "cost_records": [
                    {
                        "id": item.id,
                        "project_id": item.project_id,
                        "shot_id": item.shot_id,
                        "candidate_id": item.candidate_id,
                        "generation_job_id": item.generation_job_id,
                        "provider": item.provider,
                        "model": item.model,
                        "duration": item.duration,
                        "resolution": item.resolution,
                        "credits": item.credits,
                        "estimated_cost": item.estimated_cost,
                        "actual_cost": item.actual_cost,
                        "retry_cost": item.retry_cost,
                        "accepted": item.accepted,
                        "wasted": item.wasted,
                        "created_at": item.created_at,
                    }
                    for item in cost_rows
                ],
                "flow_bindings": [
                    {
                        "id": item.id,
                        "local_project_id": item.local_project_id,
                        "provider": item.provider,
                        "provider_account_id": item.provider_account_id,
                        "provider_project_id": item.provider_project_id,
                        "status": item.status,
                        "version": item.version,
                        "ready_at": item.ready_at,
                        "migration_required_at": item.migration_required_at,
                        "created_at": item.created_at,
                    }
                    for item in binding_rows
                ],
                "qa_evidence": [_qa_evidence_view(item) for item in qa_rows],
                "decision_outcomes": [
                    {
                        "id": item.id,
                        "project_id": item.project_id,
                        "shot_id": item.shot_id,
                        "candidate_id": item.candidate_id,
                        "generation_job_id": item.generation_job_id,
                        "qa_result_id": item.qa_result_id,
                        "continuity_decision": item.continuity_decision,
                        "generation_policy": item.generation_policy,
                        "provider": item.provider,
                        "model": item.model,
                        "shot_features": _safe_scalar_mapping(
                            item.shot_features_json,
                            allowed_keys=frozenset(
                                {
                                    "sequence",
                                    "shot_type",
                                    "duration",
                                    "status",
                                    "continuity_policy",
                                    "prompt_hash",
                                }
                            ),
                        ),
                        "user_outcome": item.user_outcome,
                        "accepted": item.accepted,
                        "estimated_cost_usd": _money_view(item.estimated_cost_usd),
                        "actual_cost_usd": _money_view(item.actual_cost_usd),
                        "billing_source": item.billing_source,
                        "created_at": item.created_at,
                    }
                    for item in outcome_rows
                ],
                "timeline_transitions": [
                    {
                        "id": item.id,
                        "project_id": item.project_id,
                        "source_shot_id": item.source_shot_id,
                        "target_shot_id": item.target_shot_id,
                        "transition_type": item.transition_type,
                        "branch_key": item.branch_key,
                        "reconciliation_required": item.reconciliation_required,
                        "metadata": _timeline_metadata_view(item.metadata_json),
                        "created_at": item.created_at,
                        "updated_at": item.updated_at,
                    }
                    for item in transition_rows
                ],
            }

    @internal_router.get("/internal/shots/{shot_id}/traces")
    def shot_traces(shot_id: str):
        with container.database.session() as session:
            return [
                {
                    "trace_id": item.trace_id,
                    "mode": item.mode,
                    "generation_job_id": item.generation_job_id,
                    "provider": item.provider,
                    "model_id": item.model_id,
                    "prompt_version": item.prompt_version,
                    "context_asset_ids": item.context_asset_ids,
                    "retrieved_memory_ids": item.retrieved_memory_ids,
                    "router_scores": item.router_scores_json,
                    "evaluation": item.evaluation_json,
                    "retry": item.retry_json,
                }
                for item in session.scalars(
                    select(ProductionTrace)
                    .where(ProductionTrace.shot_id == shot_id)
                    .order_by(ProductionTrace.created_at)
                )
            ]

    app.include_router(user_router)
    app.include_router(internal_router)
