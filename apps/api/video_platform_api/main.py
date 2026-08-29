from __future__ import annotations

import asyncio
import hashlib
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from asset_registry_core import VersionMediaInput
from character_core import (
    CharacterStateConflict,
    CharacterStatePolicyViolation,
    TimelineBranchConflict,
    TimelineBranchError,
)
from character_evidence.client import (
    CharacterEvidenceCallbackAuthenticationError,
    CharacterEvidenceCallbackPayloadError,
    report_from_payload,
    verify_callback,
)
from continuity_core import ContinuityRiskVector
from director_production import CandidateNotCommittable
from entitlement_core import (
    InsufficientWorkspaceCredits,
    PlanEntitlementDenied,
    WorkspaceCreditConflict,
)
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from generation_gateway import (
    FlowAffinityConflict,
    GenerationTargetError,
    IdempotencyConflict,
)
from generation_gateway.gateway import UnsafeRetry
from image_prompt_core import ImagePromptCorrectRequest
from media_service import (
    DEFAULT_SWEEP_LIMIT,
    DirectUploadConflict,
    DirectUploadExpired,
    DirectUploadNotFinished,
    DirectUploadUnsupported,
    ProviderMediaReconciliationConflict,
    ProviderMediaValidationFailed,
    StorageReservationConflict,
    ThumbnailUnavailable,
    WorkspaceStorageQuota,
    WorkspaceStorageQuotaExceeded,
    lineage_key,
    sweep_expired_uploads,
    sweep_generation_staging,
)
from memory_core import MemoryLayer, MultimodalContent, ShotMemoryInput
from model_registry_core import ModelRole, ShotRequirements
from payment_core import (
    AlchemyWebhookAuthenticationError,
    AlchemyWebhookConfigurationError,
    AlchemyWebhookConflict,
    AlchemyWebhookPayloadError,
    DePayAuthenticationError,
    DePayConfigurationError,
    DePayConflict,
    DePayPayloadError,
)
from platform_contracts import (
    EpisodeCreate,
    GenerationRequest,
    ProjectCreate,
    ProviderMediaReconcileRequest,
    ProviderMediaReconcileView,
    SceneCreate,
    ShotCreate,
)
from platform_shared import (
    SAFE_INLINE_MEDIA_TYPES,
    StorageLimitExceeded,
    UnsafeMediaUpload,
    validate_user_media_upload,
    verify_local_reference_signature,
)
from production_domain.models import (
    BrowserWorker,
    Character,
    CharacterIdentityVersion,
    CostRecord,
    DecisionRecord,
    DirectUploadStatus,
    Episode,
    GenerationCandidate,
    MediaAsset,
    MediaProviderBinding,
    MediaRendition,
    ModelDefinition,
    ModelPricingProfile,
    Project,
    PromptCompilation,
    PromptRevision,
    ProviderAccount,
    ProviderCredential,
    ProviderProjectBinding,
    QAResult,
    Scene,
    Shot,
    ShotDependency,
    TimelineState,
    User,
    WorkerStatus,
    Workspace,
    utcnow,
)
from provider_sdk import LIVE_PROVIDER_CONFIRMATION, FactLockSet, NotConfiguredProvider
from pydantic import BaseModel, ConfigDict, Field
from qa_core import HumanReviewNotAllowed
from sqlalchemy import select

from .admin_routes import register_admin_routes
from .auth import AuthPrincipal, AuthService, CookieCSRFMiddleware
from .container import Container, build_container
from .creative_routes import register_creative_routes
from .payment_routes import register_payment_routes
from .request_limits import UploadSizeLimitMiddleware
from .runtime_routes import register_runtime_routes
from .worker_auth import WorkerAuthenticationError, WorkerCredentialService, WorkerPrincipal


class DirectUploadAuthorize(BaseModel):
    """What a client declares before it is allowed to transfer anything."""

    project_id: str
    asset_type: str
    filename: str
    mime_type: str
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(gt=0)
    shot_id: str | None = None
    character_id: str | None = None


class AccountCreate(BaseModel):
    provider: str = "google_flow"
    account_identifier: str
    tier: str = "PRO"
    credits: int = Field(default=100, ge=0)
    image_capacity: int = Field(default=1, ge=0)
    video_capacity: int = Field(default=1, ge=0)
    supported_models: list[str] = Field(default_factory=lambda: ["flow-veo-3.1", "veo", "NARWHAL"])
    provider_project_id: str = ""
    credential: str = ""


class WorkerRegister(BaseModel):
    worker_id: str
    provider: str = "google_flow"
    account_id: str | None = None
    connection_id: str | None = None
    capabilities: list[str] = Field(default_factory=lambda: ["image", "video", "upload", "poll"])
    max_jobs: int = Field(default=1, ge=1, le=20)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerHeartbeat(BaseModel):
    connection_id: str
    status: str = WorkerStatus.READY.value
    credits: int | None = None
    current_jobs: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerResponse(BaseModel):
    connection_id: str
    command_id: str
    response: dict[str, Any] | None = None
    error: str | None = None


class WorkerCredentialIssue(BaseModel):
    worker_id: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=80)
    account_id: str = Field(min_length=1, max_length=36)
    expires_in_seconds: int | None = Field(default=None, ge=60, le=30 * 24 * 60 * 60)


class CharacterCreate(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    canonical_facts: dict[str, Any] = Field(default_factory=dict)


class CharacterConfirm(BaseModel):
    master_asset_id: str
    references: dict[str, str | None] = Field(default_factory=dict)
    hair_signature: str = ""
    costume_signature: str = ""


class CharacterStatePatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["ADD", "REPLACE", "REMOVE"]
    path: str = Field(min_length=1, max_length=320)
    from_value: Any | None = Field(default=None, alias="from")
    to: Any | None = None

    def as_patch_operation(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": self.op, "path": self.path}
        if "from_value" in self.model_fields_set:
            payload["from"] = self.from_value
        if "to" in self.model_fields_set:
            payload["to"] = self.to
        return payload


class CandidateCharacterStateDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    character_id: str = Field(min_length=1, max_length=36)
    base_state_version_id: str = Field(min_length=1, max_length=36)
    operations: list[CharacterStatePatchOperation] = Field(min_length=1, max_length=100)

    def as_service_delta(self) -> dict[str, object]:
        return {
            "character_id": self.character_id,
            "base_state_version_id": self.base_state_version_id,
            "patch": {"operations": [item.as_patch_operation() for item in self.operations]},
        }


class CharacterStateInitialize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1, max_length=36)
    shot_id: str = Field(min_length=1, max_length=36)
    candidate_id: str = Field(min_length=1, max_length=36)
    timeline_scope_key: str = Field(default="main", min_length=1, max_length=120)
    narrative_state: dict[str, Any]
    reason: str = Field(min_length=1, max_length=2000)
    explicit_confirmation: Literal[True]


class CandidateGenerate(BaseModel):
    idempotency_key: str = Field(min_length=3, max_length=250)
    fallback_providers: list[str] = Field(
        default_factory=lambda: ["google_flow", "seedance", "veo_official", "kling", "grok"]
    )
    character_ids: list[str] = Field(default_factory=list, max_length=20)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=100)
    estimated_cost: float = Field(default=0.0, ge=0)
    state_deltas: list[CandidateCharacterStateDelta] = Field(default_factory=list, max_length=20)


class CandidateValidate(BaseModel):
    evidence: dict[str, Any] = Field(default_factory=dict)


class HumanReviewApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)
    explicit_confirmation: bool = Field(default=False, strict=True)


class CharacterEvidenceReconcile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["RESUBMIT", "MARK_FAILED"]
    note: str = Field(min_length=1, max_length=2000)
    resolved_by: str = Field(min_length=1, max_length=120)


class TimelineBranchMerge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    into_scope_key: str = Field(default="main", min_length=1, max_length=120)
    allowed_state_paths: list[str] = Field(min_length=1, max_length=100)
    allow_dream_states: bool = Field(default=False, strict=True)


class TimelineBranchClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


class PromptRefine(BaseModel):
    project_id: str
    prompt: str = Field(min_length=1, max_length=30_000)


class ContinuityEvaluate(BaseModel):
    project_id: str
    risk: dict[str, Any] = Field(default_factory=dict)


class ShotDependencyDeclare(BaseModel):
    project_id: str
    dependency_type: str = Field(min_length=1, max_length=40)
    source_shot_id: str | None = None
    fact_key: str | None = Field(default=None, max_length=160)
    obligation_key: str | None = Field(default=None, max_length=160)
    summary: str = Field(default="", max_length=2000)


class ProviderProjectBind(BaseModel):
    provider: str
    provider_account_id: str
    provider_project_id: str = Field(min_length=1, max_length=500)


def _job_view(job, *, credit_status: str | None = None) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "id": job.id,
        "status": job.status,
        "provider": job.provider,
        "model": job.model,
        "provider_job_id": job.provider_job_id,
        "submission_state": job.submission_state,
        "credit_status": credit_status,
        "output_asset_id": job.output_asset_id,
        "safe_to_retry": job.safe_to_retry,
        "attempt_count": job.attempt_count,
        "error_code": job.error_code,
        "error_message": job.error_message,
    }


def _candidate_view(candidate, qa=None, costs: list | None = None) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    cost_rows = costs or []
    known_actuals = [item for item in cost_rows if item.actual_cost is not None]
    effective_cost = sum(
        (item.actual_cost if item.actual_cost is not None else item.estimated_cost) + item.retry_cost
        for item in cost_rows
    )
    if not cost_rows:
        cost_source = "UNKNOWN"
    elif len(known_actuals) == len(cost_rows):
        cost_source = "ACTUAL"
    elif known_actuals:
        cost_source = "MIXED_ACTUAL_ESTIMATED"
    else:
        cost_source = "ESTIMATED"
    return {
        "id": candidate.id,
        "shot_id": candidate.shot_id,
        "attempt_number": candidate.attempt_number,
        "generation_job_id": candidate.generation_job_id,
        "output_asset_id": candidate.output_asset_id,
        "status": candidate.status,
        "accepted_by": candidate.accepted_by,
        "rejection_reason": candidate.rejection_reason,
        "generation_plan": candidate.metadata_json.get("generation_plan", {}),
        "qa": (
            {
                "id": qa.id,
                "profile": qa.profile,
                "decision": qa.decision,
                "overall_score": qa.overall_score,
                "character_score": qa.character_score,
                "camera_score": qa.camera_score,
                "action_score": qa.action_score,
                "summary": qa.summary,
                "hard_failures": qa.hard_failures,
                "human_review": (
                    {
                        "reviewer_user_id": qa.metrics_json.get("reviewer_user_id"),
                        "reason": qa.metrics_json.get("reason"),
                        "source": qa.metrics_json.get("source"),
                    }
                    if qa.profile == "HUMAN_REVIEW"
                    else None
                ),
            }
            if qa
            else None
        ),
        "cost": round(effective_cost, 4),
        "cost_source": cost_source,
        "known_actual_cost": round(
            sum(item.actual_cost + item.retry_cost for item in known_actuals),
            4,
        ),
        "estimated_fallback_cost": round(
            sum(item.estimated_cost + item.retry_cost for item in cost_rows if item.actual_cost is None),
            4,
        ),
    }


def create_app(container: Container | None = None) -> FastAPI:
    container = container or build_container()
    auth = AuthService(
        container.database,
        session_ttl_days=container.settings.auth_session_ttl_days,
        auth_required=container.settings.auth_required,
        deployment_environment=container.settings.deployment_environment,
    )
    storage_quota = WorkspaceStorageQuota(container.database)
    worker_credentials = WorkerCredentialService(
        container.database,
        default_ttl_seconds=container.settings.worker_credential_ttl_seconds,
        socket_ticket_ttl_seconds=container.settings.worker_socket_ticket_ttl_seconds,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        container.gateway.recover_after_restart()
        yield

    app = FastAPI(title="AI Director Platform", version="1.0.0", lifespan=lifespan)
    app.state.container = container
    app.add_middleware(CookieCSRFMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip() for origin in container.settings.web_origins.split(",") if origin.strip()
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        UploadSizeLimitMiddleware,
        max_file_bytes=container.settings.max_upload_bytes,
        multipart_overhead_bytes=container.settings.max_upload_request_overhead_bytes,
    )

    @app.post("/v1/webhooks/alchemy")
    async def receive_alchemy_webhook(request: Request):
        raw_body = await request.body()
        try:
            result = container.alchemy_webhooks.handle(
                raw_body,
                request.headers.get("x-alchemy-signature"),
            )
        except AlchemyWebhookConfigurationError as exc:
            raise HTTPException(503, str(exc)) from exc
        except AlchemyWebhookAuthenticationError as exc:
            raise HTTPException(401, str(exc)) from exc
        except AlchemyWebhookPayloadError as exc:
            raise HTTPException(400, str(exc)) from exc
        except AlchemyWebhookConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return result.as_dict()

    @app.post("/v1/webhooks/depay")
    async def receive_depay_webhook(request: Request):
        raw_body = await request.body()
        try:
            result = container.depay_payments.handle_callback(
                raw_body,
                request.headers.get("x-signature"),
            )
        except DePayConfigurationError as exc:
            raise HTTPException(503, str(exc)) from exc
        except DePayAuthenticationError as exc:
            raise HTTPException(401, str(exc)) from exc
        except DePayPayloadError as exc:
            raise HTTPException(400, str(exc)) from exc
        except DePayConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return result.as_dict()

    @app.post("/v1/webhooks/character-evidence")
    async def receive_character_evidence_callback(request: Request):
        raw_body = await request.body()
        try:
            callback = verify_callback(
                raw_body,
                timestamp=request.headers.get("x-character-evidence-timestamp"),
                signature=request.headers.get("x-character-evidence-signature"),
                signing_key=container.settings.character_evidence_callback_signing_key,
            )
            with container.database.session() as session:
                candidate = session.get(GenerationCandidate, callback.job_id)
                if candidate is None:
                    raise LookupError("character evidence callback candidate was not found")
                shot = session.get(Shot, candidate.shot_id)
                output = (
                    session.get(MediaAsset, candidate.output_asset_id)
                    if candidate.output_asset_id
                    else None
                )
                if shot is None or output is None:
                    raise LookupError("character evidence callback candidate context is incomplete")
                if shot.id != callback.shot_id or output.project_id != callback.project_id:
                    raise ValueError("character evidence callback lineage does not match the candidate")
                if candidate.metadata_json.get("character_evidence_job_id") != callback.job_id:
                    raise ValueError("character evidence callback job was not submitted by this service")
                completed_run_ids = set(
                    candidate.metadata_json.get("character_evidence_run_ids", [])
                )
            if callback.status == "FAILED":
                container.qa.record_character_evidence_failure(
                    callback.job_id,
                    job_id=callback.job_id,
                    error_code=callback.error_code,
                    error_message=callback.error_message,
                )
                container.character_evidence_tracker.record_callback(
                    callback.job_id, status="FAILED", error_code=callback.error_code
                )
                return {"status": "RECORDED", "reports": 0}
            reports = [report_from_payload(item) for item in callback.reports]
            for report in reports:
                if report.candidate_id != callback.job_id:
                    raise ValueError("character evidence report belongs to a different candidate")
                if report.operating_mode != "SHADOW":
                    raise ValueError("character evidence callback attempted a non-shadow decision")
                if report.producer_run_id in completed_run_ids:
                    continue
                container.qa.validate_candidate(
                    callback.job_id,
                    character_evidence=report,
                    observation_only=True,
                )
            container.character_evidence_tracker.record_callback(
                callback.job_id, status="SUCCEEDED"
            )
            return {"status": "RECORDED", "reports": len(reports)}
        except CharacterEvidenceCallbackAuthenticationError as exc:
            raise HTTPException(401, str(exc)) from exc
        except CharacterEvidenceCallbackPayloadError as exc:
            raise HTTPException(400, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    def verify_api_key(authorization: str | None = Header(default=None)) -> None:
        expected = container.settings.platform_api_key
        if not expected:
            raise HTTPException(503, "PLATFORM_API_KEY is required for internal/admin routes")
        token = authorization.removeprefix("Bearer ").strip() if authorization else ""
        if not secrets.compare_digest(token, expected):
            raise HTTPException(401, "invalid API key")

    def require_worker_credential(
        authorization: str | None = Header(default=None),
    ) -> WorkerPrincipal:
        try:
            return worker_credentials.authenticate_authorization(authorization)
        except WorkerAuthenticationError as exc:
            raise HTTPException(401, str(exc)) from exc

    def require_worker_binding(
        principal: WorkerPrincipal,
        *,
        worker_id: str,
        provider: str | None = None,
        account_id: str | None = None,
    ) -> None:
        if principal.worker_id != worker_id:
            raise HTTPException(403, "worker credential is bound to another worker")
        if provider is not None and principal.provider != provider:
            raise HTTPException(403, "worker credential is bound to another provider")
        if account_id is not None and principal.account_id != account_id:
            raise HTTPException(403, "worker credential is bound to another account")

    def ensure_workspace(
        session,
        principal: AuthPrincipal,
        requested_id: str | None = None,
    ):  # type: ignore[no-untyped-def]
        if requested_id:
            workspace = session.get(Workspace, requested_id)
            if not workspace:
                raise HTTPException(404, "workspace not found")
            auth.require_workspace(principal, workspace.id, write=True)
            return workspace
        authorized_workspace_id = auth.first_workspace_id(principal, write=True)
        if authorized_workspace_id:
            workspace = session.get(Workspace, authorized_workspace_id)
            if workspace:
                return workspace
        if not principal.development_bypass:
            raise HTTPException(403, "账号尚未加入可用的工作空间")
        workspace = session.scalar(select(Workspace).order_by(Workspace.created_at))
        if workspace:
            return workspace
        user = session.scalar(select(User).where(User.email == "local@ai-director.invalid"))
        if not user:
            user = User(email="local@ai-director.invalid", display_name="Local Director")
            session.add(user)
            session.flush()
        workspace = Workspace(
            owner_user_id=user.id,
            name="Director Workspace",
            # Authentication-disabled local development is the explicit legacy
            # bypass surface; it still receives server pricing/CostRecords but
            # must not consume a real Free-plan wallet.
            plan_tier="ALL",
        )
        session.add(workspace)
        session.flush()
        return workspace

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "ai-video-platform", "providers": container.providers.list()}

    @app.post("/v1/projects")
    def create_project(
        body: ProjectCreate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            workspace = ensure_workspace(session, principal, body.workspace_id)
            item = Project(
                workspace_id=workspace.id,
                name=body.title,
                title=body.title,
                description=body.description,
                default_aspect_ratio=body.default_aspect_ratio,
                default_provider=body.default_provider,
                default_language=body.default_language,
            )
            session.add(item)
            session.flush()
            return {
                "id": item.id,
                "title": item.title,
                "status": item.status,
                "canonical_style_version_id": item.canonical_style_version_id,
            }

    @app.get("/v1/projects")
    def list_projects(principal: AuthPrincipal = Depends(auth.current_user)):
        with container.database.session() as session:
            query = select(Project).order_by(Project.updated_at.desc())
            if not principal.development_bypass:
                query = query.where(Project.workspace_id.in_(principal.workspace_roles))
            return [
                {
                    "id": project.id,
                    "workspace_id": project.workspace_id,
                    "name": project.name or project.title,
                    "description": project.description,
                    "status": project.status,
                    "default_provider": project.default_provider,
                    "default_aspect_ratio": project.default_aspect_ratio,
                    "canonical_style_version_id": project.canonical_style_version_id,
                }
                for project in session.scalars(query)
            ]

    @app.get("/v1/projects/{project_id}")
    def get_project(
        project_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, project_id)
        with container.database.session() as session:
            project = session.get(Project, project_id)
            if not project:
                raise HTTPException(404, "project not found")
            episodes = list(
                session.scalars(
                    select(Episode).where(Episode.project_id == project.id).order_by(Episode.episode_number)
                )
            )
            return {
                "id": project.id,
                "workspace_id": project.workspace_id,
                "name": project.name or project.title,
                "description": project.description,
                "status": project.status,
                "canonical_style_version_id": project.canonical_style_version_id,
                "defaults": {
                    "aspect_ratio": project.default_aspect_ratio,
                    "provider": project.default_provider,
                    "language": project.default_language,
                },
                "episodes": [
                    {
                        "id": episode.id,
                        "episode_number": episode.episode_number,
                        "title": episode.title,
                        "status": episode.status,
                    }
                    for episode in episodes
                ],
            }

    @app.post("/v1/episodes")
    def create_episode(
        body: EpisodeCreate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, body.project_id, write=True)
        with container.database.session() as session:
            if not session.get(Project, body.project_id):
                raise HTTPException(404, "project not found")
            item = Episode(**body.model_dump())
            session.add(item)
            session.flush()
            return {"id": item.id, "project_id": item.project_id, "episode_number": item.episode_number}

    @app.post("/v1/projects/{project_id}/episodes")
    def create_project_episode(
        project_id: str,
        body: EpisodeCreate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        if body.project_id != project_id:
            raise HTTPException(409, "project ID in path and body differ")
        return create_episode(body, principal)

    @app.post("/v1/episodes/{episode_id}/compile")
    def compile_episode(
        episode_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            episode = session.get(Episode, episode_id)
            if not episode:
                raise HTTPException(404, "episode not found")
            auth.require_project(principal, episode.project_id, write=True)
        try:
            result = container.orchestrator.compile_episode(episode_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"episode_id": episode_id, "stage": result.stage, **result.detail}

    @app.get("/v1/episodes/{episode_id}")
    def get_episode(
        episode_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            episode = session.get(Episode, episode_id)
            if not episode:
                raise HTTPException(404, "episode not found")
            auth.require_project(principal, episode.project_id)
            scenes = list(
                session.scalars(select(Scene).where(Scene.episode_id == episode.id).order_by(Scene.sequence))
            )
            return {
                "id": episode.id,
                "project_id": episode.project_id,
                "title": episode.title,
                "episode_number": episode.episode_number,
                "script_source": episode.script_source,
                "script_structured": episode.script_structured,
                "status": episode.status,
                "scenes": [
                    {
                        "id": scene.id,
                        "sequence": scene.sequence,
                        "description": scene.scene_description or scene.description,
                        "time_context": scene.time_context,
                        "shots": [
                            {
                                "id": shot.id,
                                "sequence": shot.sequence,
                                "shot_type": shot.shot_type,
                                "duration": shot.duration,
                                "status": shot.status,
                                "prompt": shot.user_prompt or shot.prompt,
                                "continuity_policy": shot.continuity_policy,
                                "generation_policy": shot.generation_policy,
                                "provider": shot.preferred_provider,
                            }
                            for shot in session.scalars(
                                select(Shot).where(Shot.scene_id == scene.id).order_by(Shot.sequence)
                            )
                        ],
                    }
                    for scene in scenes
                ],
            }

    @app.post("/v1/scenes")
    def create_scene(
        body: SceneCreate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            episode = session.get(Episode, body.episode_id)
            if not episode:
                raise HTTPException(404, "episode not found")
            auth.require_project(principal, episode.project_id, write=True)
            values = body.model_dump()
            values["scene_description"] = body.description
            item = Scene(**values)
            session.add(item)
            session.flush()
            return {"id": item.id, "episode_id": item.episode_id, "sequence": item.sequence}

    @app.post("/v1/shots")
    def create_shot(
        body: ShotCreate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            scene = session.get(Scene, body.scene_id)
            if not scene:
                raise HTTPException(404, "scene not found")
            episode = session.get(Episode, scene.episode_id)
            auth.require_project(principal, episode.project_id, write=True)
            if body.previous_shot_id:
                previous = session.get(Shot, body.previous_shot_id)
                if not previous:
                    raise HTTPException(404, "previous shot not found")
                previous_scene = session.get(Scene, previous.scene_id)
                previous_episode = session.get(Episode, previous_scene.episode_id) if previous_scene else None
                if not previous_episode or previous_episode.project_id != episode.project_id:
                    raise HTTPException(422, "previous shot must belong to the same project")
            values = body.model_dump()
            values["user_prompt"] = body.prompt
            values["compiled_prompt"] = body.prompt
            values["preferred_provider"] = body.provider
            values["preferred_model"] = body.model
            values["continuity_policy"] = body.continuity_mode
            item = Shot(**values)
            session.add(item)
            session.flush()
            return {
                "id": item.id,
                "scene_id": item.scene_id,
                "sequence": item.sequence,
                "continuity_mode": item.continuity_mode,
            }

    @app.get("/v1/shots/{shot_id}")
    def get_shot(
        shot_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise HTTPException(404, "shot not found")
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id)
            auth.require_project(principal, episode.project_id)
            input_state = session.get(TimelineState, shot.input_state_id) if shot.input_state_id else None
            output_state = session.get(TimelineState, shot.output_state_id) if shot.output_state_id else None
            return {
                "id": shot.id,
                "scene_id": shot.scene_id,
                "sequence": shot.sequence,
                "shot_type": shot.shot_type,
                "duration": shot.duration,
                "status": shot.status,
                "user_prompt": shot.user_prompt or shot.prompt,
                "compiled_prompt": shot.compiled_prompt,
                "continuity_policy": shot.continuity_policy,
                "generation_policy": shot.generation_policy,
                "provider": shot.preferred_provider,
                "model": shot.preferred_model,
                "input_state": input_state.state_json if input_state else None,
                "output_state": output_state.state_json if output_state else None,
                "committed_candidate_id": shot.committed_candidate_id,
            }

    @app.post("/v1/shots/{shot_id}/generate", status_code=202)
    def generate_shot(
        shot_id: str,
        body: CandidateGenerate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise HTTPException(404, "shot not found")
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id)
            auth.require_project(principal, episode.project_id, write=True)
            project_id = episode.project_id
            input_state_id = shot.input_state_id
            if body.character_ids:
                owned_character_ids = set(
                    session.scalars(
                        select(Character.id).where(
                            Character.id.in_(body.character_ids),
                            Character.project_id == episode.project_id,
                        )
                    )
                )
                if owned_character_ids != set(body.character_ids):
                    raise HTTPException(404, "character not found in shot project")
            delta_character_ids = [item.character_id for item in body.state_deltas]
            if len(delta_character_ids) != len(set(delta_character_ids)):
                raise HTTPException(422, "each character may have only one state delta")
            if not set(delta_character_ids).issubset(set(body.character_ids)):
                raise HTTPException(422, "state delta characters must be included in character_ids")
            if delta_character_ids and principal.development_bypass:
                raise HTTPException(403, "角色状态变更需要真实登录用户确认来源")
        try:
            bindings = [
                container.characters.binding(
                    character_id,
                    project_id=project_id,
                    timeline_state_id=input_state_id,
                )
                for character_id in body.character_ids
            ]
            candidate, replayed = container.candidates.create_candidate(
                shot_id,
                idempotency_key=body.idempotency_key,
                fallback_providers=(body.fallback_providers if principal.development_bypass else None),
                character_bindings=bindings,
                reference_asset_ids=body.reference_asset_ids,
                estimated_cost=body.estimated_cost,
                enforce_entitlements=not principal.development_bypass,
                state_deltas=[item.as_service_delta() for item in body.state_deltas],
                proposed_by_user_id=(None if principal.development_bypass else principal.user_id),
                state_delta_source="HUMAN",
            )
            return {**_candidate_view(candidate), "replayed": replayed}
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
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
        except (CharacterStateConflict, CharacterStatePolicyViolation, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/v1/shots/{shot_id}/candidates")
    def list_candidates(
        shot_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise HTTPException(404, "shot not found")
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id)
            auth.require_project(principal, episode.project_id)
            candidates = list(
                session.scalars(
                    select(GenerationCandidate)
                    .where(GenerationCandidate.shot_id == shot_id)
                    .order_by(GenerationCandidate.attempt_number)
                )
            )
            return [
                _candidate_view(
                    candidate,
                    session.get(QAResult, candidate.qa_result_id) if candidate.qa_result_id else None,
                    list(session.scalars(select(CostRecord).where(CostRecord.candidate_id == candidate.id))),
                )
                for candidate in candidates
            ]

    @app.post("/v1/shots/{shot_id}/candidates/{candidate_id}/validate")
    def validate_candidate(
        shot_id: str,
        candidate_id: str,
        body: CandidateValidate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate or candidate.shot_id != shot_id:
                raise HTTPException(404, "candidate not found for shot")
            shot = session.get(Shot, shot_id)
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id)
            auth.require_project(principal, episode.project_id, write=True)
        if body.evidence:
            raise HTTPException(403, "质量评分只能由受信任的内部评审服务写入")
        try:
            result = container.candidates.sync_candidate(candidate_id)
            return _candidate_view(result)
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post(
        "/internal/shots/{shot_id}/candidates/{candidate_id}/validate",
        dependencies=[Depends(verify_api_key)],
    )
    def internal_validate_candidate(
        shot_id: str,
        candidate_id: str,
        body: CandidateValidate,
    ):
        with container.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate or candidate.shot_id != shot_id:
                raise HTTPException(404, "candidate not found for shot")
        try:
            trusted_evidence = {**body.evidence, "_trusted_source": "INTERNAL_QC"}
            result = container.candidates.sync_candidate(candidate_id, trusted_evidence)
            return _candidate_view(result)
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/shots/{shot_id}/candidates/{candidate_id}/human-review")
    def approve_candidate_after_human_review(
        shot_id: str,
        candidate_id: str,
        body: HumanReviewApprove,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        if principal.development_bypass:
            raise HTTPException(403, "人工复核必须由真实登录用户完成")
        with container.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate or candidate.shot_id != shot_id:
                raise HTTPException(404, "candidate not found for shot")
            shot = session.get(Shot, shot_id)
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id)
            project_id = episode.project_id
            auth.require_project(principal, project_id, write=True)
        try:
            review = container.qa.approve_human_review(
                candidate_id,
                project_id=project_id,
                reviewer_user_id=principal.user_id,
                reason=body.reason,
                explicit_confirmation=body.explicit_confirmation,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except HumanReviewNotAllowed as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        with container.database.session() as session:
            approved = session.get(GenerationCandidate, candidate_id)
            if approved is None:
                raise HTTPException(404, "candidate not found")
            return _candidate_view(approved, session.get(QAResult, review.id))

    @app.post("/v1/shots/{shot_id}/candidates/{candidate_id}/commit")
    def commit_candidate(
        shot_id: str,
        candidate_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate or candidate.shot_id != shot_id:
                raise HTTPException(404, "candidate not found for shot")
            shot = session.get(Shot, shot_id)
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id)
            auth.require_project(principal, episode.project_id, write=True)
        try:
            return _candidate_view(
                container.candidates.commit(
                    candidate_id,
                    accepted_by=(None if principal.development_bypass else principal.user_id),
                )
            )
        except CandidateNotCommittable as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/characters")
    def create_character(
        body: CharacterCreate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, body.project_id, write=True)
        with container.database.session() as session:
            if not session.get(Project, body.project_id):
                raise HTTPException(404, "project not found")
        character = container.characters.create_character(
            body.project_id, body.name, body.description, body.canonical_facts
        )
        return {"id": character.id, "name": character.name, "status": character.status}

    @app.get("/v1/projects/{project_id}/characters")
    def list_characters(
        project_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, project_id)
        with container.database.session() as session:
            if not session.get(Project, project_id):
                raise HTTPException(404, "project not found")
            characters = list(
                session.scalars(
                    select(Character).where(Character.project_id == project_id).order_by(Character.created_at)
                )
            )
            result = []
            for character in characters:
                versions = list(
                    session.scalars(
                        select(CharacterIdentityVersion)
                        .where(CharacterIdentityVersion.character_id == character.id)
                        .order_by(CharacterIdentityVersion.version)
                    )
                )
                result.append(
                    {
                        "id": character.id,
                        "name": character.name,
                        "description": character.description,
                        "status": character.status,
                        "current_identity_version_id": character.current_identity_version_id,
                        "identity_versions": [
                            {
                                "id": identity.id,
                                "version": identity.version,
                                "status": identity.status,
                                "master_asset_id": identity.master_asset_id,
                            }
                            for identity in versions
                        ],
                    }
                )
            return result

    @app.post("/v1/characters/{character_id}/confirm-identity")
    def confirm_character_identity(
        character_id: str,
        body: CharacterConfirm,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            character = session.get(Character, character_id)
            if not character:
                raise HTTPException(404, "character not found")
            auth.require_project(principal, character.project_id, write=True)
        try:
            identity = container.characters.confirm_identity(
                character_id,
                body.master_asset_id,
                references=body.references,
                hair_signature=body.hair_signature,
                costume_signature=body.costume_signature,
            )
            with container.database.session() as session:
                character = session.get(Character, character_id)
                master = session.get(MediaAsset, body.master_asset_id)
                project_id = character.project_id
                character_name = character.name
            logical = next(
                (
                    item
                    for item in container.asset_registry.list(project_id, asset_type="CHARACTER")
                    if item.canonical_metadata.get("character_id") == character_id
                ),
                None,
            )
            if logical is None:
                logical = container.asset_registry.create(
                    project_id,
                    "CHARACTER",
                    character_name,
                    canonical_metadata={"character_id": character_id},
                    created_by_user_id=(None if principal.development_bypass else principal.user_id),
                )
            asset_version = container.asset_registry.add_version(
                logical.id,
                primary_media_asset_id=body.master_asset_id,
                references=[
                    VersionMediaInput(media_asset_id=media_id, role=role)
                    for role, media_id in body.references.items()
                    if media_id
                ],
                label=f"Identity v{identity.version}",
                source="CHARACTER_IDENTITY_CONFIRMATION",
                metadata={
                    "character_identity_version_id": identity.id,
                    "hair_signature": body.hair_signature,
                    "costume_signature": body.costume_signature,
                },
                created_by_user_id=(None if principal.development_bypass else principal.user_id),
            )
            container.asset_registry.promote(
                logical.id,
                asset_version.id,
                promoted_by_user_id=(None if principal.development_bypass else principal.user_id),
                reason="explicit character identity confirmation",
            )
            memory_id = None
            if container.feature_flags.enabled("voyage_memory", project_id=project_id):
                memory = container.memory.index(
                    ShotMemoryInput(
                        project_id=project_id,
                        layer=MemoryLayer.CANONICAL,
                        memory_type="CHARACTER_ASSET",
                        content=MultimodalContent(
                            text=(
                                f"Canonical character {character_name}; hair {body.hair_signature}; "
                                f"wardrobe {body.costume_signature}"
                            ),
                            image_urls=(
                                [master.public_url]
                                if master and master.public_url and master.public_url.startswith("https://")
                                else []
                            ),
                        ),
                        entity_ids=[logical.id, character_id],
                        asset_version_ids=[asset_version.id],
                        canonical=True,
                    )
                )
                memory_id = memory.id
            return {
                "id": identity.id,
                "character_id": identity.character_id,
                "version": identity.version,
                "status": identity.status,
                "master_asset_id": identity.master_asset_id,
                "logical_asset_id": logical.id,
                "logical_asset_version_id": asset_version.id,
                "memory_id": memory_id,
            }
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/characters/{character_id}/narrative-state/initialize", status_code=201)
    def initialize_character_narrative_state(
        character_id: str,
        body: CharacterStateInitialize,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        if principal.development_bypass:
            raise HTTPException(403, "角色状态初始化需要真实登录用户确认")
        auth.require_project(principal, body.project_id, write=True)
        with container.database.session() as session:
            character = session.get(Character, character_id)
            if character is None or character.project_id != body.project_id:
                raise HTTPException(404, "character not found in project")
        try:
            version = container.character_states.initialize_from_committed_candidate(
                project_id=body.project_id,
                character_id=character_id,
                shot_id=body.shot_id,
                candidate_id=body.candidate_id,
                narrative_state=body.narrative_state,
                timeline_scope_key=body.timeline_scope_key,
                committed_by_user_id=principal.user_id,
                reason=body.reason,
            )
            return {
                "id": version.id,
                "character_id": version.character_id,
                "timeline_scope_key": version.timeline_scope_key,
                "version": version.version,
                "previous_state_version_id": version.previous_state_version_id,
                "identity_version_id": version.identity_version_id,
                "source_shot_id": version.source_shot_id,
                "source_candidate_id": version.source_candidate_id,
                "state_hash": version.state_hash,
                "narrative_state": version.narrative_state_json,
            }
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (CharacterStateConflict, CharacterStatePolicyViolation, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/v1/projects/{project_id}/characters/{character_id}/narrative-state")
    def current_character_narrative_state(
        project_id: str,
        character_id: str,
        timeline_scope_key: str = "main",
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, project_id)
        try:
            version = container.character_states.current(
                project_id,
                character_id,
                timeline_scope_key=timeline_scope_key,
            )
            if version is None:
                raise HTTPException(404, "character narrative state is not initialized")
            binding = container.characters.binding(
                character_id,
                project_id=project_id,
                timeline_scope_key=timeline_scope_key,
            )
            return {
                "character_id": character_id,
                "identity": {
                    "identity_version_id": binding["identity_version_id"],
                    "identity_version": binding["version"],
                    "hair_signature": binding["hair_signature"],
                    "costume_signature": binding["costume_signature"],
                    "canonical_assets": binding["canonical_assets"],
                },
                "narrative_state": {
                    "id": version.id,
                    "version": version.version,
                    "previous_state_version_id": version.previous_state_version_id,
                    "timeline_scope_key": version.timeline_scope_key,
                    "state_hash": version.state_hash,
                    "source_shot_id": version.source_shot_id,
                    "source_candidate_id": version.source_candidate_id,
                    "value": version.narrative_state_json,
                },
            }
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/v1/shots/{shot_id}/candidates/{candidate_id}/state-transitions")
    def list_candidate_state_transitions(
        shot_id: str,
        candidate_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if candidate is None or candidate.shot_id != shot_id:
                raise HTTPException(404, "candidate not found for shot")
            shot = session.get(Shot, shot_id)
            auth.require_project(principal, shot.scene.episode.project_id)
        return container.character_states.transition_view(candidate_id)

    @app.post("/v1/prompts/refine")
    async def refine_prompt(
        body: PromptRefine,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, body.project_id, write=True)
        with container.database.session() as session:
            if not session.get(Project, body.project_id):
                raise HTTPException(404, "project not found")
        result = container.image_prompts.correct(ImagePromptCorrectRequest(prompt=body.prompt))
        approved_prompt = result.corrected_prompt
        role_result = await container.model_roles.refine_prompt(
            body.project_id,
            original_prompt=approved_prompt,
            fact_locks=FactLockSet(
                {"narrative_event": approved_prompt},
                locked_spans={"narrative_event": (approved_prompt,)},
            ),
        )
        refined_prompt = role_result.optimized_candidate
        with container.database.session() as session:
            compilation = PromptCompilation(
                project_id=body.project_id,
                user_prompt=result.original_prompt,
                compiled_prompt=refined_prompt,
                compiler_version=(f"{result.corrector_version}+{container.model_roles.version}"),
                skill_versions={
                    "image-prompt-corrector": "v1",
                    "model-role-runtime": container.model_roles.version,
                },
                diff_json={
                    "changes": [change.model_dump() for change in result.changes],
                    "preserved_facts": result.preserved_constraints,
                    "model_refinement": {
                        "accepted": role_result.accepted,
                        "source": role_result.source,
                        "reason_codes": list(role_result.reason_codes),
                        "diff": role_result.diff,
                    },
                },
            )
            session.add(compilation)
            session.flush()
            return {
                "id": compilation.id,
                "original": result.original_prompt,
                "refined": refined_prompt,
                "changes": [change.model_dump() for change in result.changes],
                "preserved_facts": result.preserved_constraints,
                "model_refinement": {
                    "accepted": role_result.accepted,
                    "source": role_result.source,
                    "reason_codes": list(role_result.reason_codes),
                },
            }

    @app.post("/api/prompt/correct")
    def correct_image_prompt(
        body: ImagePromptCorrectRequest,
        principal: AuthPrincipal = Depends(auth.current_user),
        project_id: str | None = None,
    ):
        if project_id:
            auth.require_project(principal, project_id, write=True)
        if body.reference_assets:
            with container.database.session() as session:
                references = list(
                    session.scalars(select(MediaAsset).where(MediaAsset.id.in_(body.reference_assets)))
                )
                if len({item.id for item in references}) != len(set(body.reference_assets)):
                    raise HTTPException(404, "reference asset not found")
                for reference in references:
                    if project_id and reference.project_id != project_id:
                        raise HTTPException(409, "reference asset does not belong to project")
                    auth.require_project(principal, reference.project_id)
        result = container.image_prompts.correct(body)
        with container.database.session() as session:
            revision = PromptRevision(
                project_id=project_id,
                user_id=None if principal.development_bypass else principal.user_id,
                mode="IMAGE",
                original_prompt=result.original_prompt,
                corrected_prompt=result.corrected_prompt,
                detected_type=result.detected_type,
                reference_asset_ids=body.reference_assets,
                preserved_constraints=result.preserved_constraints,
                editable_variables=result.editable_variables,
                changes_json=[change.model_dump() for change in result.changes],
                corrector_version=result.corrector_version,
            )
            session.add(revision)
            session.flush()
            return {"revision_id": revision.id, **result.model_dump()}

    @app.post("/v1/shots/{shot_id}/continuity")
    def evaluate_continuity(
        shot_id: str,
        body: ContinuityEvaluate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, body.project_id, write=True)
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise HTTPException(404, "shot not found")
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id)
            if episode.project_id != body.project_id:
                raise HTTPException(409, "shot does not belong to project")
        try:
            risk = ContinuityRiskVector(**body.risk)
            result = container.orchestrator.plan_continuity(shot_id, body.project_id, risk)
            return result.detail
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except TypeError as exc:
            raise HTTPException(422, str(exc)) from exc

    def _shot_dependency_view(row) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return {
            "id": row.id,
            "project_id": row.project_id,
            "target_shot_id": row.target_shot_id,
            "source_shot_id": row.source_shot_id,
            "dependency_type": row.dependency_type,
            "fact_key": row.fact_key,
            "obligation_key": row.obligation_key,
            "summary": row.summary,
            "origin": row.origin,
            "created_at": row.created_at,
        }

    def _require_shot_project(shot_id: str, project_id: str, principal: AuthPrincipal) -> None:
        auth.require_project(principal, project_id, write=True)
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise HTTPException(404, "shot not found")
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id)
            if episode.project_id != project_id:
                raise HTTPException(409, "shot does not belong to project")

    @app.post("/v1/shots/{shot_id}/dependencies")
    def declare_shot_dependency(
        shot_id: str,
        body: ShotDependencyDeclare,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        """Manual editing surface for explicit plot dependencies."""

        _require_shot_project(shot_id, body.project_id, principal)
        try:
            row = container.shot_dependencies.declare(
                body.project_id,
                target_shot_id=shot_id,
                dependency_type=body.dependency_type,
                source_shot_id=body.source_shot_id,
                fact_key=body.fact_key,
                obligation_key=body.obligation_key,
                summary=body.summary,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return _shot_dependency_view(row)

    @app.get("/v1/shots/{shot_id}/dependencies")
    def list_shot_dependencies(
        shot_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise HTTPException(404, "shot not found")
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id)
            auth.require_project(principal, episode.project_id)
        return [_shot_dependency_view(row) for row in container.shot_dependencies.list_for(shot_id)]

    @app.delete("/v1/shots/{shot_id}/dependencies/{dependency_id}")
    def remove_shot_dependency(
        shot_id: str,
        dependency_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise HTTPException(404, "shot not found")
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id)
            auth.require_project(principal, episode.project_id, write=True)
            project_id = episode.project_id
            row = session.get(ShotDependency, dependency_id)
            if row is None or row.target_shot_id != shot_id:
                raise HTTPException(404, "shot dependency not found")
        try:
            container.shot_dependencies.remove(project_id, dependency_id=dependency_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"removed": dependency_id}

    @app.get("/v1/projects/{project_id}/timeline-branches")
    def list_timeline_branches(
        project_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, project_id)
        return {"branches": container.timeline_branches.list_for_project(project_id)}

    @app.post("/v1/projects/{project_id}/timeline-branches/{scope_key}/merge")
    def merge_timeline_branch(
        project_id: str,
        scope_key: str,
        body: TimelineBranchMerge,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        if principal.development_bypass:
            raise HTTPException(403, "时间线分支合并必须由真实登录用户确认")
        auth.require_project(principal, project_id, write=True)
        try:
            return container.timeline_branches.merge(
                project_id,
                scope_key,
                into_scope_key=body.into_scope_key,
                allowed_state_paths=body.allowed_state_paths,
                merged_by=principal.user_id or "unknown",
                allow_dream_states=body.allow_dream_states,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except TimelineBranchConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except TimelineBranchError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/projects/{project_id}/timeline-branches/{scope_key}/retire")
    def retire_timeline_branch(
        project_id: str,
        scope_key: str,
        body: TimelineBranchClose,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, project_id, write=True)
        try:
            return container.timeline_branches.retire(project_id, scope_key, reason=body.reason)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except TimelineBranchConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except TimelineBranchError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post(
        "/internal/maintenance/timeline-branches",
        dependencies=[Depends(verify_api_key)],
    )
    def sweep_timeline_branches_endpoint(project_id: str, limit: int = 50):
        """Close orphaned branches (never referenced, idle) as ABANDONED."""

        return container.timeline_branches.sweep_orphans(project_id, limit=limit).as_dict()

    @app.get(
        "/internal/models/live-status",
        dependencies=[Depends(verify_api_key)],
    )
    def model_live_status():
        """The three live facts per model, kept deliberately separate.

        ``live_enabled`` is configuration permission (a credential exists and
        the operator opened the gate); ``lifecycle_status`` is registration
        state; only ``live_canary_status = VERIFIED_LIVE`` — one real
        generation completed and reconciled — is production validation. The
        summary counts them apart so "enabled" can never read as "proven":
        with zero VERIFIED_LIVE rows, this endpoint reports zero models as
        live-verified no matter how many are enabled.
        """

        from production_domain.models import ModelDefinition

        with container.database.session() as session:
            rows = list(
                session.scalars(
                    select(ModelDefinition).order_by(
                        ModelDefinition.provider, ModelDefinition.logical_name
                    )
                )
            )
            models = [
                {
                    "logical_name": row.logical_name,
                    "provider": row.provider,
                    "provider_model_id": row.provider_model_id,
                    "modality": row.modality,
                    "enabled": row.enabled,
                    "live_enabled": row.live_enabled,
                    "lifecycle_status": row.lifecycle_status,
                    "live_canary_status": row.live_canary_status,
                    "live_canary_detail": row.live_canary_detail,
                    "last_live_test_at": (
                        row.last_live_test_at.isoformat() if row.last_live_test_at else None
                    ),
                }
                for row in rows
            ]
        verified = [item for item in models if item["live_canary_status"] == "VERIFIED_LIVE"]
        return {
            "models": models,
            "summary": {
                "total": len(models),
                "live_enabled": sum(1 for item in models if item["live_enabled"]),
                "verified_live": len(verified),
                "verified_live_models": [item["logical_name"] for item in verified],
                "note": (
                    "live_enabled is configuration permission and lifecycle_status is "
                    "registration state; neither is production validation. Only "
                    "live_canary_status=VERIFIED_LIVE records a completed, reconciled "
                    "real generation."
                ),
            },
        }

    @app.get(
        "/internal/style-drift/{project_id}",
        dependencies=[Depends(verify_api_key)],
    )
    def style_drift_report(project_id: str, drift_threshold: float | None = None):
        """Aggregate cross-episode style drift over committed candidates.

        Monitoring only: reads the append-only candidate style evaluations the
        commit gate already writes and reports per-episode means, drift from
        the baseline episode, flags and the decline streak. Changes no gate.
        """

        if drift_threshold is None:
            return container.style_drift.series_report(project_id).as_dict()
        return container.style_drift.series_report(
            project_id, drift_threshold=drift_threshold
        ).as_dict()

    @app.post("/v1/episodes/{episode_id}/plan-frame-anchors")
    def plan_frame_anchors(
        episode_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        """Re-run the automatic per-pair frame strategy for an episode."""

        with container.database.session() as session:
            episode = session.get(Episode, episode_id)
            if not episode:
                raise HTTPException(404, "episode not found")
            auth.require_project(principal, episode.project_id, write=True)
        try:
            result = container.orchestrator.plan_frame_anchors(episode_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"episode_id": episode_id, "stage": result.stage, **result.detail}

    @app.get("/v1/shots/{shot_id}/decisions")
    def shot_decisions(
        shot_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise HTTPException(404, "shot not found")
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id)
            auth.require_project(principal, episode.project_id)
            return [
                {
                    "decision_type": record.decision_type,
                    "input_features": record.input_features,
                    "selected_action": record.selected_action,
                    "reason_codes": record.reason_codes,
                    "model_version": record.model_version,
                    "policy_version": record.policy_version,
                    "created_at": record.created_at,
                }
                for record in session.scalars(
                    select(DecisionRecord)
                    .where(DecisionRecord.shot_id == shot_id)
                    .order_by(DecisionRecord.created_at)
                )
            ]

    @app.get("/v1/shots/{shot_id}/cost")
    def shot_cost(
        shot_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise HTTPException(404, "shot not found")
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id)
            auth.require_project(principal, episode.project_id)
        try:
            return container.cost.shot_cost(shot_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/accounts", dependencies=[Depends(verify_api_key)])
    def create_account(body: AccountCreate):
        with container.database.session() as session:
            credential_id = None
            if body.credential:
                credential = ProviderCredential(
                    provider=body.provider, secret_ciphertext=container.credentials.encrypt(body.credential)
                )
                session.add(credential)
                session.flush()
                credential_id = credential.id
            account = ProviderAccount(
                provider=body.provider,
                account_identifier=body.account_identifier,
                tier=body.tier,
                credits=body.credits,
                image_capacity=body.image_capacity,
                video_capacity=body.video_capacity,
                supported_models=body.supported_models,
                credential_id=credential_id,
                metadata_json={"project_id": body.provider_project_id} if body.provider_project_id else {},
            )
            session.add(account)
            session.flush()
            return {
                "id": account.id,
                "provider": account.provider,
                "status": account.status,
                "credits": account.credits,
            }

    @app.post("/v1/projects/{project_id}/provider-bindings", dependencies=[Depends(verify_api_key)])
    def bind_provider_project(project_id: str, body: ProviderProjectBind):
        if body.provider == "google_flow":
            try:
                binding = container.flow_affinity.bind_existing(
                    local_project_id=project_id,
                    provider_account_id=body.provider_account_id,
                    provider_project_id=body.provider_project_id,
                )
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except (FlowAffinityConflict, ValueError) as exc:
                raise HTTPException(409, str(exc)) from exc
            return {
                "id": binding.id,
                "project_id": binding.local_project_id,
                "provider": binding.provider,
                "provider_account_id": binding.provider_account_id,
                "provider_project_id": binding.provider_project_id,
                "status": binding.status,
            }
        with container.database.session() as session:
            project = session.get(Project, project_id)
            account = session.get(ProviderAccount, body.provider_account_id)
            if not project or not account:
                raise HTTPException(404, "project or provider account not found")
            if account.provider != body.provider:
                raise HTTPException(409, "provider account type does not match binding provider")
            binding = session.scalar(
                select(ProviderProjectBinding).where(
                    ProviderProjectBinding.local_project_id == project.id,
                    ProviderProjectBinding.provider == body.provider,
                    ProviderProjectBinding.provider_account_id == account.id,
                )
            )
            if not binding:
                binding = ProviderProjectBinding(
                    local_project_id=project.id,
                    provider=body.provider,
                    provider_account_id=account.id,
                    provider_project_id=body.provider_project_id,
                )
                session.add(binding)
            else:
                binding.provider_project_id = body.provider_project_id
                binding.status = "READY"
            session.flush()
            return {
                "id": binding.id,
                "project_id": project.id,
                "provider": binding.provider,
                "provider_account_id": binding.provider_account_id,
                "provider_project_id": binding.provider_project_id,
                "status": binding.status,
            }

    @app.get("/v1/accounts", dependencies=[Depends(verify_api_key)])
    def list_accounts():
        with container.database.session() as session:
            return [
                {
                    "id": item.id,
                    "provider": item.provider,
                    "account_identifier": item.account_identifier,
                    "tier": item.tier,
                    "credits": item.credits,
                    "status": item.status,
                    "image_inflight": item.image_inflight,
                    "video_inflight": item.video_inflight,
                    "worker_id": item.worker_id,
                }
                for item in session.scalars(select(ProviderAccount).order_by(ProviderAccount.created_at))
            ]

    @app.post(
        "/internal/provider-media-bindings/{binding_id}/reconcile",
        dependencies=[Depends(verify_api_key)],
        response_model=ProviderMediaReconcileView,
    )
    async def reconcile_provider_media_binding(
        binding_id: str,
        body: ProviderMediaReconcileRequest,
    ) -> ProviderMediaReconcileView:
        with container.database.session() as session:
            binding = session.get(MediaProviderBinding, binding_id)
            if binding is None:
                raise HTTPException(404, "provider media binding not found")
            provider_name = binding.provider
        try:
            provider = container.providers.get(provider_name)
            result = await container.media.reconcile_provider_media(
                binding_id,
                provider,
                action=body.action,
                provider_media_id=body.provider_media_id,
                reason=body.reason,
                explicit_confirmation=body.explicit_confirmation,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ProviderMediaReconciliationConflict, ProviderMediaValidationFailed) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return ProviderMediaReconcileView(**asdict(result))

    def _asset_view(asset: MediaAsset, *, reused: bool) -> dict[str, Any]:
        return {
            "id": asset.id,
            "sha256": asset.sha256,
            "asset_type": asset.asset_type,
            "storage_key": asset.storage_key,
            "public_url": asset.public_url,
            # PENDING_VERIFICATION for a fresh direct upload: registered, not
            # yet usable by providers until the full-content check passes.
            "verification_status": asset.verification_status,
            "reused": reused,
        }

    def _aware_utc(value: datetime) -> datetime:
        """SQLite hands back naive datetimes; a deadline must still compare."""

        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _upload_lineage(shot_id: str | None, character_id: str | None) -> str:
        # `media_service.lineage_key` is the only definition of this. The route
        # used to carry its own, and dedupe worked only while the two happened
        # to order their associations the same way.
        return lineage_key(shot_id=shot_id, character_id=character_id)

    def _upload_scope(principal: AuthPrincipal, body: DirectUploadAuthorize) -> str | None:
        auth.require_project(principal, body.project_id, write=True)
        with container.database.session() as session:
            project = session.get(Project, body.project_id)
            if not project:
                raise HTTPException(404, "project not found")
            if body.character_id:
                character = session.get(Character, body.character_id)
                if not character or character.project_id != body.project_id:
                    raise HTTPException(404, "character not found in project")
            if body.shot_id:
                shot = session.get(Shot, body.shot_id)
                if not shot:
                    raise HTTPException(404, "shot not found")
                scene = session.get(Scene, shot.scene_id)
                episode = session.get(Episode, scene.episode_id)
                if episode.project_id != body.project_id:
                    raise HTTPException(409, "shot does not belong to project")
            return project.workspace_id

    @app.post("/v1/assets/uploads", status_code=201)
    def authorize_direct_upload(
        request: Request,
        body: DirectUploadAuthorize,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        """Authorize an upload the client sends straight to object storage.

        The response contains a presigned PUT. The bytes never traverse this
        service: it decides *whether* and *where*, storage does the transfer.
        """

        workspace_id = _upload_scope(principal, body)
        idempotency_key = request.headers.get("Idempotency-Key") or (
            f"direct-upload-{secrets.token_urlsafe(24)}"
        )
        lineage_key = _upload_lineage(body.shot_id, body.character_id)
        in_flight = None
        reservation_id: str | None = None
        # Only a hold *this* call created may be released by it; a replay keeps
        # the hold the original authorization already owns.
        held_reservation_id: str | None = None
        expired_upload = None
        authorized = None
        try:
            # The quota hold is taken on the *declared* size before the client
            # is allowed to transfer, so a workspace cannot exceed its capacity
            # by uploading first and being counted afterwards.
            duplicate = container.media.find_by_content(
                project_id=body.project_id,
                sha256=body.sha256.lower(),
                asset_type=body.asset_type.strip().upper(),
                lineage_key=lineage_key,
            )
            # A client that lost the response may legitimately ask again, and
            # re-authorizing is a replay of one upload rather than a second one.
            # The reservation must not be re-taken for it: WorkspaceStorageQuota
            # reads a RESERVED row as "already in progress", which is right for a
            # multipart upload passing through this process and wrong for one the
            # client holds its own presigned URL for.
            in_flight = container.direct_uploads.find_by_idempotency_key(
                project_id=body.project_id, idempotency_key=idempotency_key
            )
            if in_flight is not None:
                reservation_id = in_flight.storage_reservation_id
            elif workspace_id and duplicate is None:
                reservation = storage_quota.reserve(
                    workspace_id=workspace_id,
                    project_id=body.project_id,
                    byte_count=body.size_bytes,
                    idempotency_key=idempotency_key,
                )
                reservation_id = held_reservation_id = reservation.id
            authorized = container.direct_uploads.authorize(
                project_id=body.project_id,
                workspace_id=workspace_id,
                created_by_user_id=principal.user_id,
                asset_type=body.asset_type,
                filename=body.filename,
                mime_type=body.mime_type,
                sha256=body.sha256,
                size_bytes=body.size_bytes,
                idempotency_key=idempotency_key,
                lineage_key=lineage_key,
                shot_id=body.shot_id,
                character_id=body.character_id,
                reservation_id=reservation_id,
            )
        except DirectUploadExpired as exc:
            expired_upload = in_flight
            raise HTTPException(409, str(exc)) from exc
        except DirectUploadUnsupported as exc:
            raise HTTPException(501, str(exc)) from exc
        except DirectUploadConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except WorkspaceStorageQuotaExceeded as exc:
            raise HTTPException(413, str(exc)) from exc
        except StorageReservationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except StorageLimitExceeded as exc:
            raise HTTPException(413, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            # No presigned URL was returned, so the client cannot PUT and no
            # object can appear. A hold kept past that point is capacity no
            # upload will ever use — and while it stays RESERVED this
            # Idempotency-Key can never be retried either.
            if authorized is None and held_reservation_id:
                storage_quota.release(held_reservation_id)
            # An expired session is the one place a stale hold is observable,
            # so it is also the one place it can be reclaimed. Everything else
            # abandoned mid-flight still needs a sweeper.
            if expired_upload is not None:
                container.direct_uploads.abandon(expired_upload.id)
                if expired_upload.storage_reservation_id:
                    storage_quota.release(expired_upload.storage_reservation_id)
        return {
            "upload_id": authorized.upload_id,
            "url": authorized.presigned.url,
            "method": authorized.presigned.method,
            "headers": authorized.presigned.headers,
            "storage_key": authorized.presigned.storage_key,
            "expires_at": authorized.expires_at.isoformat(),
            # When set, this content is already held for the project: complete
            # immediately and skip the transfer.
            "existing_asset_id": authorized.existing_asset_id,
        }

    @app.post("/v1/assets/uploads/{upload_id}/complete")
    def complete_direct_upload(
        upload_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        """Adopt an object the client already put in storage.

        Size comes from the store's `HEAD` and the digest was enforced by the
        store on write, so neither is taken on the client's word. Validation
        reads a bounded header prefix, never the whole object.
        """

        try:
            upload = container.direct_uploads.pending(upload_id)
        except LookupError as exc:
            raise HTTPException(404, "upload not found") from exc
        auth.require_project(principal, upload.project_id, write=True)
        if upload.status == DirectUploadStatus.COMPLETED.value and upload.media_asset_id:
            asset = container.media.get(upload.media_asset_id)
            if asset is not None:
                return _asset_view(asset, reused=True)
        if upload.status != DirectUploadStatus.PENDING.value:
            raise HTTPException(409, "this upload can no longer be completed")

        adopted: MediaAsset | None = None
        reused = False
        # "Not there yet" is not a failed upload. Set while the presigned PUT is
        # still live, so a client that polls too early keeps its session.
        recoverable = False
        try:
            # Storage I/O stays outside the transaction: no row lock is held
            # across a network call.
            size_bytes, mime_type = container.direct_uploads.verify_object(upload)
            with container.database.session() as session:
                # Locking the upload row first is what makes the rest single
                # -owner. Two requests can both see the object and both try to
                # adopt it; only one may settle the hold.
                claim = container.direct_uploads.claim_completion(session, upload.id)
                if not claim.claimed:
                    winner = container.media.get(claim.media_asset_id) if claim.media_asset_id else None
                    if winner is not None:
                        return _asset_view(winner, reused=True)
                    raise HTTPException(409, "this upload can no longer be completed")
                adopted, reused = container.media.adopt_stored_object_in(
                    session,
                    upload.project_id,
                    upload.asset_type,
                    upload.storage_key,
                    sha256=upload.sha256,
                    mime_type=mime_type,
                    size_bytes=size_bytes,
                    shot_id=upload.shot_id,
                    character_id=upload.character_id,
                    metadata={"upload_id": upload.id},
                    # The value the authorization already decided and dedupli-
                    # cated against, not a second derivation of it.
                    lineage_key=upload.lineage_key,
                )
                container.direct_uploads.mark_completed(session, upload.id, media_asset_id=adopted.id)
                if claim.reservation_id:
                    # Same transaction as the asset row and the status change,
                    # so a crash between them cannot leave real storage
                    # unaccounted.
                    storage_quota.settle_in(
                        session,
                        claim.reservation_id,
                        asset_id=adopted.id,
                        storage_key=adopted.storage_key,
                        used_bytes=0 if reused else size_bytes,
                    )
        except DirectUploadNotFinished as exc:
            # The object is absent. While the presigned PUT is still live that
            # means the client has not finished, not that it failed: abandoning
            # the row here would kill a URL that still works and leave the bytes
            # it later writes with no row to adopt them.
            recoverable = _aware_utc(upload.expires_at) > utcnow()
            raise HTTPException(409, str(exc)) from exc
        except StorageLimitExceeded as exc:
            raise HTTPException(413, str(exc)) from exc
        except UnsafeMediaUpload as exc:
            raise HTTPException(415, str(exc)) from exc
        except StorageReservationConflict as exc:
            # The hold this completion meant to settle is no longer settleable —
            # another path moved it. The transaction rolled back, so nothing is
            # half-written; this is a conflict the client can see and retry, not
            # the 500 it used to be. The expiry sweep was the reachable cause and
            # no longer races here, but the settlement is fail-closed by design
            # and a conflict must stay an answer rather than a stack trace.
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            # A rejected object leaves no asset, so the hold must not survive as
            # unaccounted capacity. The row is abandoned whether or not it held
            # one — a workspace-less project still must not keep a PENDING row
            # that can never complete.
            if adopted is None and not recoverable:
                container.direct_uploads.abandon(upload.id)
                if upload.storage_reservation_id:
                    storage_quota.release(upload.storage_reservation_id)
        return _asset_view(adopted, reused=reused)

    @app.get(
        "/internal/models/router-evidence",
        dependencies=[Depends(verify_api_key)],
    )
    def router_evidence():
        """The four layers, side by side and still separate.

        Reports what each external layer holds, what production has observed,
        and — most usefully — where they disagree and what is missing. The
        layers are returned as four keys rather than one merged list, because
        merging them is the thing the whole structure exists to prevent.

        ``lcb`` answers the question an operator actually asks: is the
        conservative lower bound affecting routing right now, and if not, what
        is stopping it.
        """

        from router_evidence_core import (
            EvidenceLayer,
            EvidenceLayerStore,
            attach_community_effective_sizes,
            build_coverage,
            build_layer_priors,
            find_conflicts,
            prior_summary,
        )
        from router_evidence_core.calibration import BRIDGES

        store = EvidenceLayerStore()
        snapshots = store.snapshots()
        priors_by_layer = {
            snapshot.layer: build_layer_priors(snapshot) for snapshot in snapshots
        }
        coverage = build_coverage(snapshots)
        attach_community_effective_sizes(
            coverage, priors_by_layer.get(EvidenceLayer.COMMUNITY, [])
        )
        conflicts = find_conflicts(snapshots, priors_by_layer)
        observation_counts = container.router_observations.coverage_counts()
        for token, count in observation_counts.items():
            entry = coverage.models.get(token)
            if entry is not None:
                entry.production_observations = count
        latest_run = container.router_observations.latest_posterior_run_id()
        return {
            "layers": {
                snapshot.layer.value: {
                    "version": snapshot.version,
                    "frozen_at": snapshot.frozen_at,
                    "records": len(snapshot.records),
                    "prior_eligible_records": len(snapshot.eligible()),
                    "gaps": [item.model_dump() for item in snapshot.gaps],
                    "prior_summary": prior_summary(priors_by_layer[snapshot.layer]),
                }
                for snapshot in snapshots
            },
            "source_distribution": store.source_distribution(),
            "production": {
                "observations_by_version": observation_counts,
                "latest_posterior_run_id": latest_run,
            },
            "calibration_bridges": len(BRIDGES),
            "coverage": {
                token: {
                    "official_records": entry.official_records,
                    "benchmark_records": entry.benchmark_records,
                    "community_records": entry.community_records,
                    "official_eligible": entry.official_eligible,
                    "benchmark_eligible": entry.benchmark_eligible,
                    "community_effective_sample_size": entry.community_effective_sample_size,
                    "production_observations": entry.production_observations,
                    "scenarios_with_evidence": sorted(entry.scenarios_with_evidence),
                    "scenarios_with_eligible_evidence": sorted(entry.scenarios_with_eligible_evidence),
                    "scales": sorted(entry.scales),
                    "exclusion_reasons": entry.exclusion_reasons,
                    "insufficient": entry.insufficient,
                }
                for token, entry in sorted(coverage.models.items())
            },
            "insufficient_models": coverage.insufficient_models,
            "unconfirmed_versions": coverage.unconfirmed_versions,
            "conflicts": [
                {
                    "conflict_id": item.conflict_id,
                    "kind": item.kind,
                    "key": item.key.token,
                    "description": item.description,
                    "record_ids": list(item.record_ids),
                    "layers": list(item.layers),
                }
                for item in conflicts
            ],
            "lcb": {
                "settings_default": container.settings.feature_router_lcb,
                "flag_enabled": container.feature_flags.enabled("router_lcb"),
                "posterior_snapshot_available": latest_run is not None,
                "affecting_routing": bool(
                    container.feature_flags.enabled("router_lcb") and latest_run is not None
                ),
            },
            "exploration": {
                "online": False,
                "note": (
                    "the exploration policy exists as an offline simulator only; no service "
                    "imports it and no flag turns it on"
                ),
            },
        }

    @app.get(
        "/internal/models/external-evidence",
        dependencies=[Depends(verify_api_key)],
    )
    def external_evidence(logical_name: str | None = None):
        """What the public record says about the models this platform runs.

        Read-only, and deliberately reports the excluded evidence too. The
        question an operator actually has is not "what is the prior" but "why
        is this model's prior still a hand-authored number", and the answer is
        in the exclusions: a version mismatch, a weak source grade, or nothing
        published at all.
        """

        from external_evidence_core import ExternalEvidenceService

        service = ExternalEvidenceService.load()
        if logical_name is None:
            return {
                "registry_version": service.version,
                "frozen_at": service.registry.frozen_at,
                "prior_eligibility_rule": service.registry.prior_eligibility_rule,
                "external_prior_enabled": container.settings.feature_external_prior,
                "coverage": service.coverage(),
                "gaps": [item.model_dump() for item in service.registry.gaps],
                "conflicts": [item.model_dump() for item in service.registry.conflicts],
            }
        items = service.items_for(logical_name)
        if not items and logical_name not in service.unbacked_model_names():
            raise HTTPException(404, f"no external evidence entry for {logical_name}")
        return {
            "registry_version": service.version,
            "logical_name": logical_name,
            "metrics": [
                {
                    "evidence_id": item.evidence_id,
                    "benchmark": item.evidence.benchmark_name,
                    "model_version": item.evidence.model.version,
                    "model_revision": item.evidence.model.revision,
                    "operation": item.evidence.operation,
                    "metric_name": item.metric.metric_name,
                    "value": item.metric.value,
                    "metric_scale": item.metric.metric_scale_override or item.evidence.metric_scale,
                    "sample_size_prompts": item.evidence.sample_size_prompts,
                    "sample_size_runs": item.evidence.sample_size_runs,
                    "confidence_interval": item.evidence.confidence_interval,
                    "evaluator": item.evidence.evaluator,
                    "canonical_scene": item.metric.canonical_scene,
                    "canonical_capability": item.metric.canonical_capability,
                    "mapping_confidence": item.metric.mapping_confidence,
                    "source_grade": item.grade,
                    "sources": [
                        {
                            "source_id": s.source_id,
                            "title": s.title,
                            "url": s.url,
                            "snapshot_at": s.snapshot_at,
                            "dynamic": s.dynamic,
                        }
                        for s in item.sources
                    ],
                    "version_match": item.binding.version_match,
                    "prior_eligible": item.prior_eligible,
                    "ineligibility_reasons": list(item.ineligibility_reasons),
                    "limitations": item.evidence.limitations,
                }
                for item in items
            ],
        }

    @app.post(
        "/internal/models/reconcile-live",
        dependencies=[Depends(verify_api_key)],
    )
    def reconcile_live_models(apply: bool = False):
        """Re-derive every model's `live_enabled` from the credentials present now.

        Startup seeds the registry once and then deliberately never replays
        defaults over an administrator's changes, which is right — but it left
        no way at all to *open* a model after its credential arrived. Adding a
        key to `.env` and restarting did nothing, because reconciliation only
        ran for models this startup happened to create. That is the gap between
        "the credentials are in place" and "the platform will use them".

        Only `live_enabled` moves. `enabled` is a routing decision an operator
        owns, and the execution ID is never restated, so this cannot overwrite
        an administrator's chosen model.

        Reports by default. Pass `?apply=true` to write.
        """

        live_gate_ready = (
            container.settings.provider_mode == "live"
            and container.settings.allow_live_provider_calls is True
            and container.settings.live_provider_confirmation == LIVE_PROVIDER_CONFIRMATION
        )
        # Every (provider, provider_model_id) that carries a published rate. A
        # model with no row is refused in live mode rather than estimated, so
        # opening one is a promise the platform cannot keep: the router would
        # offer it and every generation would fail on PricingUnverified. Credit
        # for the transport is not credit for the price.
        with container.database.session() as session:
            priced_models = {
                (row.provider, row.provider_model_id)
                for row in session.execute(
                    select(
                        ModelPricingProfile.provider, ModelPricingProfile.provider_model_id
                    ).distinct()
                )
            }

        rows: list[dict[str, Any]] = []
        changed = 0
        for state in container.model_infrastructure.all_runtime_models():
            provider: object | None
            try:
                provider = container.providers.get(state.provider)
            except LookupError:
                # The generation router holds image/video adapters only. A
                # chat-only or embedding-only provider lives in the capability
                # catalogue instead, and is no less configured for it.
                provider = container.provider_capabilities.implementation(state.provider)
            if provider is None:
                transport, reason = False, "provider has no adapter in this deployment"
            else:
                if isinstance(provider, NotConfiguredProvider):
                    # A stub answers every call with PROVIDER_NOT_CONFIGURED and
                    # carries no `configured` attribute, so a getattr default of
                    # True would mark it live and let the router pick something
                    # that cannot dispatch.
                    transport, reason = False, "provider is a reserved stub with no transport"
                else:
                    transport = bool(getattr(provider, "configured", True))
                    reason = "" if transport else "provider credential or model ID is not configured"
            priced = (state.provider, state.provider_model_id) in priced_models
            if transport and not priced:
                reason = "no pricing profile; live mode refuses an unpriced model"
            target = bool(state.enabled and transport and priced and live_gate_ready)
            if not target and not reason:
                reason = "" if state.enabled else "model is disabled"
                if live_gate_ready is False:
                    reason = "live gate is closed"
            moved = target != state.live_enabled
            if moved and apply:
                container.model_infrastructure.set_enablement(
                    state.logical_name, enabled=state.enabled, live_enabled=target
                )
                changed += 1
            rows.append(
                {
                    "logical_name": state.logical_name,
                    "provider": state.provider,
                    "enabled": state.enabled,
                    "live_enabled": target if apply else state.live_enabled,
                    "would_change": moved,
                    "blocked_by": reason or None,
                }
            )
        return {
            "applied": apply,
            "live_gate_ready": live_gate_ready,
            "changed": changed if apply else sum(1 for row in rows if row["would_change"]),
            "models": rows,
        }

    @app.post(
        "/internal/maintenance/expired-uploads",
        dependencies=[Depends(verify_api_key)],
    )
    def sweep_expired_uploads_endpoint(limit: int = DEFAULT_SWEEP_LIMIT):
        """Reclaim uploads whose authorized window closed without completing.

        The operator-triggered face of the same sweep the worker runs on a
        schedule (`EXPIRED_UPLOAD_SWEEP_INTERVAL_SECONDS`). Both call one
        implementation, so an out-of-band reclaim and the periodic one cannot
        drift apart — and because each upload is claimed under its own row lock,
        running both at once is safe.
        """

        return sweep_expired_uploads(
            database=container.database,
            uploads=container.direct_uploads,
            quota=storage_quota,
            limit=limit,
            reservation_stale_after_seconds=(container.settings.storage_reservation_stale_after_seconds),
        ).as_response()

    @app.post(
        "/internal/maintenance/generation-staging",
        dependencies=[Depends(verify_api_key)],
    )
    def sweep_generation_staging_endpoint(limit: int | None = None):
        """Reclaim staged generation output nothing can ever adopt again.

        The operator-triggered face of the sweep the worker runs on
        `GENERATION_STAGING_SWEEP_INTERVAL_SECONDS`. A staged object is deleted
        only when it is past the TTL, its job is terminal or unknown, and no
        media row adopted its key — so running this beside the worker, or twice
        at once, deletes nothing a completion could still need.
        """

        return sweep_generation_staging(
            database=container.database,
            storage=container.media.storage,
            ttl_seconds=container.settings.generation_staging_ttl_seconds,
            limit=max(1, limit or container.settings.generation_staging_sweep_limit),
        ).as_response()

    @app.post(
        "/internal/maintenance/media-verification",
        dependencies=[Depends(verify_api_key)],
    )
    def verify_media_endpoint(limit: int | None = None):
        """The operator-triggered face of the sweep the worker runs on
        `MEDIA_VERIFICATION_INTERVAL_SECONDS`: full decode plus SHA re-check
        of every PENDING_VERIFICATION direct upload, crash-recoverable via
        the VERIFYING lease.
        """

        from media_service import verify_pending_assets

        return verify_pending_assets(
            database=container.database,
            storage=container.storage,
            limit=max(1, limit or container.settings.media_verification_limit),
            lease_seconds=container.settings.media_verification_lease_seconds,
        ).as_response()

    @app.post(
        "/internal/maintenance/reclaim-rejected-media",
        dependencies=[Depends(verify_api_key)],
    )
    def reclaim_rejected_media_endpoint(
        asset_id: str | None = None,
        min_age_seconds: int | None = None,
        limit: int | None = None,
    ):
        """Delete rejected upload bytes and hand their quota back, in that order.

        Verification keeps an INVALID/QUARANTINED object charged because the
        bytes are still stored as evidence. This is the other half: an
        explicit operator action that removes the object and only then
        releases the reservation, so a workspace is not charged forever for
        files it can never use — and un-charging can never outrun deletion.
        Objects shared with another asset or a live rendition are kept.
        """

        from media_service import reclaim_rejected_assets

        return reclaim_rejected_assets(
            database=container.database,
            storage=container.storage,
            quota=WorkspaceStorageQuota(container.database),
            asset_ids=[asset_id] if asset_id else None,
            min_age_seconds=(
                min_age_seconds if min_age_seconds is not None else 7 * 24 * 3600
            ),
            limit=max(1, limit or 50),
        ).as_response()

    @app.post(
        "/internal/maintenance/rendition-gc",
        dependencies=[Depends(verify_api_key)],
    )
    def sweep_rendition_gc_endpoint(limit: int | None = None):
        """The operator-triggered face of the sweep the worker runs on
        `RENDITION_GC_INTERVAL_SECONDS`. Claim/lease per row, so running it
        beside the worker double-deletes nothing; originals are never
        eligible; deleted rows stay as reconcilable tombstones.
        """

        from media_service import active_reference_profiles, sweep_rendition_gc

        return sweep_rendition_gc(
            database=container.database,
            storage=container.storage,
            active_constraint_profiles=active_reference_profiles(container.providers._providers),
            min_idle_seconds=container.settings.rendition_gc_min_idle_seconds,
            lease_seconds=container.settings.rendition_gc_lease_seconds,
            limit=max(1, limit or container.settings.rendition_gc_limit),
        ).as_response()

    @app.post(
        "/internal/maintenance/character-evidence",
        dependencies=[Depends(verify_api_key)],
    )
    def sweep_character_evidence_endpoint(limit: int | None = None):
        """The operator-triggered face of the shadow Character Evidence sweep.

        Same implementation the worker runs on
        `CHARACTER_EVIDENCE_SWEEP_INTERVAL_SECONDS`: enqueue candidates with
        registered video output, dispatch PENDING submissions, and move
        silent acceptances to RECONCILIATION_REQUIRED. Shadow-only.
        """

        return container.character_evidence_tracker.sweep(
            limit=max(1, limit or container.settings.character_evidence_sweep_limit)
        ).as_dict()

    @app.get(
        "/internal/character-evidence/submissions",
        dependencies=[Depends(verify_api_key)],
    )
    def list_character_evidence_submissions(status: str | None = None, limit: int = 100):
        return {
            "submissions": container.character_evidence_tracker.list_submissions(
                status=status, limit=limit
            )
        }

    @app.post(
        "/internal/character-evidence/submissions/{submission_id}/reconcile",
        dependencies=[Depends(verify_api_key)],
    )
    def reconcile_character_evidence_submission(
        submission_id: str,
        body: CharacterEvidenceReconcile,
    ):
        try:
            resolved = container.character_evidence_tracker.resolve_reconciliation(
                submission_id,
                action=body.action,
                note=body.note,
                resolved_by=body.resolved_by,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "id": resolved.id,
            "candidate_id": resolved.candidate_id,
            "status": resolved.status,
            "reconciled_by": resolved.reconciled_by,
        }

    @app.post("/v1/assets")
    async def upload_asset(
        request: Request,
        principal: AuthPrincipal = Depends(auth.current_user),
        project_id: str = Form(...),
        asset_type: str = Form(...),
        file: UploadFile = File(...),
        shot_id: str | None = Form(default=None),
        character_id: str | None = Form(default=None),
    ):
        asset_type = asset_type.strip().upper()
        auth.require_project(principal, project_id, write=True)
        workspace_id: str | None = None
        with container.database.session() as session:
            project = session.get(Project, project_id)
            if not project:
                raise HTTPException(404, "project not found")
            workspace_id = project.workspace_id
            if character_id:
                character = session.get(Character, character_id)
                if not character or character.project_id != project_id:
                    raise HTTPException(404, "character not found in project")
            if shot_id:
                shot = session.get(Shot, shot_id)
                if not shot:
                    raise HTTPException(404, "shot not found")
                scene = session.get(Scene, shot.scene_id)
                episode = session.get(Episode, scene.episode_id)
                if episode.project_id != project_id:
                    raise HTTPException(409, "shot does not belong to project")
        reservation_id: str | None = None
        asset: MediaAsset | None = None
        reused = False
        try:
            validated = validate_user_media_upload(
                file.file,
                filename=file.filename or "asset.bin",
                declared_mime=file.content_type,
                asset_type=asset_type,
                max_bytes=getattr(
                    container.storage,
                    "max_object_bytes",
                    container.settings.max_upload_bytes,
                ),
                max_image_pixels=container.settings.max_image_pixels,
            )
            file.file.seek(0, 2)
            upload_bytes = file.file.tell()
            file.file.seek(0)
            digest = hashlib.sha256()
            while chunk := file.file.read(1024 * 1024):
                digest.update(chunk)
            upload_digest = digest.hexdigest()
            file.file.seek(0)
            idempotency_key = request.headers.get("Idempotency-Key") or (
                f"upload-{secrets.token_urlsafe(24)}"
            )
            lineage_parts = []
            if shot_id:
                lineage_parts.append(f"shot:{shot_id}")
            if character_id:
                lineage_parts.append(f"character:{character_id}")
            lineage_key = "|".join(lineage_parts) or "shared"
            with container.database.session() as session:
                asset = session.scalar(
                    select(MediaAsset).where(
                        MediaAsset.project_id == project_id,
                        MediaAsset.sha256 == upload_digest,
                        MediaAsset.asset_type == asset_type,
                        MediaAsset.lineage_key == lineage_key,
                    )
                )
            if asset is not None:
                if workspace_id:
                    storage_quota.record_deduplicated(
                        workspace_id=workspace_id,
                        project_id=project_id,
                        byte_count=upload_bytes,
                        idempotency_key=idempotency_key,
                        asset_id=asset.id,
                        storage_key=asset.storage_key,
                    )
                reused = True
                return {
                    "id": asset.id,
                    "sha256": asset.sha256,
                    "asset_type": asset.asset_type,
                    "storage_key": asset.storage_key,
                    "public_url": asset.public_url,
                    "reused": reused,
                }
            if workspace_id:
                reservation = storage_quota.reserve(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    byte_count=upload_bytes,
                    idempotency_key=idempotency_key,
                )
                reservation_id = reservation.id
                if reservation.replayed:
                    asset = container.media.get(reservation.asset_id or "")
                    if (
                        asset is None
                        or asset.project_id != project_id
                        or asset.asset_type != asset_type
                        or asset.sha256 != upload_digest
                    ):
                        raise StorageReservationConflict(
                            "Idempotency-Key was already used for a different upload"
                        )
                    reused = True
                    return {
                        "id": asset.id,
                        "sha256": asset.sha256,
                        "asset_type": asset.asset_type,
                        "storage_key": asset.storage_key,
                        "public_url": asset.public_url,
                        "reused": reused,
                    }
            asset, reused = container.media.register(
                project_id,
                asset_type,
                file.file,
                filename=file.filename or "asset.bin",
                mime_type=validated.mime_type,
                shot_id=shot_id,
                character_id=character_id,
            )
            if reservation_id:
                storage_quota.settle(
                    reservation_id,
                    asset_id=asset.id,
                    storage_key=asset.storage_key,
                    used_bytes=0 if reused else upload_bytes,
                )
        except WorkspaceStorageQuotaExceeded as exc:
            raise HTTPException(413, str(exc)) from exc
        except StorageReservationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except StorageLimitExceeded as exc:
            raise HTTPException(413, str(exc)) from exc
        except UnsafeMediaUpload as exc:
            raise HTTPException(415, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            # Release only while no MediaAsset is known to have committed. Once
            # registration returned, a failed settle keeps the hold RESERVED so
            # storage can never become unaccounted capacity.
            if reservation_id and asset is None:
                storage_quota.release(reservation_id)
        assert asset is not None
        return {
            "id": asset.id,
            "sha256": asset.sha256,
            "asset_type": asset.asset_type,
            "storage_key": asset.storage_key,
            "public_url": asset.public_url,
            "reused": reused,
        }

    @app.get("/v1/assets/{asset_id}")
    def get_asset(
        asset_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        asset = container.media.get(asset_id)
        if not asset:
            raise HTTPException(404, "asset not found")
        auth.require_project(principal, asset.project_id)
        return {
            "id": asset.id,
            "project_id": asset.project_id,
            "asset_type": asset.asset_type,
            "sha256": asset.sha256,
            "storage_key": asset.storage_key,
            "mime_type": asset.mime_type,
            "width": asset.width,
            "height": asset.height,
            "duration": asset.duration,
            "provider": asset.provider,
            "provider_media_id": asset.provider_media_id,
            "public_url": asset.public_url,
        }

    @app.get("/v1/assets/{asset_id}/thumbnail")
    def get_asset_thumbnail(
        asset_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        """A small JPEG for galleries — derived lazily, cached as a rendition.

        The Web UI reads this instead of the original, so a gallery of 4K
        plates downloads thumbnails rather than 4K plates.
        """

        asset = container.media.get(asset_id)
        if not asset:
            raise HTTPException(404, "asset not found")
        auth.require_project(principal, asset.project_id)
        try:
            thumbnail = container.thumbnails.ensure_thumbnail(asset_id)
        except ThumbnailUnavailable as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            path = container.storage.path_for(thumbnail.storage_key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not path.is_file():
            raise HTTPException(404, "thumbnail object not found")
        return FileResponse(
            path,
            media_type=thumbnail.mime_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
                "Cache-Control": "private, max-age=3600",
            },
        )

    @app.get("/v1/media/reference/{storage_key:path}")
    def serve_signed_reference(storage_key: str, request: Request):
        """A signed, expiring, unauthenticated object read for external fetchers.

        This route exists only because local disk cannot issue an
        object-storage URL, and an external provider cannot present a session
        cookie. **It proxies bytes through this process**, which is what
        presigned object-storage URLs exist to avoid — so it stays off unless an
        operator sets `LOCAL_REFERENCE_SIGNING_KEY`, and production configures
        S3-compatible storage instead. The signature is over the exact key and
        expiry, so it grants one object for one bounded window and nothing else.
        """

        signing_key = container.settings.local_reference_signing_key
        if not signing_key:
            raise HTTPException(404, "signed media references are not enabled")
        if not verify_local_reference_signature(
            storage_key,
            expires=request.query_params.get("expires", ""),
            signature=request.query_params.get("signature", ""),
            signing_key=signing_key,
        ):
            raise HTTPException(403, "invalid or expired media reference signature")
        with container.database.session() as session:
            asset = session.scalar(select(MediaAsset).where(MediaAsset.storage_key == storage_key))
            rendition = session.scalar(
                select(MediaRendition).where(MediaRendition.storage_key == storage_key)
            )
        if asset is None and rendition is None:
            raise HTTPException(404, "stored object not found")
        media_type = (
            rendition.mime_type if rendition is not None else asset.mime_type if asset else None
        ) or "application/octet-stream"
        try:
            path = container.storage.path_for(storage_key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not path.is_file():
            raise HTTPException(404, "stored object not found")
        return FileResponse(path, media_type=media_type)

    @app.get("/v1/storage/{storage_key:path}")
    def serve_storage(
        storage_key: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        authorized_asset: MediaAsset | None = None
        with container.database.session() as session:
            assets = list(session.scalars(select(MediaAsset).where(MediaAsset.storage_key == storage_key)))
            if not assets:
                raise HTTPException(404, "stored object not found")
            authorized = principal.development_bypass
            for asset in assets:
                try:
                    auth.require_project(principal, asset.project_id)
                except HTTPException as exc:
                    if exc.status_code not in {403, 404}:
                        raise
                else:
                    authorized = True
                    authorized_asset = asset
                    break
            if not authorized:
                raise HTTPException(403, "你无权访问该素材")
        try:
            path = container.storage.path_for(storage_key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not path.is_file():
            raise HTTPException(404, "stored object not found")
        assert authorized_asset is not None
        media_type = (authorized_asset.mime_type or "application/octet-stream").lower()
        disposition = "inline" if media_type in SAFE_INLINE_MEDIA_TYPES else "attachment"
        return FileResponse(
            path,
            media_type=media_type,
            filename=Path(storage_key).name,
            content_disposition_type=disposition,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
            },
        )

    @app.post("/v1/generations", status_code=202)
    def create_generation(
        body: GenerationRequest,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, body.project_id, write=True)
        try:
            requested_role = body.metadata.get("model_role")
            admitted = container.generation_admission.admit_passenger(
                body,
                requested_role=requested_role if isinstance(requested_role, str) else None,
                enforce_plan=not principal.development_bypass,
            )
            mode = str(admitted.request.metadata.get("mode") or "PASSENGER_SEAT")
            job, replayed = container.visual_runtime.submit(
                admitted.request,
                mode=mode,
                prompt_version="user-authored-v1",
                estimated_credits=admitted.estimate.credits,
                pricing_version=container.credit_pricing.version,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except GenerationTargetError as exc:
            raise HTTPException(400, str(exc)) from exc
        except InsufficientWorkspaceCredits as exc:
            raise HTTPException(402, str(exc)) from exc
        except PlanEntitlementDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except WorkspaceCreditConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            **_job_view(job, credit_status=container.gateway.credit_status(job.id)),
            "replayed": replayed,
        }

    @app.get("/v1/generations/{job_id}")
    def get_generation(
        job_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        job = container.gateway.get(job_id)
        if not job:
            raise HTTPException(404, "generation not found")
        auth.require_project(principal, job.project_id)
        return {
            **_job_view(job, credit_status=container.gateway.credit_status(job.id)),
            "events": [
                {"type": event.event_type, "detail": event.detail, "created_at": event.created_at}
                for event in container.gateway.events(job_id)
            ],
        }

    @app.post("/v1/generations/{job_id}/retry")
    def retry_generation(
        job_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        job = container.gateway.get(job_id)
        if not job:
            raise HTTPException(404, "generation not found")
        auth.require_project(principal, job.project_id, write=True)
        try:
            retried = container.gateway.retry(job_id)
            return _job_view(
                retried,
                credit_status=container.gateway.credit_status(retried.id),
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except UnsafeRetry as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/generations/{job_id}/cancel")
    async def cancel_generation(
        job_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        job = container.gateway.get(job_id)
        if not job:
            raise HTTPException(404, "generation not found")
        auth.require_project(principal, job.project_id, write=True)
        try:
            cancelled = await container.gateway.cancel(job_id)
            if cancelled.status != "CANCELLED" and cancelled.submission_state == "SENT_UNCONFIRMED":
                raise HTTPException(
                    409,
                    "provider submission is unconfirmed; credits are frozen pending reconciliation",
                )
            return _job_view(
                cancelled,
                credit_status=container.gateway.credit_status(cancelled.id),
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/generations/{job_id}/reconcile")
    def reconcile_generation(
        job_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        job = container.gateway.get(job_id)
        if not job:
            raise HTTPException(404, "generation not found")
        auth.require_project(principal, job.project_id, write=True)
        try:
            reconciled = container.gateway.reconcile(job_id)
            return _job_view(
                reconciled,
                credit_status=container.gateway.credit_status(reconciled.id),
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/v1/providers")
    async def list_providers(_principal: AuthPrincipal = Depends(auth.current_user)):
        result = []
        for name in container.providers.list():
            if not container.providers.is_configured(name) or not container.model_registry.provider_enabled(
                name
            ):
                continue
            profiles = container.capabilities.by_provider(name)
            with container.database.session() as session:
                visible_model_ids = set(
                    session.scalars(
                        select(ModelDefinition.id).where(
                            ModelDefinition.provider == name,
                            ModelDefinition.enabled.is_(True),
                            ModelDefinition.user_visible.is_(True),
                        )
                    )
                )
            profiles = [profile for profile in profiles if profile.model_definition_id in visible_model_ids]
            if not profiles:
                continue
            result.append(
                {
                    "name": name,
                    "models": [
                        {
                            "model_id": profile.model_id,
                            "version": profile.version,
                            "status": profile.status,
                            "confidence_level": profile.confidence_level,
                            "modality": profile.modality,
                            "supported_operations": profile.supported_operations,
                            "supports_reference_image": profile.supports_reference_image,
                            "max_reference_images": profile.max_reference_images,
                            "source": profile.source,
                            "cost": profile.cost,
                            "latency": profile.latency,
                        }
                        for profile in profiles
                    ],
                }
            )
        return result

    @app.post("/internal/router/video", dependencies=[Depends(verify_api_key)])
    def route_video_model(body: ShotRequirements):
        """Explainable internal ranking; it does not submit a generation request."""

        excluded = {
            profile.key
            for profile in container.model_registry.all()
            if not container.providers.is_configured(profile.provider)
        }
        return container.video_router.rank(body, excluded_models=excluded).model_dump()

    @app.get("/v1/skills")
    def list_skills(_principal: AuthPrincipal = Depends(auth.current_user)):
        return [
            {
                "name": skill.name,
                "category": skill.category,
                "description": skill.description,
                "version": skill.version,
            }
            for skill in container.skills.list_skills()
        ]

    @app.get("/v1/providers/{provider}/health")
    async def provider_health(
        provider: str,
        _principal: AuthPrincipal = Depends(auth.current_user),
    ):
        try:
            result = await container.providers.get(provider).health()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": result.ok, "detail": result.detail}

    @app.get("/v1/workers", dependencies=[Depends(verify_api_key)])
    def list_workers():
        container.runtime.expire_stale_workers()
        with container.database.session() as session:
            return [
                {
                    "id": item.id,
                    "provider": item.provider,
                    "account_id": item.account_id,
                    "status": item.status,
                    "capabilities": item.capabilities,
                    "credits": item.credits,
                    "current_jobs": item.current_jobs,
                    "max_jobs": item.max_jobs,
                    "last_heartbeat": item.last_heartbeat,
                }
                for item in session.scalars(select(BrowserWorker).order_by(BrowserWorker.id))
            ]

    @app.post("/internal/workers/credentials", dependencies=[Depends(verify_api_key)], status_code=201)
    def issue_worker_credential(body: WorkerCredentialIssue, response: Response):
        try:
            issued = worker_credentials.issue(
                worker_id=body.worker_id,
                provider=body.provider,
                account_id=body.account_id,
                ttl_seconds=body.expires_in_seconds,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        response.headers["Cache-Control"] = "no-store"
        return {
            "id": issued.id,
            "worker_id": body.worker_id,
            "provider": body.provider,
            "account_id": body.account_id,
            "access_token": issued.token,
            "token_type": "Bearer",
            "expires_at": issued.expires_at,
        }

    @app.post(
        "/internal/workers/credentials/{credential_id}/revoke",
        dependencies=[Depends(verify_api_key)],
    )
    def revoke_worker_credential(credential_id: str):
        if not worker_credentials.revoke(credential_id):
            raise HTTPException(404, "worker credential not found")
        return {"ok": True}

    @app.post("/v1/workers/register")
    def register_worker(
        body: WorkerRegister,
        principal: WorkerPrincipal = Depends(require_worker_credential),
    ):
        require_worker_binding(
            principal,
            worker_id=body.worker_id,
            provider=body.provider,
            account_id=body.account_id,
        )
        if body.account_id is None:
            raise HTTPException(400, "worker registration requires its bound account")
        with container.database.session() as session:
            account = session.get(ProviderAccount, body.account_id)
            if not account or account.provider != body.provider:
                raise HTTPException(400, "worker account is invalid for provider")
        worker = container.runtime.register(
            body.worker_id,
            body.provider,
            account_id=body.account_id,
            capabilities=body.capabilities,
            max_jobs=body.max_jobs,
            connection_id=body.connection_id,
            metadata=body.metadata,
        )
        return {"worker_id": worker.id, "connection_id": worker.connection_id, "status": worker.status}

    @app.post("/v1/workers/{worker_id}/heartbeat")
    def heartbeat(
        worker_id: str,
        body: WorkerHeartbeat,
        principal: WorkerPrincipal = Depends(require_worker_credential),
    ):
        require_worker_binding(principal, worker_id=worker_id)
        try:
            worker = container.runtime.heartbeat(
                worker_id,
                body.connection_id,
                status=body.status,
                credits=body.credits,
                current_jobs=body.current_jobs,
                metadata=body.metadata,
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "status": worker.status}

    @app.get("/v1/workers/{worker_id}/commands")
    def poll_commands(
        worker_id: str,
        connection_id: str,
        principal: WorkerPrincipal = Depends(require_worker_credential),
    ):
        require_worker_binding(principal, worker_id=worker_id)
        try:
            commands = container.runtime.claim_commands(worker_id, connection_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "commands": [{"id": cmd.id, "type": cmd.message_type, "payload": cmd.payload} for cmd in commands]
        }

    @app.post("/v1/workers/{worker_id}/responses")
    def command_response(
        worker_id: str,
        body: WorkerResponse,
        principal: WorkerPrincipal = Depends(require_worker_credential),
    ):
        require_worker_binding(principal, worker_id=worker_id)
        try:
            command = container.runtime.complete_command(
                worker_id, body.connection_id, body.command_id, response=body.response, error=body.error
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "status": command.status}

    @app.post("/v1/workers/{worker_id}/socket-ticket", status_code=201)
    def issue_worker_socket_ticket(
        worker_id: str,
        response: Response,
        principal: WorkerPrincipal = Depends(require_worker_credential),
    ):
        require_worker_binding(principal, worker_id=worker_id)
        try:
            issued = worker_credentials.issue_socket_ticket(principal)
        except WorkerAuthenticationError as exc:
            raise HTTPException(401, str(exc)) from exc
        ticket_protocol = f"worker-ticket.{issued.token}"
        response.headers["Cache-Control"] = "no-store"
        return {
            "ticket": issued.token,
            "expires_at": issued.expires_at,
            "websocket_protocols": ["ai-director.worker.v1", ticket_protocol],
        }

    @app.websocket("/v1/workers/ws/{worker_id}")
    async def worker_socket(websocket: WebSocket, worker_id: str):
        authorization = websocket.headers.get("authorization")
        offered_protocols = [
            item.strip()
            for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if item.strip()
        ]
        ticket_protocol = next(
            (item for item in offered_protocols if item.startswith("worker-ticket.")),
            None,
        )
        try:
            if authorization:
                principal = worker_credentials.authenticate_authorization(authorization)
            elif ticket_protocol:
                principal = worker_credentials.consume_socket_ticket(
                    ticket_protocol.removeprefix("worker-ticket."),
                    worker_id=worker_id,
                )
            else:
                raise WorkerAuthenticationError("worker credential or WebSocket ticket is required")
            require_worker_binding(principal, worker_id=worker_id)
        except (WorkerAuthenticationError, HTTPException):
            await websocket.close(code=4401)
            return
        accepted_protocol = "ai-director.worker.v1" if "ai-director.worker.v1" in offered_protocols else None
        await websocket.accept(subprotocol=accepted_protocol)
        connection_id = ""
        try:
            first = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            if first.get("type") != "worker.register":
                await websocket.close(code=4400)
                return
            payload = WorkerRegister.model_validate({**first.get("payload", {}), "worker_id": worker_id})
            try:
                require_worker_binding(
                    principal,
                    worker_id=worker_id,
                    provider=payload.provider,
                    account_id=payload.account_id,
                )
            except HTTPException:
                await websocket.close(code=4403)
                return
            if payload.account_id is None:
                await websocket.close(code=4403)
                return
            worker = container.runtime.register(
                worker_id,
                payload.provider,
                account_id=payload.account_id,
                capabilities=payload.capabilities,
                max_jobs=payload.max_jobs,
                connection_id=payload.connection_id,
                metadata=payload.metadata,
            )
            connection_id = worker.connection_id
            await websocket.send_json({"type": "worker.registered", "connection_id": connection_id})
            while True:
                try:
                    principal = worker_credentials.validate_principal(principal)
                except WorkerAuthenticationError:
                    await websocket.close(code=4401)
                    break
                for command in container.runtime.claim_commands(worker_id, connection_id):
                    await websocket.send_json(
                        {"id": command.id, "type": command.message_type, "payload": command.payload}
                    )
                try:
                    message = await asyncio.wait_for(websocket.receive_json(), timeout=1)
                except TimeoutError:
                    container.runtime.heartbeat(worker_id, connection_id)
                    continue
                try:
                    principal = worker_credentials.validate_principal(principal)
                except WorkerAuthenticationError:
                    await websocket.close(code=4401)
                    break
                if message.get("type") == "worker.heartbeat":
                    container.runtime.heartbeat(
                        worker_id,
                        connection_id,
                        status=message.get("status", WorkerStatus.READY.value),
                        credits=message.get("credits"),
                        current_jobs=message.get("current_jobs"),
                        metadata=message.get("metadata"),
                    )
                elif message.get("type") in {"provider.response", "provider.error"}:
                    container.runtime.complete_command(
                        worker_id,
                        connection_id,
                        message["id"],
                        response=message.get("response"),
                        error=message.get("error"),
                    )
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if connection_id:
                container.runtime.mark_offline(worker_id, connection_id)

    @app.post("/v1/images/generations", status_code=202)
    def openai_image_adapter(
        request: Request,
        body: dict[str, Any],
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, body.get("project_id", ""), write=True)
        key = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
        if not key:
            raise HTTPException(400, "Idempotency-Key is required")
        # The default image target is the server-owned IMAGE_GENERATION role, not
        # a constant in this handler: the registry is the single place where the
        # project's image model is chosen.
        requested_provider, requested_model = body.get("provider"), body.get("model")
        if requested_provider and requested_model:
            provider_name, model_name = str(requested_provider), str(requested_model)
        else:
            try:
                resolved_image = container.model_infrastructure.resolve_role(ModelRole.IMAGE_GENERATION)
            except LookupError as exc:
                raise HTTPException(503, f"no image model is available: {exc}") from exc
            provider_name = str(requested_provider or resolved_image.provider)
            model_name = str(requested_model or resolved_image.provider_model_id)
        generation = GenerationRequest(
            project_id=body["project_id"],
            type="image",
            provider=provider_name,
            model=model_name,
            prompt=body["prompt"],
            aspect_ratio=body.get("aspect_ratio", "1:1"),
            reference_asset_ids=body.get("reference_asset_ids", []),
            # Opt-in batch. Every extra image is generated and billed, so the
            # whole batch is priced and reserved before the call, and each image
            # comes back as its own selectable candidate.
            image_count=int(body.get("n") or body.get("image_count") or 1),
            idempotency_key=key,
        )
        try:
            admitted = container.generation_admission.admit_passenger(
                generation,
                enforce_plan=not principal.development_bypass,
            )
            job, replayed = container.visual_runtime.submit(
                admitted.request,
                mode="PASSENGER_SEAT",
                prompt_version="openai-image-adapter-v1",
                estimated_credits=admitted.estimate.credits,
                pricing_version=container.credit_pricing.version,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except GenerationTargetError as exc:
            raise HTTPException(400, str(exc)) from exc
        except InsufficientWorkspaceCredits as exc:
            raise HTTPException(402, str(exc)) from exc
        except PlanEntitlementDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except WorkspaceCreditConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "id": job.id,
            "object": "image.generation",
            "status": job.status,
            "replayed": replayed,
            "image_count": generation.image_count,
            "estimated_credits": admitted.estimate.credits,
        }

    @app.post("/v1/videos/generations", status_code=202)
    def openai_video_adapter(
        request: Request,
        body: dict[str, Any],
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, body.get("project_id", ""), write=True)
        key = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
        if not key:
            raise HTTPException(400, "Idempotency-Key is required")
        generation = GenerationRequest(
            project_id=body["project_id"],
            type="video",
            provider=body.get("provider", "google_flow"),
            model=body.get("model", "flow-veo-3.1"),
            prompt=body["prompt"],
            duration=body.get("duration", 8),
            aspect_ratio=body.get("aspect_ratio", "9:16"),
            start_frame_asset_id=body.get("start_frame_asset_id"),
            end_frame_asset_id=body.get("end_frame_asset_id"),
            reference_asset_ids=body.get("reference_asset_ids", []),
            idempotency_key=key,
        )
        try:
            admitted = container.generation_admission.admit_passenger(
                generation,
                enforce_plan=not principal.development_bypass,
            )
            job, replayed = container.visual_runtime.submit(
                admitted.request,
                mode="PASSENGER_SEAT",
                prompt_version="openai-video-adapter-v1",
                estimated_credits=admitted.estimate.credits,
                pricing_version=container.credit_pricing.version,
            )
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except GenerationTargetError as exc:
            raise HTTPException(400, str(exc)) from exc
        except InsufficientWorkspaceCredits as exc:
            raise HTTPException(402, str(exc)) from exc
        except PlanEntitlementDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except WorkspaceCreditConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": job.id, "object": "video.generation", "status": job.status, "replayed": replayed}

    auth.register_routes(app, verify_api_key)
    register_payment_routes(app, container, auth)
    register_runtime_routes(app, container, verify_api_key, auth)
    register_admin_routes(app, container, auth, verify_api_key)
    register_creative_routes(
        app,
        container,
        auth,
        creative=container.creative_director,
        continuations=container.episode_continuations,
    )
    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("video_platform_api.main:app", host="0.0.0.0", port=8080)
