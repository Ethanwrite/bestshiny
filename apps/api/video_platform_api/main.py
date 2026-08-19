from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from continuity_core import ContinuityRiskVector
from director_production import CandidateNotCommittable
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from generation_gateway import IdempotencyConflict
from generation_gateway.gateway import UnsafeRetry
from platform_contracts import EpisodeCreate, GenerationRequest, ProjectCreate, SceneCreate, ShotCreate
from production_domain.models import (
    BrowserWorker,
    Character,
    CharacterIdentityVersion,
    CostRecord,
    DecisionRecord,
    Episode,
    GenerationCandidate,
    Project,
    PromptCompilation,
    ProviderAccount,
    ProviderCredential,
    ProviderProjectBinding,
    QAResult,
    Scene,
    Shot,
    TimelineState,
    User,
    WorkerStatus,
    Workspace,
)
from pydantic import BaseModel, Field
from sqlalchemy import select

from .container import Container, build_container


class AccountCreate(BaseModel):
    provider: str = "google_flow"
    account_identifier: str
    tier: str = "PRO"
    credits: int = Field(default=100, ge=0)
    image_capacity: int = Field(default=1, ge=0)
    video_capacity: int = Field(default=1, ge=0)
    supported_models: list[str] = Field(default_factory=lambda: ["veo"])
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


class CandidateGenerate(BaseModel):
    idempotency_key: str = Field(min_length=3, max_length=250)
    fallback_providers: list[str] = Field(default_factory=lambda: ["google_flow", "seedance", "veo_official"])
    character_ids: list[str] = Field(default_factory=list)
    reference_asset_ids: list[str] = Field(default_factory=list)
    estimated_cost: float = Field(default=0.0, ge=0)


class CandidateValidate(BaseModel):
    evidence: dict[str, Any] = Field(default_factory=dict)


class PromptRefine(BaseModel):
    project_id: str
    prompt: str = Field(min_length=1, max_length=30_000)


class ContinuityEvaluate(BaseModel):
    project_id: str
    risk: dict[str, float] = Field(default_factory=dict)


class ProviderProjectBind(BaseModel):
    provider: str
    provider_account_id: str
    provider_project_id: str = Field(min_length=1, max_length=500)


def _job_view(job) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "id": job.id,
        "status": job.status,
        "provider": job.provider,
        "model": job.model,
        "provider_job_id": job.provider_job_id,
        "output_asset_id": job.output_asset_id,
        "safe_to_retry": job.safe_to_retry,
        "attempt_count": job.attempt_count,
        "error_code": job.error_code,
        "error_message": job.error_message,
    }


def _candidate_view(candidate, qa=None, costs: list | None = None) -> dict[str, Any]:  # type: ignore[no-untyped-def]
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
                "decision": qa.decision,
                "overall_score": qa.overall_score,
                "character_score": qa.character_score,
                "camera_score": qa.camera_score,
                "action_score": qa.action_score,
                "summary": qa.summary,
                "hard_failures": qa.hard_failures,
            }
            if qa
            else None
        ),
        "cost": round(sum(item.actual_cost + item.retry_cost for item in costs or []), 4),
    }


def create_app(container: Container | None = None) -> FastAPI:
    container = container or build_container()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        container.gateway.recover_after_restart()
        yield

    app = FastAPI(title="AI Director Platform", version="1.0.0", lifespan=lifespan)
    app.state.container = container
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip() for origin in container.settings.web_origins.split(",") if origin.strip()
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def verify_api_key(authorization: str | None = Header(default=None)) -> None:
        expected = container.settings.platform_api_key
        if expected:
            token = authorization.removeprefix("Bearer ").strip() if authorization else ""
            if not secrets.compare_digest(token, expected):
                raise HTTPException(401, "invalid API key")

    def ensure_workspace(session, requested_id: str | None = None):  # type: ignore[no-untyped-def]
        if requested_id:
            workspace = session.get(Workspace, requested_id)
            if not workspace:
                raise HTTPException(404, "workspace not found")
            return workspace
        workspace = session.scalar(select(Workspace).order_by(Workspace.created_at))
        if workspace:
            return workspace
        user = session.scalar(select(User).where(User.email == "local@ai-director.invalid"))
        if not user:
            user = User(email="local@ai-director.invalid", display_name="Local Director")
            session.add(user)
            session.flush()
        workspace = Workspace(owner_user_id=user.id, name="Director Workspace")
        session.add(workspace)
        session.flush()
        return workspace

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "ai-video-platform", "providers": container.providers.list()}

    @app.post("/v1/projects", dependencies=[Depends(verify_api_key)])
    def create_project(body: ProjectCreate):
        with container.database.session() as session:
            workspace = ensure_workspace(session, body.workspace_id)
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
            return {"id": item.id, "title": item.title, "status": item.status}

    @app.get("/v1/projects", dependencies=[Depends(verify_api_key)])
    def list_projects():
        with container.database.session() as session:
            return [
                {
                    "id": project.id,
                    "workspace_id": project.workspace_id,
                    "name": project.name or project.title,
                    "description": project.description,
                    "status": project.status,
                    "default_provider": project.default_provider,
                    "default_aspect_ratio": project.default_aspect_ratio,
                }
                for project in session.scalars(select(Project).order_by(Project.updated_at.desc()))
            ]

    @app.get("/v1/projects/{project_id}", dependencies=[Depends(verify_api_key)])
    def get_project(project_id: str):
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

    @app.post("/v1/episodes", dependencies=[Depends(verify_api_key)])
    def create_episode(body: EpisodeCreate):
        with container.database.session() as session:
            if not session.get(Project, body.project_id):
                raise HTTPException(404, "project not found")
            item = Episode(**body.model_dump())
            session.add(item)
            session.flush()
            return {"id": item.id, "project_id": item.project_id, "episode_number": item.episode_number}

    @app.post("/v1/projects/{project_id}/episodes", dependencies=[Depends(verify_api_key)])
    def create_project_episode(project_id: str, body: EpisodeCreate):
        if body.project_id != project_id:
            raise HTTPException(409, "project ID in path and body differ")
        return create_episode(body)

    @app.post("/v1/episodes/{episode_id}/compile", dependencies=[Depends(verify_api_key)])
    def compile_episode(episode_id: str):
        try:
            result = container.orchestrator.compile_episode(episode_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"episode_id": episode_id, "stage": result.stage, **result.detail}

    @app.get("/v1/episodes/{episode_id}", dependencies=[Depends(verify_api_key)])
    def get_episode(episode_id: str):
        with container.database.session() as session:
            episode = session.get(Episode, episode_id)
            if not episode:
                raise HTTPException(404, "episode not found")
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

    @app.post("/v1/scenes", dependencies=[Depends(verify_api_key)])
    def create_scene(body: SceneCreate):
        with container.database.session() as session:
            if not session.get(Episode, body.episode_id):
                raise HTTPException(404, "episode not found")
            values = body.model_dump()
            values["scene_description"] = body.description
            item = Scene(**values)
            session.add(item)
            session.flush()
            return {"id": item.id, "episode_id": item.episode_id, "sequence": item.sequence}

    @app.post("/v1/shots", dependencies=[Depends(verify_api_key)])
    def create_shot(body: ShotCreate):
        with container.database.session() as session:
            if not session.get(Scene, body.scene_id):
                raise HTTPException(404, "scene not found")
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

    @app.get("/v1/shots/{shot_id}", dependencies=[Depends(verify_api_key)])
    def get_shot(shot_id: str):
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise HTTPException(404, "shot not found")
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

    @app.post("/v1/shots/{shot_id}/generate", dependencies=[Depends(verify_api_key)], status_code=202)
    def generate_shot(shot_id: str, body: CandidateGenerate):
        try:
            bindings = [container.characters.binding(character_id) for character_id in body.character_ids]
            candidate, replayed = container.candidates.create_candidate(
                shot_id,
                idempotency_key=body.idempotency_key,
                fallback_providers=body.fallback_providers,
                character_bindings=bindings,
                reference_asset_ids=body.reference_asset_ids,
                estimated_cost=body.estimated_cost,
            )
            return {**_candidate_view(candidate), "replayed": replayed}
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/v1/shots/{shot_id}/candidates", dependencies=[Depends(verify_api_key)])
    def list_candidates(shot_id: str):
        with container.database.session() as session:
            if not session.get(Shot, shot_id):
                raise HTTPException(404, "shot not found")
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

    @app.post(
        "/v1/shots/{shot_id}/candidates/{candidate_id}/validate", dependencies=[Depends(verify_api_key)]
    )
    def validate_candidate(shot_id: str, candidate_id: str, body: CandidateValidate):
        with container.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate or candidate.shot_id != shot_id:
                raise HTTPException(404, "candidate not found for shot")
        try:
            result = container.candidates.sync_candidate(candidate_id, body.evidence)
            return _candidate_view(result)
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/shots/{shot_id}/candidates/{candidate_id}/commit", dependencies=[Depends(verify_api_key)])
    def commit_candidate(shot_id: str, candidate_id: str):
        with container.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate or candidate.shot_id != shot_id:
                raise HTTPException(404, "candidate not found for shot")
        try:
            return _candidate_view(container.candidates.commit(candidate_id))
        except CandidateNotCommittable as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/characters", dependencies=[Depends(verify_api_key)])
    def create_character(body: CharacterCreate):
        with container.database.session() as session:
            if not session.get(Project, body.project_id):
                raise HTTPException(404, "project not found")
        character = container.characters.create_character(
            body.project_id, body.name, body.description, body.canonical_facts
        )
        return {"id": character.id, "name": character.name, "status": character.status}

    @app.get("/v1/projects/{project_id}/characters", dependencies=[Depends(verify_api_key)])
    def list_characters(project_id: str):
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

    @app.post("/v1/characters/{character_id}/confirm-identity", dependencies=[Depends(verify_api_key)])
    def confirm_character_identity(character_id: str, body: CharacterConfirm):
        try:
            identity = container.characters.confirm_identity(
                character_id,
                body.master_asset_id,
                references=body.references,
                hair_signature=body.hair_signature,
                costume_signature=body.costume_signature,
            )
            return {
                "id": identity.id,
                "character_id": identity.character_id,
                "version": identity.version,
                "status": identity.status,
                "master_asset_id": identity.master_asset_id,
            }
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/prompts/refine", dependencies=[Depends(verify_api_key)])
    def refine_prompt(body: PromptRefine):
        with container.database.session() as session:
            if not session.get(Project, body.project_id):
                raise HTTPException(404, "project not found")
        result = container.prompts.refine(body.prompt)
        with container.database.session() as session:
            compilation = PromptCompilation(
                project_id=body.project_id,
                user_prompt=result.original,
                compiled_prompt=result.refined,
                compiler_version=container.prompts.version,
                skill_versions={"prompt-compiler": "v1"},
                diff_json={"changes": result.changes, "preserved_facts": result.preserved_facts},
            )
            session.add(compilation)
            session.flush()
            return {
                "id": compilation.id,
                "original": result.original,
                "refined": result.refined,
                "changes": result.changes,
                "preserved_facts": result.preserved_facts,
            }

    @app.post("/v1/shots/{shot_id}/continuity", dependencies=[Depends(verify_api_key)])
    def evaluate_continuity(shot_id: str, body: ContinuityEvaluate):
        try:
            risk = ContinuityRiskVector(**body.risk)
            decision = container.continuity_decision.decide(risk, project_id=body.project_id, shot_id=shot_id)
            return {
                "mode": decision.mode,
                "risk_score": decision.risk_score,
                "reasons": decision.reasons,
                "use_previous_end_frame": decision.use_previous_end_frame,
                "require_new_keyframe": decision.require_new_keyframe,
            }
        except TypeError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/shots/{shot_id}/decisions", dependencies=[Depends(verify_api_key)])
    def shot_decisions(shot_id: str):
        with container.database.session() as session:
            if not session.get(Shot, shot_id):
                raise HTTPException(404, "shot not found")
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

    @app.get("/v1/shots/{shot_id}/cost", dependencies=[Depends(verify_api_key)])
    def shot_cost(shot_id: str):
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

    @app.post("/v1/assets", dependencies=[Depends(verify_api_key)])
    async def upload_asset(
        project_id: str = Form(...),
        asset_type: str = Form(...),
        file: UploadFile = File(...),
        shot_id: str | None = Form(default=None),
        character_id: str | None = Form(default=None),
    ):
        with container.database.session() as session:
            if not session.get(Project, project_id):
                raise HTTPException(404, "project not found")
            if character_id:
                character = session.get(Character, character_id)
                if not character or character.project_id != project_id:
                    raise HTTPException(404, "character not found in project")
        asset, reused = container.media.register(
            project_id,
            asset_type,
            file.file,
            filename=file.filename or "asset.bin",
            mime_type=file.content_type,
            shot_id=shot_id,
            character_id=character_id,
        )
        return {
            "id": asset.id,
            "sha256": asset.sha256,
            "asset_type": asset.asset_type,
            "storage_key": asset.storage_key,
            "public_url": asset.public_url,
            "reused": reused,
        }

    @app.get("/v1/assets/{asset_id}", dependencies=[Depends(verify_api_key)])
    def get_asset(asset_id: str):
        asset = container.media.get(asset_id)
        if not asset:
            raise HTTPException(404, "asset not found")
        return {
            "id": asset.id,
            "project_id": asset.project_id,
            "asset_type": asset.asset_type,
            "sha256": asset.sha256,
            "mime_type": asset.mime_type,
            "width": asset.width,
            "height": asset.height,
            "duration": asset.duration,
            "provider": asset.provider,
            "provider_media_id": asset.provider_media_id,
            "public_url": asset.public_url,
        }

    @app.get("/v1/storage/{storage_key:path}")
    def serve_storage(storage_key: str):
        try:
            path = container.storage.path_for(storage_key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not path.is_file():
            raise HTTPException(404, "stored object not found")
        return FileResponse(path)

    @app.post("/v1/generations", dependencies=[Depends(verify_api_key)], status_code=202)
    def create_generation(body: GenerationRequest):
        try:
            job, replayed = container.gateway.create(body)
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {**_job_view(job), "replayed": replayed}

    @app.get("/v1/generations/{job_id}", dependencies=[Depends(verify_api_key)])
    def get_generation(job_id: str):
        job = container.gateway.get(job_id)
        if not job:
            raise HTTPException(404, "generation not found")
        return {
            **_job_view(job),
            "events": [
                {"type": event.event_type, "detail": event.detail, "created_at": event.created_at}
                for event in container.gateway.events(job_id)
            ],
        }

    @app.post("/v1/generations/{job_id}/retry", dependencies=[Depends(verify_api_key)])
    def retry_generation(job_id: str):
        try:
            return _job_view(container.gateway.retry(job_id))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except UnsafeRetry as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/generations/{job_id}/cancel", dependencies=[Depends(verify_api_key)])
    def cancel_generation(job_id: str):
        try:
            return _job_view(container.gateway.cancel(job_id))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/generations/{job_id}/reconcile", dependencies=[Depends(verify_api_key)])
    def reconcile_generation(job_id: str):
        try:
            return _job_view(container.gateway.reconcile(job_id))
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/v1/providers", dependencies=[Depends(verify_api_key)])
    async def list_providers():
        result = []
        for name in container.providers.list():
            health = await container.providers.get(name).health()
            capabilities = container.capabilities.get(name)
            result.append(
                {
                    "name": name,
                    "healthy": health.ok,
                    "detail": health.detail,
                    "capabilities": asdict(capabilities) if capabilities else {},
                    "performance": container.cost.provider_metrics(name),
                }
            )
        return result

    @app.get("/v1/skills", dependencies=[Depends(verify_api_key)])
    def list_skills():
        return [
            {"name": skill.name, "category": skill.category, "path": skill.path}
            for skill in container.skills.list_skills()
        ]

    @app.get("/v1/providers/{provider}/health", dependencies=[Depends(verify_api_key)])
    async def provider_health(provider: str):
        try:
            result = await container.providers.get(provider).health()
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": result.ok, "detail": result.detail, "metadata": result.metadata}

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

    @app.post("/v1/workers/register", dependencies=[Depends(verify_api_key)])
    def register_worker(body: WorkerRegister):
        if body.account_id:
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

    @app.post("/v1/workers/{worker_id}/heartbeat", dependencies=[Depends(verify_api_key)])
    def heartbeat(worker_id: str, body: WorkerHeartbeat):
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

    @app.get("/v1/workers/{worker_id}/commands", dependencies=[Depends(verify_api_key)])
    def poll_commands(worker_id: str, connection_id: str):
        try:
            commands = container.runtime.claim_commands(worker_id, connection_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "commands": [{"id": cmd.id, "type": cmd.message_type, "payload": cmd.payload} for cmd in commands]
        }

    @app.post("/v1/workers/{worker_id}/responses", dependencies=[Depends(verify_api_key)])
    def command_response(worker_id: str, body: WorkerResponse):
        try:
            command = container.runtime.complete_command(
                worker_id, body.connection_id, body.command_id, response=body.response, error=body.error
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "status": command.status}

    @app.websocket("/v1/workers/ws/{worker_id}")
    async def worker_socket(websocket: WebSocket, worker_id: str):
        token = websocket.query_params.get("token", "")
        if container.settings.platform_api_key and not secrets.compare_digest(
            token, container.settings.platform_api_key
        ):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        connection_id = ""
        try:
            first = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            if first.get("type") != "worker.register":
                await websocket.close(code=4400)
                return
            payload = WorkerRegister.model_validate({**first.get("payload", {}), "worker_id": worker_id})
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
                for command in container.runtime.claim_commands(worker_id, connection_id):
                    await websocket.send_json(
                        {"id": command.id, "type": command.message_type, "payload": command.payload}
                    )
                try:
                    message = await asyncio.wait_for(websocket.receive_json(), timeout=1)
                except TimeoutError:
                    container.runtime.heartbeat(worker_id, connection_id)
                    continue
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
            container.runtime.mark_offline(worker_id, connection_id or None)

    @app.post("/v1/images/generations", dependencies=[Depends(verify_api_key)], status_code=202)
    def openai_image_adapter(request: Request, body: dict[str, Any]):
        key = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
        if not key:
            raise HTTPException(400, "Idempotency-Key is required")
        generation = GenerationRequest(
            project_id=body["project_id"],
            type="image",
            provider=body.get("provider", "google_flow"),
            model=body.get("model", "NARWHAL"),
            prompt=body["prompt"],
            aspect_ratio=body.get("aspect_ratio", "1:1"),
            reference_asset_ids=body.get("reference_asset_ids", []),
            idempotency_key=key,
        )
        try:
            job, replayed = container.gateway.create(generation)
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": job.id, "object": "image.generation", "status": job.status, "replayed": replayed}

    @app.post("/v1/videos/generations", dependencies=[Depends(verify_api_key)], status_code=202)
    def openai_video_adapter(request: Request, body: dict[str, Any]):
        key = request.headers.get("Idempotency-Key") or body.get("idempotency_key")
        if not key:
            raise HTTPException(400, "Idempotency-Key is required")
        generation = GenerationRequest(
            project_id=body["project_id"],
            type="video",
            provider=body.get("provider", "google_flow"),
            model=body.get("model", "veo"),
            prompt=body["prompt"],
            duration=body.get("duration", 8),
            aspect_ratio=body.get("aspect_ratio", "9:16"),
            start_frame_asset_id=body.get("start_frame_asset_id"),
            end_frame_asset_id=body.get("end_frame_asset_id"),
            reference_asset_ids=body.get("reference_asset_ids", []),
            idempotency_key=key,
        )
        try:
            job, replayed = container.gateway.create(generation)
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": job.id, "object": "video.generation", "status": job.status, "replayed": replayed}

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("video_platform_api.main:app", host="0.0.0.0", port=8080)
