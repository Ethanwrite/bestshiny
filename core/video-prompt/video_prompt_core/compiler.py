from __future__ import annotations

import json
import re
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

        action = self._single_action(raw_action)
        state_characters = start_state.get("characters", {})
        binding_by_character = {str(binding.get("character_id")): binding for binding in character_bindings}
        subjects: list[CanonicalSubjectSpec] = []
        if isinstance(state_characters, dict):
            for name, state in state_characters.items():
                state = state if isinstance(state, dict) else {}
                binding = binding_by_character.get(str(state.get("character_id")), {})
                subjects.append(
                    CanonicalSubjectSpec(
                        name=str(name),
                        asset_id=binding.get("character_id"),
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
        props = start_state.get("props", []) if isinstance(start_state.get("props"), list) else []
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
                    "prompt-compiler": "v2",
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
