from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from platform_database import Database
from production_domain.models import PromptCompilation, Shot, TimelineState


@dataclass(frozen=True)
class PromptRefinement:
    original: str
    refined: str
    changes: list[dict[str, str]]
    preserved_facts: list[str]


class PromptCompilerService:
    version = "prompt-compiler-v1"

    def __init__(self, database: Database, skills) -> None:  # type: ignore[no-untyped-def]
        self.database = database
        self.skills = skills

    @staticmethod
    def refine(raw_prompt: str) -> PromptRefinement:
        compact = re.sub(r"\s+", " ", raw_prompt).strip()
        compact = re.sub(r"[。.!！]{2,}", "。", compact)
        changes = []
        if compact != raw_prompt:
            changes.append({"type": "NORMALIZE_WHITESPACE", "before": raw_prompt, "after": compact})
        return PromptRefinement(raw_prompt, compact, changes, [compact])

    def compile_shot(
        self,
        shot_id: str,
        *,
        provider: str,
        model: str,
        character_bindings: list[dict[str, Any]] | None = None,
        scene_bindings: list[str] | None = None,
        camera: dict[str, Any] | None = None,
        lighting: dict[str, Any] | None = None,
    ) -> PromptCompilation:
        character_bindings = character_bindings or []
        scene_bindings = scene_bindings or []
        camera = camera or {}
        lighting = lighting or {}
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise LookupError("shot not found")
            state = session.get(TimelineState, shot.input_state_id) if shot.input_state_id else None
            user_prompt = shot.user_prompt or shot.prompt
            refined = self.refine(user_prompt)
            state_json = state.state_json if state else {}
            identity_lines = [
                f"Character identity {binding['identity_version_id']} must remain unchanged; "
                f"canonical assets: {', '.join(binding.get('canonical_assets', []))}."
                for binding in character_bindings
            ]
            scene_line = (
                f"Scene references: {', '.join(scene_bindings)}."
                if scene_bindings
                else f"Scene state: {state_json.get('scene', {})}."
            )
            camera_line = (
                "Camera: " + ", ".join(f"{key}={value}" for key, value in camera.items())
                if camera
                else "Camera: one dominant physically possible movement; preserve the approved screen axis."
            )
            lighting_line = (
                "Lighting: " + ", ".join(f"{key}={value}" for key, value in lighting.items())
                if lighting
                else "Lighting: preserve the established direction, time of day, and contrast."
            )
            provider_line = {
                "google_flow": "Use explicit start/end positions and exactly one subject trajectory.",
                "veo_official": "Use concise spatial language and one continuous physically possible action.",
                "grok": "Do not let any character look into the camera at the end.",
                "seedance": "Keep this as one highlight action; do not compress narrative beats.",
                "omni": "Use short literal instructions and one movement only.",
            }.get(provider, "Follow the approved action literally and preserve all bindings.")
            compiled = "\n".join(
                [
                    *identity_lines,
                    scene_line,
                    camera_line,
                    lighting_line,
                    f"Approved dominant action: {refined.refined}",
                    f"Provider constraint ({provider}/{model}): {provider_line}",
                    "End state must match the approved output state. No extra actions, people, props, cuts, "
                    "identity changes, or direct gaze into the lens.",
                ]
            )
            compilation = PromptCompilation(
                project_id=shot.scene.episode.project_id,
                shot_id=shot.id,
                user_prompt=user_prompt,
                compiled_prompt=compiled,
                compiler_version=self.version,
                skill_versions={
                    "director": "v1",
                    "cinematography": "v1",
                    "continuity": "v1",
                    "prompt-compiler": "v1",
                },
                diff_json={"changes": refined.changes, "preserved_facts": refined.preserved_facts},
            )
            session.add(compilation)
            shot.compiled_prompt = compiled
            shot.prompt = compiled
            session.flush()
            return compilation
