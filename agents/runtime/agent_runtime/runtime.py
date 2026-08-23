from __future__ import annotations

from generation_gateway import GenerationGateway
from media_service import MediaRegistry
from production_engine import ProductionEngine
from skill_core.registry import SkillRegistry


class AgentRuntime:
    """Agents receive only domain services; a provider client is intentionally not exposed."""

    def __init__(
        self,
        production: ProductionEngine,
        gateway: GenerationGateway,
        media: MediaRegistry,
        skills: SkillRegistry,
    ):
        self.production = production
        self.gateway = gateway
        self.media = media
        self.skills = skills

    def prepare_shot_generation(self, shot_id: str, idempotency_key: str):  # type: ignore[no-untyped-def]
        request = self.production.request_for_shot(shot_id, idempotency_key)
        return self.gateway.create(request)
