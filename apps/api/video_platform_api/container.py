from __future__ import annotations

from dataclasses import dataclass

from agent_runtime import AgentRuntime, SkillRegistry
from asset_registry_core import AssetRegistry
from browser_runtime import BrowserRuntime
from character_core import CharacterIdentityService
from continuity_core import ContinuityDecisionEngine
from cost_core import CostEngine, CreditPricingEngine
from director_production import AgentOrchestrator, CandidatePipeline
from evaluation_core import GenerationEvaluator, RetryEngine
from generation_gateway import GenerationGateway, ProviderRouter
from generation_gateway.retry import RetryPolicy
from generation_gateway.scheduler import AccountScheduler
from generation_policy_core import CapabilityResolver, ProviderCapabilityRegistry
from google_flow_provider import GoogleFlowProvider
from grok_provider import GrokProvider
from image_prompt_core import ImagePromptCorrector
from kling_provider import KlingProvider
from media_service import MediaRegistry
from memory_core import (
    ContextAssembler,
    ContextBudget,
    LocalTestEmbeddingProvider,
    MultimodalMemoryEngine,
    VoyageMultimodalEmbeddingProvider,
)
from model_metrics_core import ModelBenchmarkSuite, ModelMetricsService
from model_registry_core import ModelCapabilityRegistry, VideoModelRouter
from narrative_core import NarrativeCompiler
from omni_provider import OmniProvider
from platform_database import Database
from platform_shared import (
    CredentialVault,
    LocalStorage,
    S3CompatibleStorage,
    Settings,
    StorageProvider,
)
from production_engine import ProductionEngine, ShotContinuityService
from production_engine.runtime import VisualProductionRuntime
from qa_core import QAPipeline
from runtime_control_core import FeatureFlagDefaults, FeatureFlagService
from runway_provider import RunwayProvider
from seedance_provider import SeedanceProvider
from skill_core import PromptCompilerService
from veo_provider import VeoOfficialProvider
from video_adapter_core import VideoAdapterRegistry
from video_prompt_core import VideoShotPromptCompiler


def _parse_provider_media_hosts(value: str) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for group in value.split(";"):
        provider, separator, hosts = group.partition("=")
        patterns = tuple(item.strip().lower() for item in hosts.split(",") if item.strip())
        if separator and provider.strip() and patterns:
            result[provider.strip()] = patterns
    return result


@dataclass
class Container:
    settings: Settings
    database: Database
    storage: StorageProvider
    media: MediaRegistry
    runtime: BrowserRuntime
    providers: ProviderRouter
    scheduler: AccountScheduler
    continuity: ShotContinuityService
    gateway: GenerationGateway
    credentials: CredentialVault
    production: ProductionEngine
    skills: SkillRegistry
    agents: AgentRuntime
    narrative: NarrativeCompiler
    characters: CharacterIdentityService
    continuity_decision: ContinuityDecisionEngine
    capabilities: ProviderCapabilityRegistry
    capability_resolver: CapabilityResolver
    qa: QAPipeline
    cost: CostEngine
    prompts: PromptCompilerService
    candidates: CandidatePipeline
    orchestrator: AgentOrchestrator
    model_registry: ModelCapabilityRegistry
    video_router: VideoModelRouter
    video_adapters: VideoAdapterRegistry
    image_prompts: ImagePromptCorrector
    asset_registry: AssetRegistry
    feature_flags: FeatureFlagService
    memory: MultimodalMemoryEngine
    context: ContextAssembler
    video_prompt_compiler: VideoShotPromptCompiler
    evaluator: GenerationEvaluator
    retry_engine: RetryEngine
    model_metrics: ModelMetricsService
    benchmarks: ModelBenchmarkSuite
    visual_runtime: VisualProductionRuntime
    credit_pricing: CreditPricingEngine


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
    if settings.deployment_environment == "production":
        if not settings.auth_required:
            raise RuntimeError("AUTH_REQUIRED must remain enabled in production")
        api_key_bytes = settings.platform_api_key.encode("utf-8")
        if len(api_key_bytes) < 32 or len(set(api_key_bytes)) < 16:
            raise RuntimeError(
                "PLATFORM_API_KEY must be a high-entropy secret of at least 32 bytes in production"
            )
    database = Database(settings.database_url)
    database.create_all()
    storage: StorageProvider
    if settings.storage_backend.lower() == "s3":
        storage = S3CompatibleStorage(
            bucket=settings.s3_bucket,
            cache_root=settings.storage_root,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
            access_key_id=settings.s3_access_key_id,
            secret_access_key=settings.s3_secret_access_key,
            public_base_url=settings.public_base_url,
            max_object_bytes=settings.max_upload_bytes,
        )
    else:
        storage = LocalStorage(
            settings.storage_root,
            settings.public_base_url,
            max_object_bytes=settings.max_upload_bytes,
        )
    media = MediaRegistry(
        database,
        storage,
        provider_media_hosts=_parse_provider_media_hosts(settings.provider_media_allowed_hosts),
        max_download_bytes=min(settings.max_provider_download_bytes, settings.max_upload_bytes),
        max_image_pixels=settings.max_image_pixels,
    )
    runtime = BrowserRuntime(database, heartbeat_timeout_seconds=settings.worker_heartbeat_timeout_seconds)
    providers = ProviderRouter()
    providers.register(GoogleFlowProvider(runtime, settings, database))
    providers.register(SeedanceProvider())
    providers.register(VeoOfficialProvider())
    providers.register(GrokProvider())
    providers.register(OmniProvider())
    providers.register(KlingProvider())
    providers.register(RunwayProvider())
    model_registry = ModelCapabilityRegistry(settings.model_config_root)
    for profile in model_registry.all(include_disabled=True):
        providers.register_model(
            profile.provider,
            profile.model_id,
            "video",
            available=profile.status != "disabled",
        )
    # Flow currently exposes one image model plus a legacy video alias. Both are
    # registered explicitly so arbitrary names never reach a provider transport.
    providers.register_model("google_flow", "NARWHAL", "image")
    flow_profile = model_registry.get("flow-veo-3.1", "google_flow")
    providers.register_model(
        "google_flow",
        "veo",
        "video",
        available=bool(flow_profile and flow_profile.status != "disabled"),
    )
    scheduler = AccountScheduler(database)
    continuity = ShotContinuityService(database, media)
    gateway = GenerationGateway(
        database,
        providers,
        media,
        scheduler,
        continuity,
        RetryPolicy(),
        claim_lease_seconds=settings.generation_claim_lease_seconds,
        poll_interval_seconds=settings.generation_poll_interval_seconds,
    )
    credentials = CredentialVault(
        settings.credential_encryption_key,
        allow_ephemeral_key=settings.deployment_environment in {"development", "test"},
    )
    production = ProductionEngine(database)
    skills = SkillRegistry(settings.skills_root)
    agents = AgentRuntime(production, gateway, media, skills)
    narrative = NarrativeCompiler(database)
    characters = CharacterIdentityService(database)
    continuity_decision = ContinuityDecisionEngine(database)
    capabilities = ProviderCapabilityRegistry()
    capability_resolver = CapabilityResolver(database, capabilities)
    qa = QAPipeline(database)
    cost = CostEngine(database)
    prompts = PromptCompilerService(database, skills)
    credit_pricing = CreditPricingEngine(model_registry)
    video_router = VideoModelRouter(model_registry)
    video_adapters = VideoAdapterRegistry()
    image_prompts = ImagePromptCorrector()
    asset_registry = AssetRegistry(database)
    feature_flags = FeatureFlagService(
        database,
        FeatureFlagDefaults(
            voyage_memory=settings.feature_voyage_memory,
            auto_evaluation=settings.feature_auto_evaluation,
            adaptive_router=settings.feature_adaptive_router,
            wan3=settings.feature_wan3,
            auto_retry=settings.feature_auto_retry,
        ),
    )
    embeddings = (
        VoyageMultimodalEmbeddingProvider(
            settings.voyage_api_key,
            model=settings.voyage_multimodal_model,
            dimension=settings.memory_embedding_dimension,
        )
        if settings.voyage_api_key
        else LocalTestEmbeddingProvider(settings.memory_embedding_dimension)
    )
    # The engine remains callable for explicit indexing/search endpoints; product
    # pipelines gate automatic retrieval per project through FeatureFlagService.
    memory = MultimodalMemoryEngine(database, embeddings, enabled=True)
    context = ContextAssembler(
        ContextBudget(
            max_characters=settings.memory_max_characters,
            max_tokens=settings.memory_max_tokens,
            max_images=settings.memory_max_images,
            max_videos=settings.memory_max_videos,
        )
    )
    video_prompt_compiler = VideoShotPromptCompiler(database)
    evaluator = GenerationEvaluator(database)
    retry_engine = RetryEngine(settings.max_auto_retries)
    model_metrics = ModelMetricsService(database)
    benchmarks = ModelBenchmarkSuite(database)
    visual_runtime = VisualProductionRuntime(
        database,
        gateway,
        asset_registry,
        memory,
        context,
        video_router,
        video_adapters,
        video_prompt_compiler,
        evaluator,
        retry_engine,
        model_metrics,
        benchmarks,
        feature_flags,
    )
    candidates = CandidatePipeline(
        database,
        gateway,
        prompts,
        capability_resolver,
        qa,
        cost,
        continuity,
        visual_runtime=visual_runtime,
    )
    orchestrator = AgentOrchestrator(narrative, characters, continuity_decision, prompts, candidates)
    return Container(
        settings=settings,
        database=database,
        storage=storage,
        media=media,
        runtime=runtime,
        providers=providers,
        scheduler=scheduler,
        continuity=continuity,
        gateway=gateway,
        credentials=credentials,
        production=production,
        skills=skills,
        agents=agents,
        narrative=narrative,
        characters=characters,
        continuity_decision=continuity_decision,
        capabilities=capabilities,
        capability_resolver=capability_resolver,
        qa=qa,
        cost=cost,
        prompts=prompts,
        candidates=candidates,
        orchestrator=orchestrator,
        model_registry=model_registry,
        video_router=video_router,
        video_adapters=video_adapters,
        image_prompts=image_prompts,
        asset_registry=asset_registry,
        feature_flags=feature_flags,
        memory=memory,
        context=context,
        video_prompt_compiler=video_prompt_compiler,
        evaluator=evaluator,
        retry_engine=retry_engine,
        model_metrics=model_metrics,
        benchmarks=benchmarks,
        visual_runtime=visual_runtime,
        credit_pricing=credit_pricing,
    )
