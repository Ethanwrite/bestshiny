from __future__ import annotations

from dataclasses import dataclass

from agent_runtime import AgentRuntime, SkillRegistry
from browser_runtime import BrowserRuntime
from character_core import CharacterIdentityService
from continuity_core import ContinuityDecisionEngine
from cost_core import CostEngine
from director_production import AgentOrchestrator, CandidatePipeline
from generation_gateway import GenerationGateway, ProviderRouter
from generation_gateway.retry import RetryPolicy
from generation_gateway.scheduler import AccountScheduler
from generation_policy_core import CapabilityResolver, ProviderCapabilityRegistry
from google_flow_provider import GoogleFlowProvider
from grok_provider import GrokProvider
from kling_provider import KlingProvider
from media_service import MediaRegistry
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
from qa_core import QAPipeline
from runway_provider import RunwayProvider
from seedance_provider import SeedanceProvider
from skill_core import PromptCompilerService
from veo_provider import VeoOfficialProvider


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


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
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
        )
    else:
        storage = LocalStorage(settings.storage_root, settings.public_base_url)
    media = MediaRegistry(database, storage)
    runtime = BrowserRuntime(database, heartbeat_timeout_seconds=settings.worker_heartbeat_timeout_seconds)
    providers = ProviderRouter()
    providers.register(GoogleFlowProvider(runtime, settings, database))
    providers.register(SeedanceProvider())
    providers.register(VeoOfficialProvider())
    providers.register(GrokProvider())
    providers.register(OmniProvider())
    providers.register(KlingProvider())
    providers.register(RunwayProvider())
    scheduler = AccountScheduler(database)
    continuity = ShotContinuityService(database, media)
    gateway = GenerationGateway(database, providers, media, scheduler, continuity, RetryPolicy())
    credentials = CredentialVault(settings.credential_encryption_key)
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
    candidates = CandidatePipeline(database, gateway, prompts, capability_resolver, qa, cost, continuity)
    orchestrator = AgentOrchestrator(narrative, characters, continuity_decision, prompts, candidates)
    return Container(
        settings,
        database,
        storage,
        media,
        runtime,
        providers,
        scheduler,
        continuity,
        gateway,
        credentials,
        production,
        skills,
        agents,
        narrative,
        characters,
        continuity_decision,
        capabilities,
        capability_resolver,
        qa,
        cost,
        prompts,
        candidates,
        orchestrator,
    )
