from __future__ import annotations

from dataclasses import dataclass

from agent_runtime import AgentRuntime, SkillRegistry
from asset_registry_core import AssetRegistry
from browser_runtime import BrowserRuntime
from character_core import CharacterIdentityService
from continuity_core import ContinuityDecisionEngine
from cost_core import CostEngine, CreditPricingEngine
from deepseek_provider import DeepSeekProvider
from director_production import AgentOrchestrator, CandidatePipeline
from entitlement_core import (
    GenerationAdmissionService,
    ModelRoleRuntime,
    WorkspaceCreditService,
    WorkspaceModelResolver,
)
from evaluation_core import GenerationEvaluator, RetryEngine
from generation_gateway import DirectAPIResourceRegistry, GenerationGateway, ProviderRouter
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
from model_registry_core import (
    ModelCapabilityRegistry,
    ModelInfrastructureService,
    VideoModelRouter,
)
from narrative_core import NarrativeCompiler
from omni_provider import OmniProvider
from openrouter_provider import OpenRouterProvider
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
from provider_budget_core import DatabaseProviderBudgetRepository
from provider_sdk import (
    LIVE_PROVIDER_CONFIRMATION,
    AssetCriticality,
    LiveProviderSettings,
    ProviderCapability,
    ProviderCapabilityCatalog,
    ProviderTrustLevel,
)
from qa_core import QAPipeline
from runapi_provider import RunAPIEdgeProvider
from runtime_control_core import FeatureFlagDefaults, FeatureFlagService
from runway_provider import RunwayProvider
from seedance_provider import SeedanceProvider
from skill_core import PromptCompilerService
from veo_provider import VeoOfficialProvider
from video_adapter_core import VideoAdapterRegistry
from video_prompt_core import VideoShotPromptCompiler
from wan_provider import WanProvider


def _parse_provider_media_hosts(value: str) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for group in value.split(";"):
        provider, separator, hosts = group.partition("=")
        patterns = tuple(item.strip().lower() for item in hosts.split(",") if item.strip())
        if separator and provider.strip() and patterns:
            result[provider.strip()] = patterns
    return result


def _bind_runtime_video_profile(
    registry: ModelCapabilityRegistry,
    provider: str,
    provider_model_id: str | None,
) -> None:
    """Expose an env-resolved model ID without changing its reviewed capability priors."""

    profiles = registry.by_provider(provider)
    if len(profiles) != 1:
        raise RuntimeError(f"expected exactly one capability profile for runtime provider {provider}")
    source = profiles[0]
    if provider_model_id is None:
        registry.replace(source.model_copy(update={"status": "disabled"}))
        return
    if source.model_id == provider_model_id:
        return
    registry.replace(source.model_copy(update={"status": "disabled"}))
    registry.replace(
        source.model_copy(
            update={
                "model_id": provider_model_id,
                "version": f"{source.version}+runtime",
                "source": "runtime_environment_alias",
            }
        )
    )


def _bind_provider_model_alias(
    registry: ModelCapabilityRegistry,
    *,
    source_provider: str,
    source_model_id: str,
    target_provider: str,
    target_model_id: str,
) -> None:
    """Bind reviewed capability/cost priors to a transport-specific model ID."""

    if registry.get(target_model_id, target_provider) is not None:
        return
    source = registry.get(source_model_id, source_provider)
    if source is None:
        raise RuntimeError(f"capability alias source is missing: {source_provider}:{source_model_id}")
    registry.replace(
        source.model_copy(
            update={
                "provider": target_provider,
                "model_id": target_model_id,
                "version": f"{source.version}+{target_provider}-alias",
                "adapter": target_provider,
                "source": "reviewed_transport_alias",
            }
        )
    )


@dataclass
class Container:
    settings: Settings
    database: Database
    storage: StorageProvider
    media: MediaRegistry
    runtime: BrowserRuntime
    providers: ProviderRouter
    provider_capabilities: ProviderCapabilityCatalog
    provider_budget: DatabaseProviderBudgetRepository
    direct_api_resources: DirectAPIResourceRegistry
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
    model_infrastructure: ModelInfrastructureService
    workspace_models: WorkspaceModelResolver
    model_roles: ModelRoleRuntime
    generation_admission: GenerationAdmissionService
    workspace_credits: WorkspaceCreditService
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
    live_provider_settings = LiveProviderSettings(
        provider_mode=settings.provider_mode,
        allow_live_provider_calls=settings.allow_live_provider_calls,
        live_provider_confirmation=settings.live_provider_confirmation,
    )
    provider_budget = DatabaseProviderBudgetRepository(database)
    provider_budget.ensure("runapi", settings.runapi_budget_usd)
    direct_api_resources = DirectAPIResourceRegistry(database)
    providers = ProviderRouter()
    google_flow = GoogleFlowProvider(runtime, settings, database)
    seedance = SeedanceProvider(
        api_key=settings.ark_api_key,
        base_url=settings.ark_base_url,
        doubao_model_id=settings.doubao_model_id,
        seedance_model_id=settings.seedance_model_id,
        timeout_seconds=settings.provider_http_timeout_seconds,
        transport_settings=live_provider_settings,
    )
    openrouter = OpenRouterProvider(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        timeout_seconds=settings.provider_http_timeout_seconds,
        transport_settings=live_provider_settings,
    )
    wan = WanProvider(
        api_key=settings.wan_api_key,
        openai_base_url=settings.wan_openai_base_url,
        dashscope_base_url=settings.wan_dashscope_base_url,
        chat_model_id=settings.wan_chat_model_id,
        t2v_model_id=settings.wan2_7_t2v_model_id,
        i2v_model_id=settings.wan2_7_i2v_model_id,
        r2v_model_id=settings.wan2_7_r2v_model_id,
        timeout_seconds=settings.provider_http_timeout_seconds,
        transport_settings=live_provider_settings,
    )
    runapi = RunAPIEdgeProvider(
        api_key=settings.runapi_api_key,
        base_url=settings.runapi_base_url,
        model_id=settings.runapi_model_id,
        chat_path=settings.runapi_chat_path,
        image_path=settings.runapi_image_path,
        video_path=settings.runapi_video_path,
        timeout_seconds=settings.provider_http_timeout_seconds,
        budget_repository=provider_budget,
        budget_usd=settings.runapi_budget_usd,
        allow_edge_calls=settings.allow_runapi_edge_calls,
        transport_settings=live_provider_settings,
    )
    deepseek = DeepSeekProvider(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model_id=settings.deepseek_model_id,
        timeout_seconds=settings.provider_http_timeout_seconds,
        transport_settings=live_provider_settings,
    )
    providers.register(google_flow)
    providers.register(seedance)
    providers.register(openrouter)
    providers.register(wan)
    providers.register(runapi)
    providers.register(VeoOfficialProvider())
    providers.register(GrokProvider())
    providers.register(OmniProvider())
    providers.register(KlingProvider())
    providers.register(RunwayProvider())
    provider_capabilities = ProviderCapabilityCatalog()
    provider_capabilities.register(
        "google_flow", google_flow, {ProviderCapability.IMAGE.value, ProviderCapability.VIDEO.value}
    )
    provider_capabilities.register(
        "seedance",
        seedance,
        {
            ProviderCapability.CHAT.value,
            ProviderCapability.IMAGE.value,
            ProviderCapability.VIDEO.value,
        },
    )
    provider_capabilities.register(
        "openrouter",
        openrouter,
        {
            ProviderCapability.CHAT.value,
            ProviderCapability.RESPONSES.value,
            ProviderCapability.EMBEDDINGS.value,
            ProviderCapability.VIDEO.value,
        },
    )
    provider_capabilities.register(
        "wan", wan, {ProviderCapability.CHAT.value, ProviderCapability.VIDEO.value}
    )
    provider_capabilities.register(
        "runapi",
        runapi,
        {
            ProviderCapability.CHAT.value,
            ProviderCapability.IMAGE.value,
            ProviderCapability.VIDEO.value,
        },
    )
    provider_capabilities.register("deepseek", deepseek, {ProviderCapability.CHAT.value})
    model_registry = ModelCapabilityRegistry(settings.model_config_root)
    model_infrastructure = ModelInfrastructureService(
        database,
        settings.model_infrastructure_config,
    )
    default_sync = model_infrastructure.ensure_defaults()
    newly_created_models = set(default_sync.model_names_created)
    workspace_models = WorkspaceModelResolver(database, model_infrastructure)
    model_roles = ModelRoleRuntime(
        database,
        workspace_models,
        provider_capabilities,
        provider_mode=settings.provider_mode,
    )
    live_gate_ready = (
        settings.provider_mode == "live"
        and settings.allow_live_provider_calls is True
        and settings.live_provider_confirmation == LIVE_PROVIDER_CONFIRMATION
    )
    defaults_by_name = {
        definition.logical_name: definition for definition in model_infrastructure.config.models
    }
    runtime_video_routes: list[tuple[str, str, bool]] = []

    # Frozen catalogue defaults seed new databases only. Startup must not replay
    # them over administrator changes in the persisted model registry.
    for definition in model_infrastructure.runtime_models("openrouter"):
        if definition.modality == "video":
            runtime_video_routes.append(
                (definition.provider, definition.provider_model_id, definition.enabled)
            )
        if definition.logical_name in {
            "kling-3-standard-openrouter",
            "kling-3-pro-openrouter",
        }:
            _bind_provider_model_alias(
                model_registry,
                source_provider="kling",
                source_model_id="kling-3.0",
                target_provider=definition.provider,
                target_model_id=definition.provider_model_id,
            )

    doubao_default = defaults_by_name["doubao-free-reasoner"]
    if doubao_default.logical_name in newly_created_models and settings.doubao_model_id.strip():
        doubao_ready = bool(settings.ark_api_key.strip() and settings.ark_base_url.strip())
        model_infrastructure.configure_runtime_model(
            doubao_default.logical_name,
            settings.doubao_model_id,
            enabled=doubao_ready,
            live_enabled=bool(doubao_ready and live_gate_ready),
        )

    seedance_default = defaults_by_name["seedance-2.5-official"]
    seedance_runtime = model_infrastructure.runtime_model(seedance_default.logical_name)
    if seedance_default.logical_name in newly_created_models and settings.seedance_model_id.strip():
        seedance_ready = bool(settings.ark_api_key.strip() and settings.ark_base_url.strip())
        model_infrastructure.configure_runtime_model(
            seedance_default.logical_name,
            settings.seedance_model_id,
            enabled=seedance_ready,
            live_enabled=bool(seedance_ready and live_gate_ready),
            provider_trust_level=ProviderTrustLevel.PRODUCTION,
            criticality_allowed=list(AssetCriticality),
        )
        seedance_runtime = model_infrastructure.runtime_model(seedance_default.logical_name)
    seedance_available = seedance_runtime.enabled and seedance.configured
    runtime_video_routes.append(
        (seedance_default.provider, seedance_runtime.provider_model_id, seedance_available)
    )
    _bind_runtime_video_profile(
        model_registry,
        seedance_default.provider,
        seedance_runtime.provider_model_id if seedance_available else None,
    )

    wan_default = defaults_by_name["wan-2.7-official"]
    wan_runtime = model_infrastructure.runtime_model(wan_default.logical_name)
    if wan_default.logical_name in newly_created_models and settings.wan2_7_t2v_model_id.strip():
        wan_ready = bool(settings.wan_api_key.strip() and settings.wan_dashscope_base_url.strip())
        model_infrastructure.configure_runtime_model(
            wan_default.logical_name,
            settings.wan2_7_t2v_model_id,
            enabled=wan_ready,
            live_enabled=bool(wan_ready and live_gate_ready),
        )
        wan_runtime = model_infrastructure.runtime_model(wan_default.logical_name)
    wan_available = wan_runtime.enabled and wan.configured
    runtime_video_routes.append((wan_default.provider, wan_runtime.provider_model_id, wan_available))
    _bind_runtime_video_profile(
        model_registry,
        wan_default.provider,
        wan_runtime.provider_model_id if wan_available else None,
    )

    runapi_default = defaults_by_name["runapi-prompt-refiner-edge"]
    runapi_runtime = model_infrastructure.runtime_model(runapi_default.logical_name)
    if runapi_default.logical_name in newly_created_models and settings.runapi_model_id.strip():
        runapi_ready = bool(settings.runapi_api_key.strip() and settings.runapi_base_url.strip())
        model_infrastructure.configure_runtime_model(
            runapi_default.logical_name,
            settings.runapi_model_id,
            enabled=runapi_ready,
            live_enabled=bool(runapi_ready and live_gate_ready and settings.allow_runapi_edge_calls),
        )
        runapi_runtime = model_infrastructure.runtime_model(runapi_default.logical_name)

    for profile in model_registry.all(include_disabled=True):
        if profile.provider in {"openrouter", "runapi", "seedance", "wan"}:
            continue
        providers.register_model(
            profile.provider,
            profile.model_id,
            "video",
            available=profile.status != "disabled",
        )
    for provider_name, provider_model_id, enabled in runtime_video_routes:
        if provider_name in providers.list() and enabled:
            providers.register_model(
                provider_name,
                provider_model_id,
                "video",
                available=True,
            )
    wan_models = {wan_runtime.provider_model_id} if wan_available else set()
    if runapi_runtime.enabled and runapi.configured:
        providers.register_model("runapi", runapi_runtime.provider_model_id, "image")
        providers.register_model("runapi", runapi_runtime.provider_model_id, "video")

    openrouter_video_models = {
        provider_model_id
        for provider_name, provider_model_id, enabled in runtime_video_routes
        if provider_name == "openrouter" and enabled
    }
    if openrouter.configured and openrouter_video_models:
        direct_api_resources.ensure_provider(
            "openrouter",
            supported_models=openrouter_video_models,
            capabilities={"video"},
        )
    if seedance_available:
        direct_api_resources.ensure_provider(
            "seedance",
            supported_models={seedance_runtime.provider_model_id},
            capabilities={"video"},
        )
    if wan.configured and wan_models:
        direct_api_resources.ensure_provider(
            "wan",
            supported_models=wan_models,
            capabilities={"video"},
        )
    if runapi.configured and runapi_runtime.enabled:
        direct_api_resources.ensure_provider(
            "runapi",
            supported_models={runapi_runtime.provider_model_id},
            capabilities={"image", "video"},
        )
    # Flow currently exposes one image model plus a legacy video alias. Both are
    # registered explicitly so arbitrary names never reach a provider transport.
    flow_image_runtime = model_infrastructure.runtime_model("flow-narwhal-image-internal")
    providers.register_model(
        "google_flow",
        "NARWHAL",
        "image",
        available=flow_image_runtime.enabled,
    )
    flow_video_runtime = model_infrastructure.runtime_model("flow-veo-3.1-internal")
    flow_profile = model_registry.get("flow-veo-3.1", "google_flow")
    providers.register_model(
        "google_flow",
        "veo",
        "video",
        available=bool(flow_video_runtime.enabled and flow_profile and flow_profile.status != "disabled"),
    )
    scheduler = AccountScheduler(database)
    continuity = ShotContinuityService(database, media)
    workspace_credits = WorkspaceCreditService()
    gateway = GenerationGateway(
        database,
        providers,
        media,
        scheduler,
        continuity,
        RetryPolicy(),
        claim_lease_seconds=settings.generation_claim_lease_seconds,
        poll_interval_seconds=settings.generation_poll_interval_seconds,
        workspace_credits=workspace_credits,
        model_infrastructure=model_infrastructure,
        provider_mode=settings.provider_mode,
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
    generation_admission = GenerationAdmissionService(
        workspace_models,
        model_roles,
        credit_pricing,
    )
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
            transport_settings=live_provider_settings,
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
        generation_admission,
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
        generation_admission=generation_admission,
    )
    orchestrator = AgentOrchestrator(narrative, characters, continuity_decision, prompts, candidates)
    return Container(
        settings=settings,
        database=database,
        storage=storage,
        media=media,
        runtime=runtime,
        providers=providers,
        provider_capabilities=provider_capabilities,
        provider_budget=provider_budget,
        direct_api_resources=direct_api_resources,
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
        model_infrastructure=model_infrastructure,
        workspace_models=workspace_models,
        model_roles=model_roles,
        generation_admission=generation_admission,
        workspace_credits=workspace_credits,
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
