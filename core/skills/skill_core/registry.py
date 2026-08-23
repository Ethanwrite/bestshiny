from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]


class SkillRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    category: str
    description: str
    system_prompt: str
    version: str
    content_hash: str
    path: str
    metadata: dict[str, Any]

    @property
    def content(self) -> str:
        """Compatibility alias for callers that previously read raw Skill content."""

        return self.system_prompt


class SkillRegistry:
    """Filesystem-authoritative, content-versioned Skill registry."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    @staticmethod
    def _parse(path: Path) -> SkillDefinition:
        raw = path.read_text("utf-8")
        if not raw.startswith("---\n"):
            raise SkillRegistryError(f"Skill frontmatter is required: {path}")
        try:
            _opening, frontmatter, body = raw.split("---", 2)
        except ValueError as exc:
            raise SkillRegistryError(f"Skill frontmatter is not closed: {path}") from exc
        parsed = yaml.safe_load(frontmatter) or {}
        if not isinstance(parsed, dict):
            raise SkillRegistryError(f"Skill frontmatter must be an object: {path}")
        name = str(parsed.get("name") or "").strip()
        description = str(parsed.get("description") or "").strip()
        if not name or not description:
            raise SkillRegistryError(f"Skill name and description are required: {path}")
        if name != path.parent.name:
            raise SkillRegistryError(
                f"Skill name must match its directory ({path.parent.name}): {path}"
            )
        system_prompt = body.strip()
        if not system_prompt:
            raise SkillRegistryError(f"Skill body is empty: {path}")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        metadata = parsed.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise SkillRegistryError(f"Skill metadata must be an object: {path}")
        version = f"sha256:{digest[:16]}"
        return SkillDefinition(
            name=name,
            category=str(metadata.get("category") or name),
            description=description,
            system_prompt=system_prompt,
            version=version,
            content_hash=digest,
            path=str(path),
            metadata=dict(metadata),
        )

    def list_skills(self) -> list[SkillDefinition]:
        if not self.root.is_dir():
            return []
        return [self._parse(path) for path in sorted(self.root.glob("*/SKILL.md"))]

    def resolve(self, name: str) -> SkillDefinition:
        normalized = name.strip()
        matches = [skill for skill in self.list_skills() if skill.name == normalized]
        if not matches:
            raise LookupError(f"Skill not found: {name}")
        if len(matches) > 1:  # pragma: no cover - directory layout prevents this defensively.
            raise SkillRegistryError(f"Skill name is ambiguous: {name}")
        return matches[0]

    def prompt_context(self, categories: list[str]) -> str:
        selected = [skill.system_prompt for skill in self.list_skills() if skill.category in categories]
        return "\n\n".join(selected)
