from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Any

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
from fastapi.responses import FileResponse
from generation_gateway import IdempotencyConflict
from generation_gateway.gateway import UnsafeRetry
from platform_contracts import EpisodeCreate, GenerationRequest, ProjectCreate, SceneCreate, ShotCreate
from production_domain.models import (
    BrowserWorker,
    Episode,
    Project,
    ProviderAccount,
    ProviderCredential,
    Scene,
    Shot,
    WorkerStatus,
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


def create_app(container: Container | None = None) -> FastAPI:
    container = container or build_container()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        container.gateway.recover_after_restart()
        yield

    app = FastAPI(title="AI Video Production Platform", version="1.0.0", lifespan=lifespan)
    app.state.container = container

    def verify_api_key(authorization: str | None = Header(default=None)) -> None:
        expected = container.settings.platform_api_key
        if expected:
            token = authorization.removeprefix("Bearer ").strip() if authorization else ""
            if not secrets.compare_digest(token, expected):
                raise HTTPException(401, "invalid API key")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "ai-video-platform", "providers": container.providers.list()}

    @app.post("/v1/projects", dependencies=[Depends(verify_api_key)])
    def create_project(body: ProjectCreate):
        with container.database.session() as session:
            item = Project(title=body.title, description=body.description)
            session.add(item)
            session.flush()
            return {"id": item.id, "title": item.title, "status": item.status}

    @app.post("/v1/episodes", dependencies=[Depends(verify_api_key)])
    def create_episode(body: EpisodeCreate):
        with container.database.session() as session:
            if not session.get(Project, body.project_id):
                raise HTTPException(404, "project not found")
            item = Episode(**body.model_dump())
            session.add(item)
            session.flush()
            return {"id": item.id, "project_id": item.project_id, "episode_number": item.episode_number}

    @app.post("/v1/scenes", dependencies=[Depends(verify_api_key)])
    def create_scene(body: SceneCreate):
        with container.database.session() as session:
            if not session.get(Episode, body.episode_id):
                raise HTTPException(404, "episode not found")
            item = Scene(**body.model_dump())
            session.add(item)
            session.flush()
            return {"id": item.id, "episode_id": item.episode_id, "sequence": item.sequence}

    @app.post("/v1/shots", dependencies=[Depends(verify_api_key)])
    def create_shot(body: ShotCreate):
        with container.database.session() as session:
            if not session.get(Scene, body.scene_id):
                raise HTTPException(404, "scene not found")
            item = Shot(**body.model_dump())
            session.add(item)
            session.flush()
            return {
                "id": item.id,
                "scene_id": item.scene_id,
                "sequence": item.sequence,
                "continuity_mode": item.continuity_mode,
            }

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
    ):
        with container.database.session() as session:
            if not session.get(Project, project_id):
                raise HTTPException(404, "project not found")
        asset, reused = container.media.register(
            project_id,
            asset_type,
            file.file,
            filename=file.filename or "asset.bin",
            mime_type=file.content_type,
            shot_id=shot_id,
        )
        return {
            "id": asset.id,
            "sha256": asset.sha256,
            "asset_type": asset.asset_type,
            "storage_key": asset.storage_key,
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
            result.append({"name": name, "healthy": health.ok, "detail": health.detail})
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
