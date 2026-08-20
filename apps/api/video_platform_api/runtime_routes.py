from __future__ import annotations

from typing import Any, Literal

from asset_registry_core import (
    AssetVersionNotPromotable,
    CanonicalVersionNotSet,
    VersionMediaInput,
)
from evaluation_core import (
    EvaluationEvidence,
    EvaluationExpectation,
    EvaluationResult,
)
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from generation_gateway import IdempotencyConflict
from memory_core import MemoryLayer, MemoryQuery, MultimodalContent, ShotMemoryInput
from platform_contracts import PassengerGenerationCommand
from production_domain.models import (
    Asset,
    AssetVersion,
    GenerationJob,
    MediaAsset,
    ProductionTrace,
)
from pydantic import BaseModel, Field
from sqlalchemy import select

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
            estimate = container.credit_pricing.estimate(
                provider=body.provider,
                model=body.model,
                media_type=body.media_type,
                duration=body.duration or 1,
                resolution=body.resolution,
                reference_count=len(body.reference_asset_ids),
            )
            body = body.model_copy(update={"estimated_cost": estimate.estimated_total_usd})
            job, replayed = container.visual_runtime.submit_passenger(body)
        except IdempotencyConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LookupError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "id": job.id,
            "status": job.status,
            "provider": job.provider,
            "model": job.model,
            "output_asset_id": job.output_asset_id,
            "estimated_cost": job.cost_estimate,
            "estimated_credits": estimate.credits,
            "credit_pricing_version": container.credit_pricing.version,
            "replayed": replayed,
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
            return _asset_view(
                container.asset_registry.promote(
                    asset_id,
                    version_id,
                    promoted_by_user_id=(None if principal.development_bypass else principal.user_id),
                    reason=body.reason,
                    metadata=body.metadata,
                )
            )
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
                metadata={"generation_job_id": job_id},
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
