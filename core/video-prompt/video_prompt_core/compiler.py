from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from platform_contracts import (
    CanonicalCameraSpec,
    CanonicalLightingSpec,
    CanonicalShotSpec,
    CanonicalSubjectSpec,
    PromptCompilerInput,
    PromptCompilerOutput,
    PromptContinuityContext,
)
from platform_database import Database
from production_domain.models import PromptCompilation, Shot, TimelineState
from pydantic import ValidationError


class ResolvedSkill(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def content_hash(self) -> str: ...

    @property
    def system_prompt(self) -> str: ...


class PromptSkillRegistry(Protocol):
    def resolve(self, name: str) -> ResolvedSkill: ...


class LockedStyle(Protocol):
    @property
    def asset_id(self) -> str: ...

    def prompt_view(self) -> dict[str, Any]: ...


class ProjectStyleSource(Protocol):
    """The authoritative project style lock, resolved by the compiler itself.

    Style lock used to arrive as a `style_lock` key a caller had merged into its
    `canonical_assets`. Exactly one caller did so, and every other path through
    `compile()` produced a prompt with no style lock at all — a wrong image, not
    an error. The compiler now reads the lock from this source, so the guarantee
    holds for every caller rather than for one.
    """

    def generation_control(self, project_id: str) -> LockedStyle | None: ...


class SeriesContextResult(Protocol):
    @property
    def open_obligations(self) -> list[str]: ...

    def continuity_facts(self) -> list[dict[str, Any]]: ...


class SeriesLedgerSource(Protocol):
    """The narrative ledger: established facts per holder and open obligations.

    `series_context()` is O(1) in episode count and its `continuity_facts()`
    render directly into `PromptCompilerInput.continuity_context.facts` — the
    only door into `continuity_assertions`, so an undisclosed fact cannot reach
    a prompt by accident.
    """

    def series_context(
        self, project_id: str, *, episode: int, holder_keys: list[str] | None = None
    ) -> SeriesContextResult: ...


class ResolvedDependency(Protocol):
    @property
    def dependency_id(self) -> str: ...

    @property
    def dependency_type(self) -> str: ...

    @property
    def summary(self) -> str: ...

    @property
    def source_shot_id(self) -> str | None: ...

    @property
    def fact_key(self) -> str | None: ...

    @property
    def obligation_key(self) -> str | None: ...

    @property
    def payload(self) -> dict[str, Any]: ...


class ShotDependencySource(Protocol):
    """Explicit shot dependencies, resolved or refused — never guessed.

    `resolve_for_generation` raises when a declared dependency cannot be
    resolved; the compiler lets that propagate so the caller moves the shot to
    review instead of compiling a prompt that silently omits owed material.
    """

    def resolve_for_generation(self, shot_id: str) -> Sequence[ResolvedDependency]: ...


@dataclass(frozen=True)
class PromptCompilerResult:
    spec: CanonicalShotSpec
    input: PromptCompilerInput
    output: PromptCompilerOutput
    record_id: str
    skill_name: str
    skill_version: str

    @property
    def neutral_prompt(self) -> str:
        return self.output.positive_prompt or ""


class PromptCompilerService:
    """The single provider-neutral Prompt/Skill compilation boundary."""

    version = "prompt-compiler-v3-unified"

    def __init__(
        self,
        database: Database,
        skills: PromptSkillRegistry,
        styles: ProjectStyleSource | None = None,
        ledger: SeriesLedgerSource | None = None,
        dependencies: ShotDependencySource | None = None,
    ):
        self.database = database
        self.skills = skills
        self.styles = styles
        self.ledger = ledger
        self.dependencies = dependencies

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

    def _locked_style(
        self,
        project_id: str,
        canonical_assets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Resolve the project's locked style, authoritative source first.

        A caller-supplied `style_lock` is only a fallback for callers that
        already resolved the lock themselves; it can never *replace* the
        authoritative one, because a prompt compiled against a stale style would
        pass every check while rendering the wrong look.
        """

        if self.styles is not None:
            control = self.styles.generation_control(project_id)
            if control is not None:
                return dict(control.prompt_view())
            # A project with no lock has no style to preserve. Falling through to
            # a caller's dict here would reintroduce the unenforceable path.
            return {}
        return next(
            (
                dict(asset.get("style_lock") or {})
                for asset in canonical_assets
                if asset.get("type") == "STYLE" and asset.get("style_lock")
            ),
            {},
        )

    def compile(
        self,
        shot_id: str,
        *,
        character_bindings: list[dict[str, Any]] | None = None,
        canonical_assets: list[dict[str, Any]] | None = None,
        camera: dict[str, Any] | None = None,
        lighting: dict[str, Any] | None = None,
        resolution: str = "720p",
        dependency_contexts: Sequence[ResolvedDependency] | None = None,
    ) -> PromptCompilerResult:
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
            episode_number = shot.scene.episode.episode_number
            shot_type = shot.shot_type
            raw_action = shot.user_prompt or shot.prompt
            duration = shot.duration
            aspect_ratio = project.default_aspect_ratio
            generation_policy = shot.generation_policy
            continuity_policy = shot.continuity_policy

        # Stage one of context: explicit material the shot *requires*. The
        # resolver raises when a declared dependency cannot be resolved, and
        # that error must propagate — compiling a prompt that silently omits
        # owed material is exactly the degradation this stage exists to forbid.
        if dependency_contexts is None and self.dependencies is not None:
            dependency_contexts = list(self.dependencies.resolve_for_generation(shot_id))
        dependency_facts: list[dict[str, Any]] = [
            {
                "name": "explicit_dependency",
                "dependency_type": item.dependency_type,
                "value": item.summary,
                **({"source_shot_id": item.source_shot_id} if item.source_shot_id else {}),
                **({"fact_key": item.fact_key} if item.fact_key else {}),
                **({"obligation_key": item.obligation_key} if item.obligation_key else {}),
                **({"payload": item.payload} if item.payload else {}),
                "source_reason": "EXPLICIT_DEPENDENCY",
            }
            for item in (dependency_contexts or [])
        ]
        series_facts: list[dict[str, Any]] = []
        if self.ledger is not None:
            holder_keys = [
                str(binding["character_id"])
                for binding in character_bindings
                if binding.get("character_id")
            ]
            series = self.ledger.series_context(
                project_id, episode=episode_number, holder_keys=holder_keys
            )
            series_facts = [
                {
                    **fact,
                    "source_reason": (
                        "OPEN_OBLIGATION" if fact.get("name") == "open_obligation" else "SERIES_FACT"
                    ),
                }
                for fact in series.continuity_facts()
            ]

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
        locked_style = self._locked_style(project_id, canonical_assets)
        if locked_style:
            constraints.append(
                "preserve the locked visual style across every frame; do not drift in palette, "
                "contrast, texture, rendering medium, or edge treatment"
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
            style_lock=locked_style,
            constraints=list(dict.fromkeys(constraints)),
            allow_camera_gaze=allow_camera_gaze,
            generation_policy=generation_policy,
            profile=self._profile(shot_type, dialogue),
        )
        asset_bindings = list(
            dict.fromkeys(
                str(asset_id)
                for asset_id in (
                    *(
                        item
                        for binding in character_bindings
                        for item in binding.get("canonical_assets", [])
                    ),
                    *(
                        item
                        for asset in canonical_assets
                        for item in [
                            *(asset.get("image_urls") or []),
                            *(asset.get("video_urls") or []),
                        ]
                    ),
                )
                if asset_id
            )
        )
        continuity_facts: list[str | dict[str, Any]] = [
            {"name": "approved_start_state", "value": start_state},
            {"name": "approved_end_state", "value": end_state},
            *dependency_facts,
            *series_facts,
            *state_constraint_lines,
        ]
        compiler_input = PromptCompilerInput(
            shot_spec=spec.model_dump(mode="json"),
            asset_bindings=asset_bindings,
            continuity_context=PromptContinuityContext(
                transition=continuity_policy,
                facts=continuity_facts,
            ),
        )
        output = self.compile_input(compiler_input)
        if output.status != "COMPILED":
            raise ValueError(output.review_reason or "approved shot is not compilable")
        skill = self.skills.resolve("prompt-compiler")
        neutral_prompt = output.positive_prompt or ""
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            record = PromptCompilation(
                project_id=project_id,
                shot_id=shot_id,
                user_prompt=raw_action,
                compiled_prompt=neutral_prompt,
                compiler_version=self.version,
                skill_versions={"prompt-compiler": skill.version},
                diff_json={
                    "canonical_shot_spec": spec.model_dump(mode="json"),
                    "prompt_compiler_input": compiler_input.model_dump(mode="json"),
                    "prompt_compiler_output": output.model_dump(mode="json"),
                    "skill_content_hash": skill.content_hash,
                    "preserved_facts": [action],
                    "provider_specific": False,
                },
            )
            session.add(record)
            shot.compiled_prompt = neutral_prompt
            session.flush()
            return PromptCompilerResult(
                spec=spec,
                input=compiler_input,
                output=output,
                record_id=record.id,
                skill_name=skill.name,
                skill_version=skill.version,
            )

    def compile_input(self, value: PromptCompilerInput) -> PromptCompilerOutput:
        """Compile one typed envelope without leaking Skill instructions into the prompt.

        The deterministic backend is authoritative until an approved model-backed
        Skill executor is installed. Both backends must return this same contract.
        """

        self.skills.resolve("prompt-compiler")
        try:
            spec = CanonicalShotSpec.model_validate(value.shot_spec)
        except ValidationError as exc:
            missing_fields = sorted(
                {
                    str(error["loc"][0])
                    for error in exc.errors()
                    if error.get("type") == "missing" and error.get("loc")
                }
            )
            return PromptCompilerOutput(
                status="NOT_COMPILABLE",
                missing_fields=missing_fields,
                review_reason="CanonicalShotSpec failed validation: " + str(exc.errors(include_url=False)),
            )
        facts = [
            item
            if isinstance(item, str)
            else json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in value.continuity_context.facts
        ]
        qc_checklist = [
            f"dominant_action={spec.dominant_action}",
            f"camera_movement={spec.camera.dominant_movement}",
            "lighting="
            + json.dumps(
                spec.lighting.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
            ),
            f"duration={spec.duration:g}s",
            f"aspect_ratio={spec.aspect_ratio}",
            *(
                f"subject_identity={subject.name}:{subject.asset_version_id or subject.asset_id or 'unbound'}"
                for subject in spec.subjects
            ),
        ]
        return PromptCompilerOutput(
            status="COMPILED",
            positive_prompt=self.to_neutral_prompt(spec),
            negative_prompt=(
                "identity drift, visual style drift, changed wardrobe, changed props, extra subjects, "
                "duplicate limbs, unintended cuts, text artifacts, unapproved direct gaze into the lens"
            ),
            asset_bindings=list(dict.fromkeys(value.asset_bindings)),
            continuity_assertions=facts,
            qc_checklist=qc_checklist,
        )

    def skill_contract(self) -> dict[str, Any]:
        """Return the exact model-execution contract without invoking a model."""

        skill = self.skills.resolve("prompt-compiler")
        return {
            "name": skill.name,
            "version": skill.version,
            "content_hash": skill.content_hash,
            "system_prompt": skill.system_prompt,
            "input_schema": PromptCompilerInput.model_json_schema(),
            "output_schema": PromptCompilerOutput.model_json_schema(),
        }

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
        """Compatibility facade backed by the same unified compiler implementation."""

        del provider, model, scene_bindings
        result = self.compile(
            shot_id,
            character_bindings=character_bindings,
            camera=camera,
            lighting=lighting,
        )
        with self.database.session() as session:
            compilation = session.get(PromptCompilation, result.record_id)
            if compilation is None:  # pragma: no cover - transaction integrity guard.
                raise RuntimeError("prompt compilation record disappeared")
            return compilation

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
            "style_lock": payload["style_lock"],
            "constraints": payload["constraints"],
        }
        return json.dumps(ordered, ensure_ascii=False, indent=2)


# Import compatibility only: both names resolve to the one implementation above.
VideoShotPromptCompiler = PromptCompilerService
VideoPromptCompilation = PromptCompilerResult
