from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from generation_gateway import GenerationGateway
from media_service import MediaRegistry
from production_engine import ProductionEngine


@dataclass(frozen=True)
class SkillDefinition:
    category: str
    name: str
    content: str
    path: str


class SkillRegistry:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def list_skills(self) -> list[SkillDefinition]:
        skills: list[SkillDefinition] = []
        if not self.root.is_dir():
            return skills
        for path in sorted(self.root.glob("*/SKILL.md")):
            skills.append(
                SkillDefinition(path.parent.name, path.parent.name, path.read_text("utf-8"), str(path))
            )
        return skills

    def prompt_context(self, categories: list[str]) -> str:
        selected = [skill.content for skill in self.list_skills() if skill.category in categories]
        return "\n\n".join(selected)


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
