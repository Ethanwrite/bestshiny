"""Filesystem-authoritative, content-versioned Skill registry.

A Skill is more than a Markdown file: it is bounded authority, a runtime
binding, a versioned instruction and a structured contract. The frontmatter
carries the machine-readable half of that - the pipeline stage, the runtime
operations the Skill answers for, the model role it executes under, what it
may decide and what it must never decide - so the runtime can resolve, bind
and audit a Skill without reading its prose. The body is the instruction the
model actually receives; its SHA-256 is the version.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

#: The sections every industrialised Skill body must carry, in order of the
#: contract they document. Checked by the installed-skills gate and by
#: ``SkillRuntime.validate``; the parser itself tolerates a partial body so
#: a candidate can still be reviewed and reported on.
REQUIRED_SECTIONS: tuple[str, ...] = (
    "Purpose",
    "Pipeline Position",
    "Inputs",
    "Authority",
    "Forbidden Authority",
    "Invariants",
    "Decision Rules",
    "Escalation Rules",
    "Output Contract",
    "Unresolved Policy",
)

RUNTIME_KINDS = ("model", "reference")

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)


class SkillRegistryError(ValueError):
    pass


def _string_list(value: Any, *, what: str, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SkillRegistryError(f"Skill metadata {what} must be a list of strings: {path}")
    cleaned = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
    return cleaned


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
    #: Machine-readable runtime binding. ``role`` is the pipeline role the
    #: Skill fulfils (``shot_planner`` for the ``short-drama`` Skill), ``stage``
    #: the pipeline stage, ``operations`` the runtime operations it answers
    #: for, ``model_role`` the model-registry role it executes under.
    role: str = ""
    stage: str = ""
    operations: tuple[str, ...] = ()
    model_role: str | None = None
    #: ``model`` when the body is meant to be injected into a model call;
    #: ``reference`` when the Skill is advisory material folded into another
    #: stage (``bound_to``) or a user-facing tool that runs deterministically.
    runtime_kind: str = "reference"
    bound_to: str | None = None
    authority: tuple[str, ...] = ()
    forbidden_authority: tuple[str, ...] = ()
    output_contract: str = ""
    sections: tuple[str, ...] = field(default_factory=tuple)

    @property
    def content(self) -> str:
        """Compatibility alias for callers that previously read raw Skill content."""

        return self.system_prompt

    @property
    def missing_sections(self) -> tuple[str, ...]:
        present = {section.casefold() for section in self.sections}
        return tuple(section for section in REQUIRED_SECTIONS if section.casefold() not in present)

    @property
    def is_runtime(self) -> bool:
        return self.runtime_kind == "model" and bool(self.operations)

    def snapshot(self) -> dict[str, Any]:
        """The identity every invocation records: what was loaded, which version."""

        return {
            "name": self.name,
            "version": self.version,
            "content_hash": self.content_hash,
            "role": self.role,
            "stage": self.stage,
        }

    def binding_json(self) -> dict[str, Any]:
        return {
            **self.snapshot(),
            "category": self.category,
            "operations": list(self.operations),
            "model_role": self.model_role,
            "runtime": self.runtime_kind,
            "bound_to": self.bound_to,
            "authority": list(self.authority),
            "forbidden_authority": list(self.forbidden_authority),
            "output_contract": self.output_contract,
            "missing_sections": list(self.missing_sections),
        }


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
        runtime_kind = str(metadata.get("runtime") or "reference").strip().lower()
        if runtime_kind not in RUNTIME_KINDS:
            raise SkillRegistryError(
                f"Skill metadata runtime must be one of {', '.join(RUNTIME_KINDS)}: {path}"
            )
        operations = _string_list(metadata.get("operations"), what="operations", path=path)
        authority = _string_list(metadata.get("authority"), what="authority", path=path)
        forbidden = _string_list(
            metadata.get("forbidden_authority"), what="forbidden_authority", path=path
        )
        overlap = sorted(set(authority) & set(forbidden))
        if overlap:
            raise SkillRegistryError(
                f"Skill authority and forbidden_authority overlap ({', '.join(overlap)}): {path}"
            )
        model_role = metadata.get("model_role")
        model_role = str(model_role).strip().upper() if model_role else None
        if runtime_kind == "model" and operations and not model_role:
            raise SkillRegistryError(
                f"a model-runtime Skill with operations must declare model_role: {path}"
            )
        if runtime_kind == "reference" and operations:
            raise SkillRegistryError(
                f"a reference Skill cannot claim runtime operations ({', '.join(operations)}): {path}"
            )
        bound_to = metadata.get("bound_to")
        return SkillDefinition(
            name=name,
            category=str(metadata.get("category") or name),
            description=description,
            system_prompt=system_prompt,
            version=version,
            content_hash=digest,
            path=str(path),
            metadata=dict(metadata),
            role=str(metadata.get("role") or name.replace("-", "_")).strip(),
            stage=str(metadata.get("stage") or "").strip().upper(),
            operations=operations,
            model_role=model_role,
            runtime_kind=runtime_kind,
            bound_to=str(bound_to).strip() if bound_to else None,
            authority=authority,
            forbidden_authority=forbidden,
            output_contract=str(metadata.get("output_contract") or "").strip(),
            sections=tuple(match.group(1).strip() for match in _HEADING.finditer(system_prompt)),
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

    def resolve_operation(self, operation: str) -> SkillDefinition:
        """The one Skill that answers for a runtime operation; never several."""

        wanted = str(operation).strip().lower()
        matches = [
            skill for skill in self.list_skills() if skill.is_runtime and wanted in skill.operations
        ]
        if not matches:
            raise LookupError(f"no installed Skill is bound to operation {operation!r}")
        if len(matches) > 1:
            raise SkillRegistryError(
                f"operation {operation!r} is bound to more than one Skill: "
                + ", ".join(sorted(skill.name for skill in matches))
            )
        return matches[0]

    def validate_bindings(self) -> list[str]:
        """Registry-wide invariants: one Skill per operation, one Skill per runtime role."""

        problems: list[str] = []
        by_operation: dict[str, list[str]] = {}
        by_role: dict[str, list[str]] = {}
        for skill in self.list_skills():
            if not skill.is_runtime:
                continue
            for operation in skill.operations:
                by_operation.setdefault(operation, []).append(skill.name)
            by_role.setdefault(skill.role, []).append(skill.name)
        for operation, names in sorted(by_operation.items()):
            if len(names) > 1:
                problems.append(f"operation {operation} is bound to {', '.join(sorted(names))}")
        for role, names in sorted(by_role.items()):
            if len(names) > 1:
                problems.append(f"runtime role {role} is claimed by {', '.join(sorted(names))}")
        return problems

    def prompt_context(self, categories: list[str]) -> str:
        selected = [skill.system_prompt for skill in self.list_skills() if skill.category in categories]
        return "\n\n".join(selected)
