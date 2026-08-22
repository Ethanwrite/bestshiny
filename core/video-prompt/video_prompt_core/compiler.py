from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from platform_contracts import (
    CanonicalCameraSpec,
    CanonicalLightingSpec,
    CanonicalShotSpec,
    CanonicalSubjectSpec,
)
from platform_database import Database
from production_domain.models import PromptCompilation, Shot, TimelineState


@dataclass(frozen=True)
class VideoPromptCompilation:
    spec: CanonicalShotSpec
    neutral_prompt: str
    record_id: str


class VideoShotPromptCompiler:
    """Compiles approved shot facts into a provider-neutral canonical specification."""

    version = "video-shot-prompt-compiler-v1"

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _single_action(value: str) -> str:
        compact = re.sub(r"\s+", " ", value).strip()
        return re.sub(r"[。.!！]{2,}", "。", compact)

    @staticmethod
    def _profile(
        shot_type: str, dialogue: str
    ) -> Literal["generic", "action", "commercial_hero", "dialogue"]:
        normalized = shot_type.upper()
        if dialogue or "DIALOG" in normalized:
            return "dialogue"
        if any(token in normalized for token in ("PRODUCT", "COMMERCIAL", "HERO")):
            return "commercial_hero"
        if any(token in normalized for token in ("ACTION", "RUN", "FIGHT")):
            return "action"
        return "generic"

    @staticmethod
    def _camera_gaze_requested(action: str, start_state: dict[str, Any], end_state: dict[str, Any]) -> bool:
        state_text = json.dumps({"start": start_state, "end": end_state}, ensure_ascii=False)
        text = f"{action} {state_text}"
        lowered = text.lower()
        patterns = (
            r"(?:look|looks|looking|gaze|gazes|gazing|stare|stares|staring)\s+"
            r"(?:directly\s+)?(?:at|into|toward)\s+(?:the\s+)?(?:camera|lens)",
            r"(?:看向|直视|凝视|看着)(?:摄影机|摄像机|镜头)",
        )
        negatives = ("never", "not", "without", "avoid", "不得", "不要", "不能", "不看", "避免")
        for pattern in patterns:
            for match in re.finditer(pattern, lowered):
                prefix = lowered[max(0, match.start() - 32) : match.start()]
                if not any(token in prefix for token in negatives):
                    return True
        return False

    @staticmethod
    def _uuid_key(value: object) -> str | None:
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, AttributeError, TypeError):
            return None

    @staticmethod
    def _canonical_props(value: object) -> list[dict[str, Any]]:
        """Preserve both legacy prop lists and authoritative UUID-key maps."""

        if isinstance(value, list):
            return [dict(item) if isinstance(item, dict) else {"state": item} for item in value]
        if not isinstance(value, dict):
            return []
        props: list[dict[str, Any]] = []
        for prop_id, state in value.items():
            payload = dict(state) if isinstance(state, dict) else {"state": state}
            # The authoritative map key identifies the prop even when the
            # state payload contains only name/visibility/holder fields.
            payload["asset_id"] = str(prop_id)
            props.append(payload)
        return props

    @staticmethod
    def _state_value(state: dict[str, Any], path: str) -> Any:
        current: Any = state
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                raise ValueError(f"required character-state path is missing: {path}")
            current = current[part]
        return current

    @classmethod
    def _inject_character_state_targets(
        cls,
        start_state: dict[str, Any],
        end_state: dict[str, Any],
        character_bindings: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        """Condition generation/evaluation on an approved, still-uncommitted target."""

        start = deepcopy(start_state)
        end = deepcopy(end_state)
        start_characters = dict(start.get("characters") or {})
        end_characters = dict(end.get("characters") or {})
        validation = dict(end.get("_validation") or {})
        existing_requirements = validation.get("required_state_paths", [])
        if not isinstance(existing_requirements, list):
            raise ValueError("end-state required_state_paths must be an array")
        requirements_by_path = {
            str(item.get("path")): dict(item)
            for item in existing_requirements
            if isinstance(item, dict) and item.get("path")
        }
        constraint_lines: list[str] = []
        for binding in character_bindings:
            character_id = binding.get("character_id")
            base_state = binding.get("narrative_state")
            if not character_id or not isinstance(base_state, dict):
                continue
            character_key = str(character_id)
            target_state = binding.get("proposed_narrative_state", base_state)
            if not isinstance(target_state, dict):
                raise ValueError("proposed character narrative state must be an object")
            start_row = dict(start_characters.get(character_key) or {})
            existing_version = start_row.get("narrative_state_version_id")
            if existing_version and existing_version != binding.get("narrative_state_version_id"):
                raise ValueError("timeline state and character binding versions do not match")
            start_row.update(
                {
                    "character_id": character_key,
                    "narrative_state": deepcopy(base_state),
                    "narrative_state_version_id": binding.get("narrative_state_version_id"),
                    "narrative_state_hash": binding.get("narrative_state_hash"),
                }
            )
            start_characters[character_key] = start_row
            end_row = dict(end_characters.get(character_key) or start_row)
            end_row.update(
                {
                    "character_id": character_key,
                    "narrative_state": deepcopy(target_state),
                    "narrative_state_version_id": binding.get("narrative_state_version_id"),
                    "narrative_state_status": (
                        "PROPOSED" if binding.get("proposed_narrative_state") is not None else "UNCHANGED"
                    ),
                    "proposed_narrative_state_hash": binding.get("proposed_narrative_state_hash"),
                }
            )
            end_characters[character_key] = end_row
            constraints_by_path = {
                str(item.get("path")): item
                for item in target_state.get("continuity_constraints", [])
                if isinstance(item, dict) and item.get("path")
            }
            for relative_path in binding.get("generation_required_visual_state_paths", []):
                relative_path = str(relative_path)
                expected = cls._state_value(target_state, relative_path)
                full_path = f"characters.{character_key}.narrative_state.{relative_path}"
                continuity = constraints_by_path.get(relative_path, {})
                requirement: dict[str, Any] = {
                    "path": full_path,
                    "operator": continuity.get("rule", "EQUALS"),
                    "minimum_confidence": 0.75,
                    "severity": "REJECT",
                    "reason_code": continuity.get("id", "CHARACTER_STATE_MISMATCH"),
                    "evidence_required": True,
                }
                if requirement["operator"] != "MUST_EXIST":
                    requirement["expected_value"] = deepcopy(expected)
                requirements_by_path[full_path] = requirement
            constraint_lines.append(
                f"character {character_key} narrative state target: "
                + json.dumps(target_state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
        if start_characters:
            start["characters"] = start_characters
        if end_characters:
            end["characters"] = end_characters
        if requirements_by_path:
            validation["required_state_paths"] = list(requirements_by_path.values())
            end["_validation"] = validation
        return start, end, constraint_lines

    def compile(
        self,
        shot_id: str,
        *,
        character_bindings: list[dict[str, Any]] | None = None,
        canonical_assets: list[dict[str, Any]] | None = None,
        camera: dict[str, Any] | None = None,
        lighting: dict[str, Any] | None = None,
        resolution: str = "720p",
    ) -> VideoPromptCompilation:
        character_bindings = character_bindings or []
        canonical_assets = canonical_assets or []
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise LookupError("shot not found")
            input_state = session.get(TimelineState, shot.input_state_id) if shot.input_state_id else None
            output_state = session.get(TimelineState, shot.output_state_id) if shot.output_state_id else None
            start_state = dict(input_state.state_json) if input_state else {}
            end_state = dict(output_state.state_json) if output_state else {}
            project = shot.scene.episode.project
            project_id = project.id
            scene_id = shot.scene_id
            shot_type = shot.shot_type
            raw_action = shot.user_prompt or shot.prompt
            duration = shot.duration
            aspect_ratio = project.default_aspect_ratio
            generation_policy = shot.generation_policy
            continuity_policy = shot.continuity_policy

        start_state, end_state, state_constraint_lines = self._inject_character_state_targets(
            start_state,
            end_state,
            character_bindings,
        )
        action = self._single_action(raw_action)
        state_characters = start_state.get("characters", {})
        binding_by_character = {
            self._uuid_key(character_id) or str(character_id): binding
            for binding in character_bindings
            if (character_id := binding.get("character_id")) is not None
        }
        subjects: list[CanonicalSubjectSpec] = []
        if isinstance(state_characters, dict):
            for state_key, state in state_characters.items():
                state = state if isinstance(state, dict) else {}
                explicit_character_id = state.get("character_id")
                normalized_state_key = self._uuid_key(state_key)
                key_character_id = (
                    str(state_key) if str(state_key) in binding_by_character else normalized_state_key
                )
                character_id = (
                    self._uuid_key(explicit_character_id) or str(explicit_character_id)
                    if explicit_character_id is not None
                    else key_character_id
                )
                binding = binding_by_character.get(character_id or "", {})
                resolved_character_id = binding.get("character_id") or character_id
                subject_name = state.get("name") or binding.get("name") or state_key
                subjects.append(
                    CanonicalSubjectSpec(
                        name=str(subject_name),
                        asset_id=resolved_character_id,
                        asset_version_id=binding.get("identity_version_id"),
                        screen_position=str(
                            state.get("screen_position") or state.get("position") or "center"
                        ),
                        body_orientation=str(
                            state.get("body_orientation")
                            or state.get("orientation")
                            or "three-quarter toward scene"
                        ),
                        eyeline_target=str(
                            state.get("eyeline_target")
                            or state.get("gaze_target")
                            or "approved scene partner or action target, never the camera"
                        ),
                        pose=str(state.get("pose") or "preserve approved pose"),
                        wardrobe_version_id=state.get("wardrobe_id"),
                        identity_constraints=[
                            constraint
                            for constraint in (
                                f"identity version {binding.get('identity_version_id')}"
                                if binding.get("identity_version_id")
                                else "",
                                f"hair: {binding.get('hair_signature')}"
                                if binding.get("hair_signature")
                                else "",
                                f"wardrobe: {binding.get('costume_signature')}"
                                if binding.get("costume_signature")
                                else "",
                            )
                            if constraint
                        ],
                    )
                )
        if not subjects:
            for index, binding in enumerate(character_bindings, 1):
                subjects.append(
                    CanonicalSubjectSpec(
                        name=str(binding.get("name") or f"subject {index}"),
                        asset_id=binding.get("character_id"),
                        asset_version_id=binding.get("identity_version_id"),
                        eyeline_target="approved scene partner or action target, never the camera",
                        identity_constraints=[
                            f"identity version {binding['identity_version_id']}"
                            for _ in [0]
                            if binding.get("identity_version_id")
                        ],
                    )
                )

        allow_camera_gaze = self._camera_gaze_requested(action, start_state, end_state) or any(
            any(token in subject.eyeline_target.lower() for token in ("camera", "lens", "镜头"))
            and not any(
                token in subject.eyeline_target.lower() for token in ("never", "not", "不得", "不要", "不看")
            )
            for subject in subjects
        )
        if allow_camera_gaze:
            for subject in subjects:
                if subject.eyeline_target in {
                    "approved scene partner or action target, never the camera",
                    "approved scene target, never the camera",
                }:
                    subject.eyeline_target = "camera lens as the explicitly approved target"

        state_camera = start_state.get("camera", {}) if isinstance(start_state.get("camera"), dict) else {}
        camera_values = {**state_camera, **(camera or {})}
        camera_spec = CanonicalCameraSpec(
            position=str(camera_values.get("position", "approved position")),
            angle=str(camera_values.get("angle", "eye level")),
            framing=str(camera_values.get("framing") or camera_values.get("shot_size") or "medium"),
            dominant_movement=str(
                camera_values.get("dominant_movement") or camera_values.get("movement") or "locked-off"
            ),
            speed=str(camera_values.get("speed", "steady")),
            path=str(camera_values.get("path", "none")),
            focus=str(camera_values.get("focus", "primary subject")),
            screen_axis=str(camera_values.get("screen_axis") or camera_values.get("axis") or "A"),
        )
        lighting_values = {
            **(start_state.get("lighting", {}) if isinstance(start_state.get("lighting"), dict) else {}),
            **(lighting or {}),
        }
        lighting_spec = CanonicalLightingSpec.model_validate(lighting_values or {})
        dialogue = str(end_state.get("dialogue") or start_state.get("dialogue") or "")
        props = self._canonical_props(start_state.get("props", []))
        constraints = [
            "one shot contains exactly one dominant action",
            "one dominant camera movement only",
            "every eyeline remains on its specified scene target",
            "canonical identity, wardrobe, scene, product, and prop facts cannot change",
            "end state must equal the approved output state",
        ]
        constraints.append(
            "camera gaze is explicitly approved and must follow the specified eyeline target"
            if allow_camera_gaze
            else "no subject acknowledges the camera"
        )
        constraints.extend(
            str(item) for asset in canonical_assets for item in asset.get("constraints", []) if item
        )
        constraints.extend(state_constraint_lines)
        spec = CanonicalShotSpec(
            project_id=project_id,
            shot_id=shot_id,
            scene_id=scene_id,
            intent=action,
            dominant_action=action,
            duration=duration,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            subjects=subjects,
            props=props,
            start_state=start_state,
            end_state=end_state,
            blocking=start_state.get("blocking", {}),
            camera=camera_spec,
            lighting=lighting_spec,
            dialogue=dialogue,
            language=project.default_language,
            continuity={
                "policy": continuity_policy,
                "previous_shot_id": start_state.get("previous_shot_id"),
                "previous_final_frame_asset_id": start_state.get("previous_final_frame_asset_id"),
            },
            constraints=list(dict.fromkeys(constraints)),
            allow_camera_gaze=allow_camera_gaze,
            generation_policy=generation_policy,
            profile=self._profile(shot_type, dialogue),
        )
        neutral_prompt = self.to_neutral_prompt(spec)
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            record = PromptCompilation(
                project_id=project_id,
                shot_id=shot_id,
                user_prompt=raw_action,
                compiled_prompt=neutral_prompt,
                compiler_version=self.version,
                skill_versions={
                    "director": "v2",
                    "cinematography": "v2",
                    "camera-movement": "v2",
                    "lighting": "v2",
                    "character-consistency": "v2",
                    "prompt-compiler": "v3-persistent-character-state",
                },
                diff_json={
                    "canonical_shot_spec": spec.model_dump(mode="json"),
                    "preserved_facts": [action],
                    "provider_specific": False,
                },
            )
            session.add(record)
            shot.compiled_prompt = neutral_prompt
            session.flush()
            return VideoPromptCompilation(spec=spec, neutral_prompt=neutral_prompt, record_id=record.id)

    @staticmethod
    def to_neutral_prompt(spec: CanonicalShotSpec) -> str:
        payload = spec.model_dump(mode="json")
        ordered = {
            "intent": payload["intent"],
            "subjects": payload["subjects"],
            "props": payload["props"],
            "start_state": payload["start_state"],
            "dominant_action": payload["dominant_action"],
            "blocking": payload["blocking"],
            "camera": payload["camera"],
            "lighting": payload["lighting"],
            "dialogue": payload["dialogue"],
            "end_state": payload["end_state"],
            "continuity": payload["continuity"],
            "constraints": payload["constraints"],
        }
        return json.dumps(ordered, ensure_ascii=False, indent=2)
