from __future__ import annotations

from dataclasses import dataclass

from agent_runtime import AgentRuntime, SkillRegistry
from browser_runtime import BrowserRuntime
from generation_gateway import GenerationGateway, ProviderRouter
from generation_gateway.retry import RetryPolicy
from generation_gateway.scheduler import AccountScheduler
from google_flow_provider import GoogleFlowProvider
from grok_provider import GrokProvider
from media_service import MediaRegistry
from omni_provider import OmniProvider
from platform_database import Database
from platform_shared import CredentialVault, LocalStorage, Settings
from production_engine import ProductionEngine, ShotContinuityService
from seedance_provider import SeedanceProvider
from veo_provider import VeoOfficialProvider


@dataclass
class Container:
    settings: Settings
    database: Database
    storage: LocalStorage
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


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or Settings()
    database = Database(settings.database_url)
    database.create_all()
    storage = LocalStorage(settings.storage_root, settings.public_base_url)
    media = MediaRegistry(database, storage)
    runtime = BrowserRuntime(database, heartbeat_timeout_seconds=settings.worker_heartbeat_timeout_seconds)
    providers = ProviderRouter()
    providers.register(GoogleFlowProvider(runtime, settings, database))
    providers.register(SeedanceProvider())
    providers.register(VeoOfficialProvider())
    providers.register(GrokProvider())
    providers.register(OmniProvider())
    scheduler = AccountScheduler(database)
    continuity = ShotContinuityService(database, media)
    gateway = GenerationGateway(database, providers, media, scheduler, continuity, RetryPolicy())
    credentials = CredentialVault(settings.credential_encryption_key)
    production = ProductionEngine(database)
    skills = SkillRegistry(settings.skills_root)
    agents = AgentRuntime(production, gateway, media, skills)
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
    )
