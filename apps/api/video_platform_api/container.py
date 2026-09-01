from __future__ import annotations

import logging
from dataclasses import dataclass

from agent_runtime import AgentRuntime
from asset_registry_core import AssetRegistry
from browser_runtime import BrowserRuntime
from character_core import (
    CharacterIdentityService,
    PersistentCharacterStateService,
    TimelineBranchService,
)
from character_evidence.client import ModalCharacterEvidenceProducer
from character_evidence.tracking import CharacterEvidenceTracker
from continuity_core import ContinuityDecisionEngine, FrameAnchorPlanner
from cost_core import CostEngine, CreditPricingEngine, TokenCostEngine
from creative_director_core import CreativeDirectorService
from deepseek_provider import DeepSeekProvider
from director_production import AgentOrchestrator, CandidatePipeline
from entitlement_core import (
    GenerationAdmissionService,
    LiveCanaryPermitService,
    ModelRoleRuntime,
    WorkspaceCreditService,
    WorkspaceModelResolver,
)
from episode_continuation_core import ContinuationContextBuilder, EpisodeContinuationService
from evaluation_core import GenerationEvaluator, RetryEngine
from generation_gateway import (
    DirectAPIResourceRegistry,
    FlowProjectAllocator,
    GenerationGateway,
    ProviderRouter,
)
from generation_gateway.retry import RetryPolicy
from generation_gateway.scheduler import AccountScheduler
from generation_policy_core import CapabilityResolver
from google_flow_provider import GoogleFlowProvider
from grok_provider import GrokProvider
from image_prompt_core import ImagePromptCorrector
from kling_provider import KlingProvider
from media_service import DirectUploadService, MediaRegistry, ThumbnailService
from memory_core import (
    ContextAssembler,
    ContextBudget,
    LocalTestEmbeddingProvider,
    ModelRoleEmbeddingProvider,
    MultimodalMemoryEngine,
)
from model_metrics_core import ModelBenchmarkSuite, ModelMetricsService
from model_registry_core import (
    ModelCapabilityRegistry,
    ModelInfrastructureService,
    VideoModelRouter,
)
from narrative_core import NarrativeCompiler
from narrative_ledger_core import NarrativeLedgerService, ShotDependencyService
from omni_provider import OmniProvider
from openrouter_provider import OpenRouterProvider
from payment_core import AlchemyUSDCWebhookService, DePayPaymentService, WalletPaymentService
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
from router_evidence_core.service import RouterObservationService
from runapi_provider import RunAPIEdgeProvider
from runtime_control_core import FeatureFlagDefaults, FeatureFlagService
from runway_provider import RunwayProvider
from seedance_provider import SeedanceProvider
from skill_core import PromptCompilerService, SkillRegistry
from sqlalchemy.engine import make_url
from style_core import ModelRoleSemanticStyleEmbedder, ProjectStyleService, StyleDriftMonitor
from veo_provider import VeoOfficialProvider
from video_adapter_core import VideoAdapterRegistry
from wan_provider import WanProvider

logger = logging.getLogger(__name__)


def _provider_media_credentials(settings: Settings) -> dict[str, str]:
    """Bearer tokens for providers that gate their own artefacts behind auth.

    Most providers hand back a signed CDN URL that carries its own
    authorization in the query string, and those must stay anonymous. OpenRouter
    does not: `GET /api/v1/videos/{id}/content` answers 401 without the API key,
    so a video generated there is billed and then unretrievable. Only providers
    with that demonstrated behaviour belong here -- presenting a key to a host
    that never asked for one is an exposure with nothing bought by it.
    """

    configured = {"openrouter": settings.openrouter_api_key}
    return {provider: key for provider, key in configured.items() if key.strip()}


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
    thumbnails: ThumbnailService
    direct_uploads: DirectUploadService
    runtime: BrowserRuntime
    providers: ProviderRouter
    provider_capabilities: ProviderCapabilityCatalog
    provider_budget: DatabaseProviderBudgetRepository
    direct_api_resources: DirectAPIResourceRegistry
    scheduler: AccountScheduler
    flow_affinity: FlowProjectAllocator
    continuity: ShotContinuityService
    gateway: GenerationGateway
    credentials: CredentialVault
    production: ProductionEngine
    skills: SkillRegistry
    agents: AgentRuntime
    narrative: NarrativeCompiler
    narrative_ledger: NarrativeLedgerService
    shot_dependencies: ShotDependencyService
    characters: CharacterIdentityService
    timeline_branches: TimelineBranchService
    character_states: PersistentCharacterStateService
    continuity_decision: ContinuityDecisionEngine
    frame_anchors: FrameAnchorPlanner
    capabilities: ModelCapabilityRegistry
    capability_resolver: CapabilityResolver
    qa: QAPipeline
    character_evidence_tracker: CharacterEvidenceTracker
    cost: CostEngine
    prompts: PromptCompilerService
    candidates: CandidatePipeline
    orchestrator: AgentOrchestrator
    model_registry: ModelCapabilityRegistry
    model_infrastructure: ModelInfrastructureService
    workspace_models: WorkspaceModelResolver
    model_roles: ModelRoleRuntime
    live_canary: LiveCanaryPermitService
    generation_admission: GenerationAdmissionService
    workspace_credits: WorkspaceCreditService
    alchemy_webhooks: AlchemyUSDCWebhookService
    wallet_payments: WalletPaymentService
    depay_payments: DePayPaymentService
    video_router: VideoModelRouter
    video_adapters: VideoAdapterRegistry
    image_prompts: ImagePromptCorrector
    asset_registry: AssetRegistry
    styles: ProjectStyleService
    style_drift: StyleDriftMonitor
    feature_flags: FeatureFlagService
    memory: MultimodalMemoryEngine
    context: ContextAssembler
    evaluator: GenerationEvaluator
    retry_engine: RetryEngine
    model_metrics: ModelMetricsService
    #: Wide production observations for the offline posterior. Distinct from
    #: `model_metrics`, which is unchanged and still feeds the adaptive router.
    router_observations: RouterObservationService
    benchmarks: ModelBenchmarkSuite
    visual_runtime: VisualProductionRuntime
    credit_pricing: CreditPricingEngine
    creative_director: CreativeDirectorService
    episode_continuations: EpisodeContinuationService

    @property
    def video_prompt_compiler(self) -> PromptCompilerService:
        """Compatibility view of the single unified Prompt Compiler."""

        return self.prompts


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
        # SQLite is not a smaller PostgreSQL here. Under pysqlite a
        # `begin_nested()` savepoint does not roll back with its enclosing
        # transaction, and seven call sites — the registry, renditions, the
        # gateway and provider affinity — depend on that rollback to keep a
        # failed step from being committed. A production database that silently
        # keeps half-applied work is not a performance problem, it is a
        # correctness one.
        backend = make_url(settings.database_url).get_backend_name()
        if backend != "postgresql":
            raise RuntimeError(
                f"production requires a PostgreSQL DATABASE_URL, not {backend}; "
                "SQLite savepoints do not roll back under pysqlite"
            )
    # Every guard above and this one read configuration only. They run before
    # the first connection is opened, so a misconfigured deployment is refused
    # for the reason it is actually misconfigured rather than for whichever
    # symptom the database happens to surface first.
    credentials = CredentialVault(
        settings.credential_encryption_key,
        allow_ephemeral_key=settings.deployment_environment in {"development", "test"},
    )
    if settings.deployment_environment == "production" and settings.character_evidence_enabled:
        # CHARACTER_EVIDENCE_ENABLED=false is the one sanctioned way to start
        # production without this service: an explicit, visible operator
        # decision (the Modal deployment is blocked on external HTTPS
        # reachability). With the switch on, everything below stays
        # fail-closed exactly as before.
        if not settings.character_evidence_base_url.startswith("https://"):
            raise RuntimeError("CHARACTER_EVIDENCE_BASE_URL must be configured as HTTPS in production")
        for name, value in (
            ("CHARACTER_EVIDENCE_API_KEY", settings.character_evidence_api_key),
            (
                "CHARACTER_EVIDENCE_CALLBACK_SIGNING_KEY",
                settings.character_evidence_callback_signing_key,
            ),
        ):
            raw = value.encode("utf-8")
            if len(raw) < 32 or len(set(raw)) < 16:
                raise RuntimeError(f"{name} must be a high-entropy secret of at least 32 bytes in production")
        if settings.character_evidence_operating_mode != "shadow":
            raise RuntimeError(
                "Character Evidence must remain in shadow mode until the versioned acceptance criteria pass"
            )
    database = Database(settings.database_url)
    if settings.deployment_environment == "test":
        # A per-test throwaway database cannot replay every revision. This is
        # the one place ORM metadata may build a schema, and it stamps what it
        # built so the check below asks the same question everywhere.
        database.create_all_and_stamp()
    database.require_schema_revision()
    alchemy_webhooks = AlchemyUSDCWebhookService(
        database,
        signing_key=settings.alchemy_webhook_signing_key,
        webhook_id=settings.alchemy_webhook_id,
        network=settings.alchemy_network,
        treasury_address=settings.alchemy_treasury_address,
        crediting_enabled=settings.alchemy_crediting_enabled,
        usdc_microunits_per_credit=settings.alchemy_usdc_microunits_per_credit,
    )
    wallet_payments = WalletPaymentService(
        database,
        network=settings.alchemy_network,
        treasury_address=settings.alchemy_treasury_address,
        usdc_microunits_per_credit=settings.alchemy_usdc_microunits_per_credit,
        challenge_ttl_seconds=settings.wallet_challenge_ttl_seconds,
        intent_ttl_minutes=settings.payment_intent_ttl_minutes,
    )
    depay_payments = DePayPaymentService(
        database,
        payment_link_url=settings.depay_payment_link_url,
        integration_id=settings.depay_integration_id,
        legacy_link_id=settings.depay_link_id,
        callback_public_key=settings.depay_callback_public_key,
        dynamic_config_private_key=settings.depay_dynamic_config_private_key,
        treasury_address=settings.alchemy_treasury_address,
        max_provider_fee_bps=settings.depay_max_provider_fee_bps,
        checkout_ttl_minutes=settings.depay_checkout_ttl_minutes,
    )
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
            addressing_style=settings.s3_addressing_style,
            enforce_checksum=settings.s3_enforce_upload_checksum,
        )
    else:
        storage = LocalStorage(
            settings.storage_root,
            settings.public_base_url,
            max_object_bytes=settings.max_upload_bytes,
            reference_signing_key=settings.local_reference_signing_key,
        )
    media = MediaRegistry(
        database,
        storage,
        provider_media_hosts=_parse_provider_media_hosts(settings.provider_media_allowed_hosts),
        provider_media_credentials=_provider_media_credentials(settings),
        max_download_bytes=min(settings.max_provider_download_bytes, settings.max_upload_bytes),
        max_image_pixels=settings.max_image_pixels,
        reference_url_ttl_seconds=settings.reference_url_ttl_seconds,
    )
    thumbnails = ThumbnailService(database, storage)
    direct_uploads = DirectUploadService(
        database,
        storage,
        max_upload_bytes=settings.max_upload_bytes,
        max_image_pixels=settings.max_image_pixels,
        ttl_seconds=settings.direct_upload_ttl_seconds,
        verify_sha256_on_complete=settings.s3_verify_upload_sha256_on_complete,
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
    model_infrastructure = ModelInfrastructureService(
        database,
        settings.model_infrastructure_config,
    )
    default_sync = model_infrastructure.ensure_defaults()
    model_registry = ModelCapabilityRegistry(database)
    providers = ProviderRouter(
        model_registry,
        allow_test_target_registration=settings.deployment_environment == "test",
    )
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
        image_model_envelopes=settings.openrouter_image_model_keys,
        image_quality=settings.openrouter_image_quality,
        video_generate_audio=settings.openrouter_video_generate_audio,
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
        video_model_keys=settings.wan_video_model_keys,
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
            ProviderCapability.IMAGE.value,
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
    newly_created_models = set(default_sync.model_names_created)

    def report_declared_model_id(logical_name: str, declared: str) -> None:
        """Say out loud when the environment and the registry disagree.

        The registry wins — an operator override of `provider_model_id` has to
        survive a restart, and a test pins that. The failure this guards against
        is not the registry winning, it is nobody noticing: a corrected ID in
        `.env` that never reached the row, and a model that goes on submitting a
        name the provider does not have.
        """

        stored = model_infrastructure.declared_model_id_divergence(logical_name, declared)
        if stored is not None:
            logger.warning(
                "model %s: environment declares provider model id %r but the registry holds %r; "
                "the registry is authoritative — correct it deliberately if the environment is right",
                logical_name,
                declared.strip(),
                stored,
            )

    workspace_models = WorkspaceModelResolver(database, model_infrastructure)
    live_canary = LiveCanaryPermitService(database)
    model_roles = ModelRoleRuntime(
        database,
        workspace_models,
        provider_capabilities,
        provider_mode=settings.provider_mode,
        live_canary=live_canary,
        # Token-billing providers report counts, not cost. This prices live
        # chat/embedding holds and settlements from the canonical list rates,
        # so a multi-request permit stops degrading into a one-call permit.
        token_costs=TokenCostEngine(database),
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

    doubao_default = defaults_by_name["doubao-free-reasoner"]
    report_declared_model_id(doubao_default.logical_name, settings.doubao_model_id)
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
    report_declared_model_id(seedance_default.logical_name, settings.seedance_model_id)
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

    runapi_default = defaults_by_name["runapi-prompt-refiner-edge"]
    report_declared_model_id(runapi_default.logical_name, settings.runapi_model_id)
    if runapi_default.logical_name in newly_created_models and settings.runapi_model_id.strip():
        runapi_ready = bool(settings.runapi_api_key.strip() and settings.runapi_base_url.strip())
        model_infrastructure.configure_runtime_model(
            runapi_default.logical_name,
            settings.runapi_model_id,
            enabled=runapi_ready,
            live_enabled=bool(runapi_ready and live_gate_ready and settings.allow_runapi_edge_calls),
        )

    for profile in model_registry.all(include_disabled=True):
        if profile.modality != "video" or profile.provider in {
            "openrouter",
            "runapi",
            "seedance",
            "wan",
        }:
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
    openrouter_video_models = {
        provider_model_id
        for provider_name, provider_model_id, enabled in runtime_video_routes
        if provider_name == "openrouter" and enabled
    }
    # The project's image model. Registered explicitly, like the Flow image
    # model, so only a reviewed ID can reach a provider transport.
    image_default = defaults_by_name["gpt-image-2-openrouter"]
    report_declared_model_id(image_default.logical_name, settings.openrouter_image_model_id)
    if image_default.logical_name in newly_created_models and settings.openrouter_image_model_id.strip():
        image_ready = bool(settings.openrouter_api_key.strip() and settings.openrouter_base_url.strip())
        model_infrastructure.configure_runtime_model(
            image_default.logical_name,
            settings.openrouter_image_model_id,
            enabled=image_ready,
            live_enabled=bool(image_ready and live_gate_ready),
            provider_trust_level=ProviderTrustLevel.PRODUCTION,
            criticality_allowed=list(AssetCriticality),
        )
    openrouter_image_runtime = model_infrastructure.runtime_model(image_default.logical_name)
    openrouter_image_available = openrouter_image_runtime.enabled and openrouter.configured
    providers.register_model(
        "openrouter",
        openrouter_image_runtime.provider_model_id,
        "image",
        available=openrouter_image_available,
    )
    seedream_runtime = model_infrastructure.runtime_model("seedream-5.0-ark")
    providers.register_model(
        "seedance",
        seedream_runtime.provider_model_id,
        "image",
        available=bool(seedream_runtime.enabled and seedance.configured),
    )

    # Last, not straight after `ensure_defaults()`. The block above rewrites
    # `provider_model_id` for every model an operator has declared, and a status
    # derived before those writes describes a row that no longer exists. That is
    # not hypothetical, and it goes wrong in both directions: Wan's row moved to
    # the ID `WAN2_7_T2V_MODEL_ID` names while its price stayed keyed on the
    # family key, so it reported VERIFIED and refused at the till; Doubao's row
    # moved off `CONFIGURE_DOUBAO_MODEL_ID` onto a priced ID and reported
    # UNVERIFIED. The report and the till have to read the same row.
    model_infrastructure.reconcile_pricing_status()

    openrouter_capabilities = {"video"} if openrouter_video_models else set()
    openrouter_models = set(openrouter_video_models)
    if openrouter_image_available:
        openrouter_capabilities.add("image")
        openrouter_models.add(openrouter_image_runtime.provider_model_id)
    if openrouter.configured and openrouter_models:
        direct_api_resources.ensure_provider(
            "openrouter",
            supported_models=openrouter_models,
            capabilities=openrouter_capabilities,
        )
    seedream_available = seedream_runtime.enabled and seedance.configured
    if seedance_available or seedream_available:
        # One Ark credential serves both the Seedance video model and the
        # Seedream image model; the scheduler capacity must say so or image
        # jobs die in RETRY_WAIT with "no ready seedance account" — observed
        # live on 2026-08-30 when only the video model was registered here.
        seedance_direct_models: set[str] = set()
        seedance_direct_capabilities: set[str] = set()
        if seedance_available:
            seedance_direct_models.add(seedance_runtime.provider_model_id)
            seedance_direct_capabilities.add("video")
        if seedream_available:
            seedance_direct_models.add(seedream_runtime.provider_model_id)
            seedance_direct_capabilities.add("image")
        direct_api_resources.ensure_provider(
            "seedance",
            supported_models=seedance_direct_models,
            capabilities=seedance_direct_capabilities,
        )
    if wan.configured and wan_models:
        direct_api_resources.ensure_provider(
            "wan",
            supported_models=wan_models,
            capabilities={"video"},
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
    flow_affinity = FlowProjectAllocator(database, scheduler)
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
        flow_affinity=flow_affinity,
        live_canary=live_canary,
    )
    production = ProductionEngine(database)
    skills = SkillRegistry(settings.skills_root)
    agents = AgentRuntime(production, gateway, media, skills)
    narrative = NarrativeCompiler(database)
    narrative_ledger = NarrativeLedgerService(database)
    shot_dependencies = ShotDependencyService(database)
    characters = CharacterIdentityService(database)
    timeline_branches = TimelineBranchService(database)
    character_states = PersistentCharacterStateService(database)
    continuity_decision = ContinuityDecisionEngine(database)
    frame_anchors = FrameAnchorPlanner(database, continuity_decision)
    capabilities = model_registry
    capability_resolver = CapabilityResolver(database, model_registry)
    evidence_producer = (
        ModalCharacterEvidenceProducer(
            database,
            media,
            base_url=settings.character_evidence_base_url,
            api_key=settings.character_evidence_api_key,
            threshold_version=settings.character_evidence_threshold_version,
            timeout_seconds=settings.character_evidence_http_timeout_seconds,
        )
        if settings.deployment_environment == "production" and settings.character_evidence_enabled
        else None
    )
    qa = QAPipeline(database, evidence_producer=evidence_producer)
    character_evidence_tracker = CharacterEvidenceTracker(
        database,
        qa,
        threshold_version=settings.character_evidence_threshold_version,
        callback_timeout_seconds=settings.character_evidence_callback_timeout_seconds,
        max_submission_attempts=settings.character_evidence_max_submission_attempts,
        backfill_hours=settings.character_evidence_backfill_hours,
    )
    cost = CostEngine(database)
    # The compiler owns style-lock enforcement, so it must hold the
    # authoritative style service rather than trust a caller's context dict.
    #
    # Layer 2 is a deployment-wide switch rather than a per-project flag: it
    # changes what "committable" means, and a gate that is quietly stronger on
    # some projects than others is not a gate.
    semantic_style = (
        ModelRoleSemanticStyleEmbedder(model_roles) if settings.feature_semantic_style_lock else None
    )
    styles = ProjectStyleService(database, storage, semantic=semantic_style)
    style_drift = StyleDriftMonitor(database)
    prompts = PromptCompilerService(
        database,
        skills,
        styles,
        ledger=narrative_ledger,
        dependencies=shot_dependencies,
    )
    credit_pricing = CreditPricingEngine(
        model_registry,
        database=database,
        # Only a route that can actually be billed has to fail closed. Mock and
        # recorded modes keep the seeded placeholder so development and the
        # offline suite still run, and every estimate reports which it used.
        require_verified_pricing=settings.provider_mode == "live",
    )
    generation_admission = GenerationAdmissionService(
        workspace_models,
        model_roles,
        credit_pricing,
        free_plan_max_images=settings.free_plan_max_images,
    )
    video_router = VideoModelRouter(
        model_registry,
        require_live_lifecycle=settings.provider_mode == "live",
    )
    video_adapters = VideoAdapterRegistry()
    image_prompts = ImagePromptCorrector()
    asset_registry = AssetRegistry(database)
    feature_flags = FeatureFlagService(
        database,
        FeatureFlagDefaults(
            voyage_memory=settings.feature_voyage_memory,
            auto_evaluation=settings.feature_auto_evaluation,
            adaptive_router=settings.feature_adaptive_router,
            router_lcb=settings.feature_router_lcb,
            auto_retry=settings.feature_auto_retry,
        ),
    )
    # Product/recorded embedding calls resolve the project-scoped
    # MULTIMODAL_EMBEDDING role.  Mock development keeps a deterministic local
    # vectorizer so ordinary tests and offline generation never need a fixture or
    # network.  A Voyage key is therefore not consumed directly by business code.
    embeddings = (
        LocalTestEmbeddingProvider(settings.memory_embedding_dimension)
        if settings.provider_mode == "mock"
        else ModelRoleEmbeddingProvider(
            model_roles,
            dimension=settings.memory_embedding_dimension,
        )
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
    evaluator = GenerationEvaluator(database)
    retry_engine = RetryEngine(settings.max_auto_retries)
    model_metrics = ModelMetricsService(database)
    router_observations = RouterObservationService(database)
    benchmarks = ModelBenchmarkSuite(database)
    visual_runtime = VisualProductionRuntime(
        database,
        gateway,
        asset_registry,
        memory,
        context,
        video_router,
        video_adapters,
        prompts,
        evaluator,
        retry_engine,
        model_metrics,
        benchmarks,
        feature_flags,
        generation_admission,
        styles,
        router_observations,
        dependencies=shot_dependencies,
        narrative_ledger=narrative_ledger,
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
        character_states=character_states,
        styles=styles,
        frame_anchors=frame_anchors,
        characters=characters,
        narrative_ledger=narrative_ledger,
        shot_dependencies=shot_dependencies,
    )
    orchestrator = AgentOrchestrator(
        narrative,
        characters,
        continuity_decision,
        prompts,
        candidates,
        frame_anchors=frame_anchors,
    )
    # Both upper-level services reuse the chain below them: the creative
    # director compiles through the orchestrator and writes the ledger, the
    # continuation service compiles through the same narrative compiler and
    # plans through the same frame anchor planner. Neither owns a provider
    # path - paid work is emitted as structured actions the API executes
    # through admission and the visual runtime.
    creative_director = CreativeDirectorService(
        database,
        orchestrator=orchestrator,
        ledger=narrative_ledger,
        model_roles=model_roles,
        free_plan_turn_limit=settings.free_plan_max_director_turns,
    )
    episode_continuations = EpisodeContinuationService(
        database,
        context_builder=ContinuationContextBuilder(database, narrative_ledger),
        narrative=narrative,
        frame_anchors=frame_anchors,
        ledger=narrative_ledger,
        model_roles=model_roles,
    )
    return Container(
        settings=settings,
        database=database,
        storage=storage,
        media=media,
        thumbnails=thumbnails,
        direct_uploads=direct_uploads,
        runtime=runtime,
        providers=providers,
        provider_capabilities=provider_capabilities,
        provider_budget=provider_budget,
        direct_api_resources=direct_api_resources,
        scheduler=scheduler,
        flow_affinity=flow_affinity,
        continuity=continuity,
        gateway=gateway,
        credentials=credentials,
        production=production,
        skills=skills,
        agents=agents,
        narrative=narrative,
        narrative_ledger=narrative_ledger,
        shot_dependencies=shot_dependencies,
        characters=characters,
        timeline_branches=timeline_branches,
        character_states=character_states,
        continuity_decision=continuity_decision,
        frame_anchors=frame_anchors,
        capabilities=capabilities,
        capability_resolver=capability_resolver,
        qa=qa,
        character_evidence_tracker=character_evidence_tracker,
        cost=cost,
        prompts=prompts,
        candidates=candidates,
        orchestrator=orchestrator,
        model_registry=model_registry,
        model_infrastructure=model_infrastructure,
        workspace_models=workspace_models,
        model_roles=model_roles,
        live_canary=live_canary,
        generation_admission=generation_admission,
        workspace_credits=workspace_credits,
        alchemy_webhooks=alchemy_webhooks,
        wallet_payments=wallet_payments,
        depay_payments=depay_payments,
        video_router=video_router,
        video_adapters=video_adapters,
        image_prompts=image_prompts,
        asset_registry=asset_registry,
        styles=styles,
        style_drift=style_drift,
        feature_flags=feature_flags,
        memory=memory,
        context=context,
        evaluator=evaluator,
        retry_engine=retry_engine,
        model_metrics=model_metrics,
        router_observations=router_observations,
        benchmarks=benchmarks,
        visual_runtime=visual_runtime,
        credit_pricing=credit_pricing,
        creative_director=creative_director,
        episode_continuations=episode_continuations,
    )
