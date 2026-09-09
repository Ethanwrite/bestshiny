from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal, Protocol

from platform_contracts import (
    CanonicalCameraSpec,
    CanonicalLightingSpec,
    CanonicalShotSpec,
    CanonicalSubjectSpec,
    PromptCompilerInput,
    PromptCompilerOutput,
    PromptContinuityContext,
    approved_aspect_ratio,
)
from platform_database import Database
from production_domain.models import PromptCompilation, Shot, TimelineState
from pydantic import ValidationError
from skill_core.runtime import (
    EXECUTION_DETERMINISTIC,
    EXECUTION_MODEL,
    AuthorityViolation,
    SkillInvocation,
    SkillOperation,
    SkillRuntime,
)
from sqlalchemy import select

#: Constraint prefixes the compiler writes and reads back: a prohibition in
#: the client's own words, and one thing that must not be shown or done.
#: Prefixed so `compile_input` can lift them out of a spec's constraints into
#: the QC checklist and the negative prompt without a second channel.
PROHIBITION_PREFIX = "the client forbade, in their words: "
FORBIDDEN_PREFIX = "must not show or do: "
PRODUCT_CLAIM_PREFIX = "product claim, verbatim and unparaphrased: "
REQUIRED_COPY_PREFIX = "required on-screen copy, exactly these words: "
UNRESOLVED_PREFIX = "unresolved:"

#: Connectors that sequence two actions in one line: the second action is a
#: second shot, and the compiler refuses to render both in one.
_SEQUENCE_CONNECTORS = re.compile(
    r"(然后|接着|紧接着|随后|而后|再接着|之后再|"
    r"\b(?:and then|then|after that|afterwards|followed by|subsequently)\b)",
    re.IGNORECASE,
)
_MOVEMENT_CONNECTORS = re.compile(r"(\s(?:and|then|plus)\s|\s\+\s|,\s*then|然后|并且|再)", re.IGNORECASE)
#: Values a stage may leave in a field to say it is not decided. The compiler
#: never resolves them; it returns NOT_COMPILABLE naming the field.
_UNRESOLVED_VALUES = frozenset({"unresolved", "tbd", "to be decided", "undecided", "?", "待定", "未定"})
#: Names that belong to Model Router and the adapters, never to a prompt.
_PROVIDER_MODEL_TERMS = re.compile(
    r"\b(kling|veo|seedance|seedream|runway|grok|openrouter|doubao|qwen|gemini|sora|pika|luma|"
    r"dashscope|midjourney|flux)\b|gpt[- ]image|google flow|stable diffusion|\bwan\s?[23]",
    re.IGNORECASE,
)
_VENDOR_SYNTAX = re.compile(r"(--\w+|\([^()]{1,60}:\d(?:\.\d+)?\)|::\d)")
_ENVELOPE_KEYS = (
    "shot_spec",
    "asset_bindings",
    "continuity_context",
    "PromptCompilerInput",
    "CanonicalShotSpec",
)

COMPILER_PROTOCOL = """
## Compiling one envelope (application protocol)

The PromptCompilerInput envelope follows. Compile it into the provider-neutral package and answer with ONE
JSON object carrying exactly these eight fields and nothing else:
{
  "status": "COMPILED" | "NOT_COMPILABLE",
  "positive_prompt": str | null,
  "negative_prompt": str | null,
  "asset_bindings": [str],          // the envelope's identifiers, deduplicated, order preserved
  "continuity_assertions": [str],   // one per entry of continuity_context.facts
  "qc_checklist": [str],            // yes/no checks over what the package asserts
  "missing_fields": [str],
  "review_reason": str | null
}

The runtime re-verifies the package: the dominant action, every subject's name, the line, every product claim
and required copy must appear verbatim in the positive prompt; the asset bindings must be echoed exactly and
never appear in the prose; there must be one assertion per fact; no model, provider, vendor syntax or envelope
key may appear anywhere. A package that fails any check is discarded and the deterministic package stands in.
""".strip()


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
        self,
        project_id: str,
        *,
        episode: int,
        scene_sequence: int | None = None,
        shot_sequence: int | None = None,
        holder_keys: list[str] | None = None,
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
    #: MODEL when the prompt-compiler Skill produced the package (fresh, verified),
    #: DETERMINISTIC when the deterministic compiler did - with the invocation
    #: record saying why.
    execution_mode: str = EXECUTION_DETERMINISTIC
    skill_invocation: dict[str, Any] = field(default_factory=dict)

    @property
    def neutral_prompt(self) -> str:
        return self.output.positive_prompt or ""

    @property
    def skill_driven(self) -> bool:
        return self.execution_mode == EXECUTION_MODEL


@dataclass(frozen=True)
class _Envelope:
    """One shot's compiler input, assembled once for every compile path."""

    shot_id: str
    project_id: str
    spec: CanonicalShotSpec
    compiler_input: PromptCompilerInput
    raw_action: str
    action: str
    input_hash: str
    cinematography: dict[str, Any]


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
        runtime: SkillRuntime | None = None,
    ):
        self.database = database
        self.skills = skills
        self.styles = styles
        self.ledger = ledger
        self.dependencies = dependencies
        #: The Skill runtime that runs the prompt-compiler Skill on a model.
        #: Without one, every compilation is the deterministic package and the
        #: record says the stage was not configured.
        self.runtime = runtime

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
    def _prompt_facts(
        dependency_facts: list[dict[str, Any]],
        series_facts: list[dict[str, Any]],
    ) -> list[str]:
        """Render narrative facts as compact prompt lines, each exactly once.

        The structured entries keep travelling untouched through
        ``continuity_context.facts`` into ``continuity_assertions``; these
        strings are the model-facing rendering of the same material.
        """

        rendered: list[str] = []
        for fact in dependency_facts:
            parts = [f"explicit_dependency[{fact.get('dependency_type', '')}]"]
            for key in ("fact_key", "obligation_key", "source_shot_id"):
                if fact.get(key):
                    parts.append(f"{key}={fact[key]}")
            rendered.append(f"{' '.join(parts)}: {fact.get('value', '')}")
        for fact in series_facts:
            name = fact.get("name")
            if name == "open_obligation":
                rendered.append(f"open_obligation: {fact.get('value', '')}")
            elif name == "director_continuity_obligation":
                rendered.append(f"continuity_obligation: {fact.get('value', '')}")
            elif name == "screenplay_invariant":
                rendered.append(f"invariant: {fact.get('value', '')}")
            elif name == "product_claim_verbatim":
                rendered.append(f'product_claim (verbatim): "{fact.get("value", "")}"')
            elif name == "required_copy_verbatim":
                rendered.append(f'required_copy (verbatim): "{fact.get("value", "")}"')
            elif name == "known_fact":
                rendered.append(f"known_fact[{fact.get('holder', '')}]: {fact.get('value', '')}")
            else:
                rendered.append(
                    json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                )
        return list(dict.fromkeys(rendered))

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

    def _envelope(
        self,
        shot_id: str,
        *,
        character_bindings: list[dict[str, Any]] | None = None,
        canonical_assets: list[dict[str, Any]] | None = None,
        camera: dict[str, Any] | None = None,
        lighting: dict[str, Any] | None = None,
        resolution: str = "720p",
        dependency_contexts: Sequence[ResolvedDependency] | None = None,
    ) -> _Envelope:
        """Assemble one shot's CanonicalShotSpec and envelope; every compile path starts here."""

        character_bindings = character_bindings or []
        canonical_assets = canonical_assets or []
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise LookupError("shot not found")
            # The Cinematography Skill's plan for this shot, when the stage
            # designed one: read between the timeline state and a caller's
            # explicit overrides, so the compiler renders the treatment the
            # stage decided rather than the locked-off defaults.
            cinematography = dict(shot.cinematography_json or {})
            input_state = session.get(TimelineState, shot.input_state_id) if shot.input_state_id else None
            output_state = session.get(TimelineState, shot.output_state_id) if shot.output_state_id else None
            start_state = dict(input_state.state_json) if input_state else {}
            end_state = dict(output_state.state_json) if output_state else {}
            project = shot.scene.episode.project
            project_id = project.id
            scene_id = shot.scene_id
            episode_number = shot.scene.episode.episode_number
            scene_sequence = shot.scene.sequence
            shot_sequence = shot.sequence
            shot_type = shot.shot_type
            # The approved director intent, written back onto the shot at
            # compile time. Read here so the staged action, the gaze target,
            # the states the shot moves between and the continuity it owes
            # reach the prompt instead of stopping at the audit record.
            director = dict(shot.director_intent_json or {})
            raw_action = shot.user_prompt or shot.prompt
            duration = shot.duration
            # The frame the approved brief fixed, when this shot came out of
            # a director session; the project default is only for shots made
            # outside one. Compiling (and billing) 9:16 for a 16:9 approval
            # is exactly the mismatch reading the default here produced.
            aspect_ratio = approved_aspect_ratio(director) or project.default_aspect_ratio
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
            # The complete position, not the episode: continuity facts and
            # open obligations from later shots of this same episode must not
            # compile into an earlier shot's prompt.
            series = self.ledger.series_context(
                project_id,
                episode=episode_number,
                scene_sequence=scene_sequence,
                shot_sequence=shot_sequence,
                holder_keys=holder_keys,
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
        director_gaze = str(director.get("gaze_target") or "").strip()
        director_staging = str(director.get("description") or "").strip()
        # Who is in frame and what may move beside the dominant action. Present
        # characters are staged in the prompt; identity references stay with the
        # identity-critical subset (narrowed in the production pipeline).
        present_characters = [
            str(item).strip() for item in (director.get("present_characters") or []) if str(item).strip()
        ]
        micro_actions = [
            str(item).strip().replace("_", " ")
            for item in (director.get("micro_actions") or [])
            if str(item).strip()
        ]
        director_obligations = [
            str(item).strip()
            for item in (director.get("continuity_obligations") or [])
            if str(item).strip()
        ]
        director_invariants = [
            str(item).strip() for item in (director.get("invariants") or []) if str(item).strip()
        ]
        director_claims = [
            str(item).strip() for item in (director.get("product_claims") or []) if str(item).strip()
        ]
        director_copy = [
            str(item).strip() for item in (director.get("required_copy") or []) if str(item).strip()
        ]
        director_prohibitions = [
            str(item).strip() for item in (director.get("prohibitions") or []) if str(item).strip()
        ]
        director_prohibited_terms = [
            str(item).strip()
            for item in (director.get("prohibited_terms") or [])
            if str(item).strip()
        ]
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
                            # The director said where this shot looks. It wins
                            # over the compiler's inference and over the
                            # default, but not over an explicit approved state.
                            or director_gaze
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
                        eyeline_target=director_gaze
                        or "approved scene partner or action target, never the camera",
                        identity_constraints=[
                            f"identity version {binding['identity_version_id']}"
                            for _ in [0]
                            if binding.get("identity_version_id")
                        ],
                    )
                )

        # The director's gaze is free text, and the token test below scans
        # every subject eyeline - which now includes it. "off-camera partner"
        # says the opposite of what the tokens would read, so the negative
        # vocabulary has to cover it; the action line keeps its own test.
        allow_camera_gaze = self._camera_gaze_requested(action, start_state, end_state) or any(
            any(token in subject.eyeline_target.lower() for token in ("camera", "lens", "镜头"))
            and not any(
                token in subject.eyeline_target.lower()
                for token in (
                    "never",
                    "not",
                    "off-camera",
                    "off camera",
                    "away from camera",
                    "不得",
                    "不要",
                    "不看",
                    "画外",
                    "镜头外",
                )
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
        plan = cinematography.get("plan") if isinstance(cinematography.get("plan"), dict) else {}
        plan_camera = dict(plan.get("camera") or {}) if isinstance(plan.get("camera"), dict) else {}
        plan_lighting = dict(plan.get("lighting") or {}) if isinstance(plan.get("lighting"), dict) else {}
        camera_values = {**state_camera, **plan_camera, **(camera or {})}
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
            **{
                key: value
                for key, value in plan_lighting.items()
                if key in {"direction", "quality", "contrast", "color_temperature", "practicals"}
            },
            **(lighting or {}),
        }
        lighting_spec = CanonicalLightingSpec.model_validate(lighting_values or {})
        # A speaking shot's line is in the compiled state; a line spoken during
        # an action shot rides on the director intent, because the narrative
        # compiler read the action line and the line beside it never entered
        # the state.
        dialogue = str(
            end_state.get("dialogue") or start_state.get("dialogue") or director.get("dialogue") or ""
        )
        props = self._canonical_props(start_state.get("props", []))
        # A canonical PRODUCT or PROP the DIRECTOR bound to *this* shot is a
        # thing the shot must render exactly, so it enters the spec's own prop
        # list and every adapter's prompt names it. Only this shot's own
        # anchors: `canonical_assets` is the whole project's canonical list, and
        # putting all of it here would make the evaluator demand a product in
        # every shot it does not appear in - a critical failure that buys a
        # paid retry for a shot that was correct.
        bound_media = {
            str(item) for item in (director.get("reference_asset_ids") or []) if item
        }
        known_prop_assets = {str(prop.get("asset_id")) for prop in props if prop.get("asset_id")}
        for asset in canonical_assets:
            if asset.get("type") not in {"PRODUCT", "PROP"}:
                continue
            if str(asset.get("id")) in known_prop_assets:
                continue
            media = {str(item) for item in (asset.get("image_urls") or [])} | {
                str(item) for item in (asset.get("video_urls") or [])
            }
            if not bound_media or not (media & bound_media):
                continue
            props.append(
                {
                    "asset_id": str(asset.get("id")),
                    "asset_version_id": asset.get("version_id"),
                    "name": asset.get("name"),
                    "kind": asset.get("type"),
                    "state": "canonical appearance is fixed",
                }
            )
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
        # What the director approved for this exact shot, in the compiler's own
        # constraint vocabulary. These are not suggestions the model may trade
        # away: they are the staging and the continuity the user signed off.
        if director_staging:
            constraints.append(f"stage the approved action as: {director_staging}")
        if present_characters:
            constraints.append("in frame: " + ", ".join(present_characters))
        if micro_actions:
            constraints.append(
                "micro-actions allowed beside the dominant action: " + ", ".join(micro_actions)
            )
        constraints.extend(f"continuity obligation: {item}" for item in director_obligations)
        # Scoped to this shot by the director, never every invariant on every
        # shot. Claims and copy are quoted, because their wording is the thing
        # being preserved: a paraphrase is a different claim.
        constraints.extend(f"invariant that holds here: {item}" for item in director_invariants)
        constraints.extend(
            f'product claim, verbatim and unparaphrased: "{item}"' for item in director_claims
        )
        constraints.extend(
            f'required on-screen copy, exactly these words: "{item}"' for item in director_copy
        )
        # What the client forbade, whole (so a model reads the instruction as
        # the client gave it) and as the things it forbids (so they reach the
        # negative prompt and the QC checklist by name). The screenplay gate
        # can only match words; this is where a prohibition is enforced on
        # what is actually rendered.
        constraints.extend(f"{PROHIBITION_PREFIX}{item}" for item in director_prohibitions)
        constraints.extend(f"{FORBIDDEN_PREFIX}{item}" for item in director_prohibited_terms)
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
            # The authoritative state stays exactly what the timeline says;
            # the director's own wording for how this shot starts and ends
            # sits beside it rather than over it.
            start_state=(
                {**start_state, "director_staging": director["start_state"]}
                if director.get("start_state")
                else start_state
            ),
            end_state=(
                {**end_state, "director_staging": director["end_state"]}
                if director.get("end_state")
                else end_state
            ),
            blocking=start_state.get("blocking", {}),
            camera=camera_spec,
            lighting=lighting_spec,
            dialogue=dialogue,
            language=project.default_language,
            continuity={
                "policy": continuity_policy,
                "previous_shot_id": start_state.get("previous_shot_id"),
                "previous_final_frame_asset_id": start_state.get("previous_final_frame_asset_id"),
                # Dependency, series and obligation facts live INSIDE the spec,
                # because the spec is the one artefact every prompt surface
                # renders: `to_neutral_prompt` (the positive/legacy prompt) and
                # every adapter's `Continuity:` line. Facts that lived only in
                # `continuity_assertions` were metadata about the prompt, not
                # part of it, and never reached a model. Each fact is rendered
                # once here and nowhere else in the spec, so no prompt surface
                # repeats it.
                **(
                    {"facts": prompt_facts}
                    if (
                        prompt_facts := self._prompt_facts(
                            dependency_facts,
                            [
                                *series_facts,
                                *(
                                    {"name": "director_continuity_obligation", "value": item}
                                    for item in director_obligations
                                ),
                                *(
                                    {"name": "screenplay_invariant", "value": item}
                                    for item in director_invariants
                                ),
                                *(
                                    {"name": "product_claim_verbatim", "value": item}
                                    for item in director_claims
                                ),
                                *(
                                    {"name": "required_copy_verbatim", "value": item}
                                    for item in director_copy
                                ),
                            ],
                        )
                    )
                    else {}
                ),
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
            {"name": "approved_start_state", "value": spec.start_state},
            {"name": "approved_end_state", "value": spec.end_state},
            *dependency_facts,
            *series_facts,
            *(
                {"name": "director_continuity_obligation", "value": item}
                for item in director_obligations
            ),
            *({"name": "screenplay_invariant", "value": item} for item in director_invariants),
            *({"name": "product_claim_verbatim", "value": item} for item in director_claims),
            *({"name": "required_copy_verbatim", "value": item} for item in director_copy),
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
        return _Envelope(
            shot_id=shot_id,
            project_id=project_id,
            spec=spec,
            compiler_input=compiler_input,
            raw_action=raw_action,
            action=action,
            input_hash=hashlib.sha256(
                json.dumps(compiler_input.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode(
                    "utf-8"
                )
            ).hexdigest(),
            cinematography=cinematography,
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
        """Compile one shot on the generation path, without calling a model.

        A package the prompt-compiler Skill produced for exactly this envelope
        (same input hash, verified, recorded by ``compile_shot_with_skill``)
        is reused as the Skill's work; otherwise the deterministic compiler
        produces the package and the record says the stage fell back and why.
        A fresh Skill verdict of NOT_COMPILABLE is honoured, not overridden.
        """

        envelope = self._envelope(
            shot_id,
            character_bindings=character_bindings,
            canonical_assets=canonical_assets,
            camera=camera,
            lighting=lighting,
            resolution=resolution,
            dependency_contexts=dependency_contexts,
        )
        fresh = self._fresh_skill_compilation(envelope)
        if fresh is not None:
            output = PromptCompilerOutput.model_validate(fresh.diff_json["prompt_compiler_output"])
            if output.status != "COMPILED":
                raise ValueError(
                    "the prompt compiler Skill judged this shot not compilable: "
                    + str(output.review_reason or "unspecified")
                )
            recorded = dict(fresh.diff_json.get("skill_invocation") or {})
            return self._record(
                envelope, output, recorded, EXECUTION_MODEL, reused_from=_original_record_id(fresh)
            )
        output = self.compile_input(envelope.compiler_input)
        if output.status != "COMPILED":
            raise ValueError(output.review_reason or "approved shot is not compilable")
        fallback = self._deterministic_invocation("NO_FRESH_SKILL_COMPILATION")
        return self._record(envelope, output, fallback.as_json(), EXECUTION_DETERMINISTIC)

    async def compile_shot_with_skill(
        self,
        shot_id: str,
        *,
        model_roles: Any | None = None,
        character_bindings: list[dict[str, Any]] | None = None,
        canonical_assets: list[dict[str, Any]] | None = None,
        camera: dict[str, Any] | None = None,
        lighting: dict[str, Any] | None = None,
        resolution: str = "720p",
        dependency_contexts: Sequence[ResolvedDependency] | None = None,
        reuse_fresh: bool = True,
    ) -> PromptCompilerResult:
        """Run the prompt-compiler Skill for one shot and record its package.

        The record is what the generation path reuses while the envelope is
        unchanged. A NOT_COMPILABLE verdict is recorded as such - it is the
        Skill's decision, and it blocks generation until the shot changes or
        the stage is run again. With ``reuse_fresh`` a package the Skill
        already produced for exactly this envelope is returned instead of
        paying for the same answer again.
        """

        envelope = self._envelope(
            shot_id,
            character_bindings=character_bindings,
            canonical_assets=canonical_assets,
            camera=camera,
            lighting=lighting,
            resolution=resolution,
            dependency_contexts=dependency_contexts,
        )
        if reuse_fresh:
            fresh = self._fresh_skill_compilation(envelope)
            if fresh is not None:
                output = PromptCompilerOutput.model_validate(fresh.diff_json["prompt_compiler_output"])
                recorded = dict(fresh.diff_json.get("skill_invocation") or {})
                return self._record(
                    envelope, output, recorded, EXECUTION_MODEL, reused_from=_original_record_id(fresh)
                )
        output, invocation = await self.compile_input_with_skill(
            envelope.compiler_input, project_id=envelope.project_id, model_roles=model_roles
        )
        return self._record(envelope, output, invocation.as_json(), invocation.execution_mode)

    async def compile_input_with_skill(
        self,
        value: PromptCompilerInput,
        *,
        project_id: str,
        model_roles: Any | None = None,
    ) -> tuple[PromptCompilerOutput, SkillInvocation]:
        """Compile one envelope through the prompt-compiler Skill, re-verified; degrade loudly.

        The deterministic preflight runs first: an envelope it refuses is
        never sent to a model, because the Skill's own first rule is the same
        refusal. A model package that fails re-verification - a dropped
        subject, a paraphrased claim, an invented asset, a provider name - is
        discarded and the deterministic package returned, on record.
        """

        deterministic = self.compile_input(value)
        operation = SkillOperation.PROMPT_COMPILATION
        if deterministic.status != "COMPILED":
            invocation = self._deterministic_invocation("PREFLIGHT_NOT_COMPILABLE")
            return deterministic, invocation
        if self.runtime is None:
            return deterministic, self._deterministic_invocation("SKILL_RUNTIME_NOT_CONFIGURED")
        spec = CanonicalShotSpec.model_validate(value.shot_spec)
        invocation = await self.runtime.invoke(
            operation,
            project_id=project_id,
            protocol=COMPILER_PROTOCOL,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(
                        {"task": "COMPILE", "envelope": value.model_dump(mode="json")},
                        ensure_ascii=False,
                        default=str,
                    ),
                }
            ],
            validator=partial(_validate_compiler_output, spec, value),
            model_roles=model_roles,
            max_tokens=2500,
        )
        if invocation.output is None:
            return deterministic, invocation
        return invocation.output, invocation

    def _deterministic_invocation(self, reason: str) -> SkillInvocation:
        if self.runtime is not None:
            return self.runtime.deterministic(SkillOperation.PROMPT_COMPILATION, reason=reason)
        invocation = SkillInvocation(
            operation=SkillOperation.PROMPT_COMPILATION.value,
            fallback_reason=reason,
            reason_codes=[reason],
        )
        try:
            skill = self.skills.resolve("prompt-compiler")
        except (LookupError, ValueError, OSError):
            invocation.reason_codes.insert(0, "SKILL_UNAVAILABLE")
        else:
            invocation.name = skill.name
            invocation.version = skill.version
            invocation.content_hash = skill.content_hash
            invocation.role = "prompt_compiler"
            invocation.resolved = True
        return invocation

    def _fresh_skill_compilation(self, envelope: _Envelope) -> PromptCompilation | None:
        """The latest Skill-produced package for exactly this envelope, if any."""

        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(PromptCompilation)
                    .where(PromptCompilation.shot_id == envelope.shot_id)
                    .order_by(PromptCompilation.created_at.desc())
                    .limit(12)
                )
            )
            for row in rows:
                diff = dict(row.diff_json or {})
                if (
                    diff.get("execution_mode") == EXECUTION_MODEL
                    and diff.get("input_hash") == envelope.input_hash
                    and isinstance(diff.get("prompt_compiler_output"), dict)
                ):
                    session.expunge(row)
                    return row
        return None

    def _record(
        self,
        envelope: _Envelope,
        output: PromptCompilerOutput,
        invocation: dict[str, Any],
        execution_mode: str,
        *,
        reused_from: str | None = None,
    ) -> PromptCompilerResult:
        skill = self.skills.resolve("prompt-compiler")
        neutral_prompt = output.positive_prompt or ""
        with self.database.session() as session:
            shot = session.get(Shot, envelope.shot_id)
            if shot is None:
                raise LookupError("shot disappeared during compilation")
            record = PromptCompilation(
                project_id=envelope.project_id,
                shot_id=envelope.shot_id,
                user_prompt=envelope.raw_action,
                compiled_prompt=neutral_prompt,
                compiler_version=self.version,
                skill_versions={"prompt-compiler": skill.version},
                diff_json={
                    "canonical_shot_spec": envelope.spec.model_dump(mode="json"),
                    "prompt_compiler_input": envelope.compiler_input.model_dump(mode="json"),
                    "prompt_compiler_output": output.model_dump(mode="json"),
                    "skill_content_hash": skill.content_hash,
                    "skill_invocation": invocation,
                    "execution_mode": execution_mode,
                    "skill_driven": execution_mode == EXECUTION_MODEL,
                    "input_hash": envelope.input_hash,
                    "reused_from": reused_from,
                    "cinematography": {
                        "execution_mode": envelope.cinematography.get("execution_mode"),
                        "skill_version": (envelope.cinematography.get("skill_invocation") or {}).get(
                            "version"
                        ),
                        "input_hash": envelope.cinematography.get("input_hash"),
                    }
                    if envelope.cinematography
                    else {"execution_mode": None, "fallback_reason": "NOT_DESIGNED"},
                    "preserved_facts": [envelope.action],
                    "provider_specific": False,
                },
            )
            session.add(record)
            if output.status == "COMPILED":
                shot.compiled_prompt = neutral_prompt
            session.flush()
            return PromptCompilerResult(
                spec=envelope.spec,
                input=envelope.compiler_input,
                output=output,
                record_id=record.id,
                skill_name=skill.name,
                skill_version=skill.version,
                execution_mode=execution_mode,
                skill_invocation=dict(invocation),
            )

    @staticmethod
    def _preflight(spec: CanonicalShotSpec) -> tuple[list[str], list[str]]:
        """The Skill's own preflight, deterministically: what makes a spec uncompilable.

        Returns the offending field paths and the reasons. Two sequenced
        actions, two camera movements, and any field a stage left unresolved
        stop compilation here; the compiler never resolves them.
        """

        missing: list[str] = []
        reasons: list[str] = []

        def unresolved(value: Any) -> bool:
            text = " ".join(str(value or "").split()).lower()
            return text in _UNRESOLVED_VALUES or text.startswith(UNRESOLVED_PREFIX)

        if not spec.intent.strip():
            missing.append("intent")
            reasons.append("intent is empty")
        connector = _SEQUENCE_CONNECTORS.search(spec.dominant_action)
        if connector is not None:
            missing.append("dominant_action")
            reasons.append(
                f"dominant_action sequences two actions ({connector.group(0)!r}); one shot carries one action"
            )
        if _MOVEMENT_CONNECTORS.search(spec.camera.dominant_movement):
            missing.append("camera.dominant_movement")
            reasons.append("camera.dominant_movement names more than one movement")
        for path, value in (
            ("intent", spec.intent),
            ("dominant_action", spec.dominant_action),
            ("dialogue", spec.dialogue),
            ("camera.dominant_movement", spec.camera.dominant_movement),
            ("start_state.director_staging", spec.start_state.get("director_staging")),
            ("end_state.director_staging", spec.end_state.get("director_staging")),
        ):
            if unresolved(value):
                missing.append(path)
                reasons.append(f"{path} is unresolved")
        for index, subject in enumerate(spec.subjects):
            for key in ("eyeline_target", "screen_position", "pose", "body_orientation"):
                if unresolved(getattr(subject, key)):
                    missing.append(f"subjects[{index}].{key}")
                    reasons.append(f"subjects[{index}].{key} is unresolved")
        for index, constraint in enumerate(spec.constraints):
            if str(constraint).strip().lower().startswith(UNRESOLVED_PREFIX):
                missing.append(f"constraints[{index}]")
                reasons.append(f"a stage left a decision unresolved: {constraint}")
        if not str(spec.aspect_ratio).strip():
            missing.append("aspect_ratio")
            reasons.append("aspect_ratio is empty")
        return list(dict.fromkeys(missing)), reasons

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
        missing, reasons = self._preflight(spec)
        if missing:
            return PromptCompilerOutput(
                status="NOT_COMPILABLE",
                missing_fields=missing,
                review_reason="; ".join(reasons),
            )
        facts = [
            item
            if isinstance(item, str)
            else json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in value.continuity_context.facts
        ]
        forbidden = [
            item.removeprefix(FORBIDDEN_PREFIX)
            for item in spec.constraints
            if item.startswith(FORBIDDEN_PREFIX)
        ]
        prohibitions = [
            item.removeprefix(PROHIBITION_PREFIX)
            for item in spec.constraints
            if item.startswith(PROHIBITION_PREFIX)
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
            # Reviewed as the client said it, not as the compiler paraphrased
            # it: the checklist line is the whole prohibition.
            *(f"prohibition={item}" for item in prohibitions),
        ]
        return PromptCompilerOutput(
            status="COMPILED",
            positive_prompt=self.to_neutral_prompt(spec),
            negative_prompt=", ".join(
                [
                    "identity drift, visual style drift, changed wardrobe, changed props, extra subjects, "
                    "duplicate limbs, unintended cuts, text artifacts, unapproved direct gaze into the lens",
                    *forbidden,
                ]
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


def _original_record_id(record: PromptCompilation) -> str:
    """The record the Skill actually produced: a reuse of a reuse still points at it."""

    original = (record.diff_json or {}).get("reused_from")
    return str(original) if original else record.id


def verify_compiled_package(
    spec: CanonicalShotSpec, value: PromptCompilerInput, output: PromptCompilerOutput
) -> tuple[list[str], list[str]]:
    """Re-verify a Skill-compiled package: (contract problems, authority problems).

    Contract problems are dropped or altered facts and a broken echo; authority
    problems are decisions the compiler may not make - naming a model or a
    provider, vendor syntax, an asset described into the prose, an envelope
    key leaked. Either list rejects the package.
    """

    contract: list[str] = []
    authority: list[str] = []
    positive = (output.positive_prompt or "").casefold()
    combined = f"{output.positive_prompt or ''}\n{output.negative_prompt or ''}"
    expected_assets = list(dict.fromkeys(value.asset_bindings))
    if list(output.asset_bindings) != expected_assets:
        contract.append("asset_bindings_not_echoed")
    if len(output.continuity_assertions) != len(value.continuity_context.facts):
        contract.append("continuity_assertions_count")
    if spec.dominant_action.casefold() not in positive:
        contract.append("dominant_action_missing")
    for subject in spec.subjects:
        if subject.name.strip() and subject.name.casefold() not in positive:
            contract.append(f"subject_missing:{subject.name}")
    if spec.dialogue.strip() and " ".join(spec.dialogue.split()).casefold() not in " ".join(positive.split()):
        contract.append("dialogue_missing")
    verbatim_prefixes = (
        (PRODUCT_CLAIM_PREFIX, "product_claim_missing"),
        (REQUIRED_COPY_PREFIX, "required_copy_missing"),
    )
    for constraint in spec.constraints:
        for prefix, code in verbatim_prefixes:
            if constraint.startswith(prefix):
                quoted = constraint.removeprefix(prefix).strip().strip('"')
                if quoted and quoted.casefold() not in positive:
                    contract.append(code)
    for identifier in expected_assets:
        if identifier and identifier in combined:
            authority.append("asset_id_in_prompt")
            break
    match = _PROVIDER_MODEL_TERMS.search(combined)
    if match is not None:
        authority.append(f"provider_or_model_named:{match.group(0).strip()}")
    if _VENDOR_SYNTAX.search(combined):
        authority.append("vendor_syntax")
    for key in _ENVELOPE_KEYS:
        if key in combined:
            authority.append(f"envelope_key_leaked:{key}")
    return list(dict.fromkeys(contract)), list(dict.fromkeys(authority))


def _validate_compiler_output(
    spec: CanonicalShotSpec, value: PromptCompilerInput, raw: dict[str, Any]
) -> PromptCompilerOutput:
    output = PromptCompilerOutput.model_validate(raw)
    if output.status != "COMPILED":
        return output
    contract, authority = verify_compiled_package(spec, value, output)
    if authority:
        raise AuthorityViolation(authority)
    if contract:
        raise ValueError("compiled package failed re-verification: " + ", ".join(contract))
    return output


# Import compatibility only: both names resolve to the one implementation above.
VideoShotPromptCompiler = PromptCompilerService
VideoPromptCompilation = PromptCompilerResult
