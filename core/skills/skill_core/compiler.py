from __future__ import annotations

import json
import re
from typing import Any

from platform_database import Database
from production_domain.models import PromptCompilation, Shot, TimelineState


class PromptCompilerService:
    version = "prompt-compiler-v2-compat"

    def __init__(self, database: Database, skills) -> None:  # type: ignore[no-untyped-def]
        self.database = database
        self.skills = skills

    @staticmethod
    def _normalize_approved_action(raw_prompt: str) -> str:
        """Normalize internal shot text without performing user-visible image correction."""

        compact = re.sub(r"\s+", " ", raw_prompt).strip()
        return re.sub(r"[。.!！]{2,}", "。", compact)

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
        # Provider/model are retained in the compatibility signature only. The
        # canonical video compiler and adapters now own provider-specific wording.
        del provider, model
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise LookupError("shot not found")
            state = session.get(TimelineState, shot.input_state_id) if shot.input_state_id else None
            user_prompt = shot.user_prompt or shot.prompt
            approved_action = self._normalize_approved_action(user_prompt)
            state_json = state.state_json if state else {}
            identity_lines: list[str] = []
            for binding in character_bindings:
                identity_lines.append(
                    f"Character identity {binding['identity_version_id']} must remain unchanged; "
                    f"canonical assets: {', '.join(binding.get('canonical_assets', []))}."
                )
                if binding.get("narrative_state_version_id"):
                    current_state = binding.get("narrative_state", {})
                    proposed_state = binding.get("proposed_narrative_state")
                    identity_lines.append(
                        "Persistent narrative state "
                        f"{binding['narrative_state_version_id']} "
                        f"(hash={binding.get('narrative_state_hash')}): "
                        + json.dumps(
                            current_state,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "."
                    )
                    if proposed_state is not None:
                        identity_lines.append(
                            "Approved mutable narrative-state target for this shot "
                            f"(hash={binding.get('proposed_narrative_state_hash')}; "
                            f"changed_paths={binding.get('proposed_state_changed_paths', [])}): "
                            + json.dumps(
                                proposed_state,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            + ". Render this target exactly; it remains proposed until visual QA and commit."
                        )
                    else:
                        identity_lines.append(
                            "No state delta is approved for this character; preserve the persistent "
                            "narrative state and every active continuity constraint exactly."
                        )
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
            compiled = "\n".join(
                [
                    *identity_lines,
                    scene_line,
                    camera_line,
                    lighting_line,
                    f"Approved dominant action: {approved_action}",
                    "End state must match the approved output state. No extra actions, people, props, cuts, "
                    "identity changes, or unapproved direct gaze into the lens.",
                ]
            )
            compilation = PromptCompilation(
                project_id=shot.scene.episode.project_id,
                shot_id=shot.id,
                user_prompt=user_prompt,
                compiled_prompt=compiled,
                compiler_version=self.version,
                skill_versions={
                    "director": "v2",
                    "cinematography": "v2",
                    "continuity": "v2",
                    "prompt-compiler": "v4-character-state-target",
                },
                diff_json={
                    "changes": (
                        [
                            {
                                "type": "NORMALIZE_INTERNAL_ACTION",
                                "before": user_prompt,
                                "after": approved_action,
                            }
                        ]
                        if approved_action != user_prompt
                        else []
                    ),
                    "preserved_facts": [approved_action],
                },
            )
            session.add(compilation)
            shot.compiled_prompt = compiled
            session.flush()
            return compilation
