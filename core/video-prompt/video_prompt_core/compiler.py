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
    FORBIDDEN_PREFIX,
    PRODUCT_CLAIM_PREFIX,
    PROHIBITION_PREFIX,
    REQUIRED_COPY_PREFIX,
    UNRESOLVED_PREFIX,
    CanonicalCameraSpec,
    CanonicalLightingSpec,
    CanonicalShotSpec,
    CanonicalSubjectSpec,
    PromptCompilerInput,
    PromptCompilerOutput,
    PromptContinuityContext,
    approved_aspect_ratio,
    identity_key,
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
    Validated,
)
from sqlalchemy import select

#: Constraint prefixes the compiler writes and reads back: a prohibition in
#: the client's own words, and one thing that must not be shown or done.
#: Prefixed so `compile_input` can lift them out of a spec's constraints into
#: the QC checklist and the negative prompt without a second channel. They
#: live with the spec contract (`platform_contracts.shot`) because the
#: adapters read them too - a prohibition must reach the provider whichever
#: path compiled the shot - and are re-exported here for every existing caller.
__all_prefixes__ = (
    PROHIBITION_PREFIX,
    FORBIDDEN_PREFIX,
    PRODUCT_CLAIM_PREFIX,
    REQUIRED_COPY_PREFIX,
    UNRESOLVED_PREFIX,
)

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
#: Hedges a stage may write *inside* a photographic value ("provisional medium
#: shot", "焦段暂定") to say it has not decided. Word-bounded for the Latin
#: tokens so "tentatively" in an action line is not a hit; the CJK tokens are
#: plain substrings. Applied to photographic fields only - never to the action,
#: the line, a pose or the director's staging, which are prose, and never to a
#: constraint, where the `unresolved:` prefix is the marker.
_HEDGE_TOKENS = re.compile(
    r"\b(?:provisional|tentative|placeholder|tbc|to be confirmed|to be determined|not yet decided)\b",
    re.IGNORECASE,
)
_HEDGE_TOKENS_CJK: tuple[str, ...] = ("待确认", "暂定")
#: What a model writes in a plan's `unresolved` list to say there is nothing
#: unresolved. Compared after casefolding and stripping punctuation.
#: A leading "none" / "N/A" and the phrases that say the design is complete.
#: An entry is discarded only when *nothing but* these remain: "None of the
#: practicals have been chosen" is a real unresolved entry, and dropping it
#: for its first word would let an undecided shot compile.
_NO_UNRESOLVED_LEAD = re.compile(r"^(?:none|nothing|na|n a|nil|无|没有|暂无)\b\s*")
_NO_UNRESOLVED_PHRASES = re.compile(
    r"^(?:"
    r"at all|here|outstanding|to report|further|"
    r"no unresolved(?: \w+)*|nothing unresolved|not applicable|"
    r"all (?:fields |points |items )?decided|all resolved|"
    r"无未解决\S*|没有未解决\S*|暂无未解决\S*|全部已决定"
    r")$"
)
#: The constraint a Skill-driven cinematography plan's unresolved entry becomes:
#: "unresolved: cinematography[<i>]: <entry>". The preflight reports it under
#: the stage's own path so the record says who left the decision open.
CINEMATOGRAPHY_UNRESOLVED_PREFIX = f"{UNRESOLVED_PREFIX} cinematography["
_CINEMATOGRAPHY_UNRESOLVED = re.compile(r"^unresolved:\s*cinematography\[(\d+)\]:", re.IGNORECASE)


def _plan_unresolved_entries(plan: dict[str, Any]) -> list[tuple[int, str]]:
    """A plan's unresolved entries that say something, each with its place in the plan.

    The index is the entry's own position in ``plan["unresolved"]``, not its
    position after filtering, so the field path the compilation record and the
    refusal name (``cinematography.unresolved[n]``) points at the entry a
    reader will find in the plan.
    """

    entries = plan.get("unresolved")
    if not isinstance(entries, list):
        return []
    kept: list[tuple[int, str]] = []
    for index, item in enumerate(entries):
        text = " ".join(str(item or "").split())
        normalized = " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())
        remainder = _NO_UNRESOLVED_LEAD.sub("", normalized, count=1).strip()
        if not normalized or not remainder or _NO_UNRESOLVED_PHRASES.match(remainder):
            continue
        kept.append((index, text))
    return kept
#: Names that belong to Model Router and the adapters, never to a prompt.
_PROVIDER_MODEL_TERMS = re.compile(
    r"\b(kling|veo|seedance|seedream|runway|grok|openrouter|doubao|qwen|gemini|sora|pika|luma|"
    r"dashscope|midjourney|flux)\b|gpt[- ]image|google flow|stable diffusion|\bwan\s?[23]",
    re.IGNORECASE,
)
_VENDOR_SYNTAX = re.compile(r"(--\w+|\([^()]{1,60}:\d(?:\.\d+)?\)|::\d)")
#: A word in scripts that separate them, a single character in scripts that do
#: not (CJK and kana). The action's tokens, in other words, whichever language
#: the director wrote it in.
_ACTION_TOKEN = re.compile(
    r"[0-9A-Za-z\u00c0-\u024f']+|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]"
)
#: How much of the action's adjacent-token structure a package must still carry
#: when it did not quote the action exactly. Calibrated on the first live
#: production compile (2026-09-10): the model rendered
#: "雨桐进入城市天台上发现了一部不属于她的手机" as "雨桐进入城市天台，发现了一部不属于她的手机。" -
#: one dropped particle and an inserted comma - and scored 0.90. A reworded or
#: substituted action scores far below this floor, because its verb pairs are
#: absent.
_ACTION_COVERAGE_FLOOR = 0.8


def _action_tokens(text: str) -> list[str]:
    return [match.group(0).casefold() for match in _ACTION_TOKEN.finditer(text)]


def _contains_run(written: list[str], wanted: list[str]) -> bool:
    """Does `written` contain `wanted` as a consecutive run of tokens?"""

    if not wanted or len(wanted) > len(written):
        return False
    head = wanted[0]
    span = len(wanted)
    return any(
        written[index] == head and written[index : index + span] == wanted
        for index in range(len(written) - span + 1)
    )


def action_preserved(action: str, positive: str) -> tuple[bool, bool]:
    """Is the approved action still the action this prompt renders? -> (carried, verbatim).

    The compiler may not change what happens in the shot, and this is the check
    that holds it to that. It used to demand the action as an exact substring,
    which is a test of punctuation as much as of meaning: the first live
    production compile was discarded because the model wrote the approved
    sentence with a comma in it and one particle fewer. That is a rewrite of
    nothing, and refusing it threw away the paid wording in favour of a JSON
    dump.

    So: an exact quotation passes, as before. A run of the action's own tokens
    passes - only punctuation or spacing differed. Otherwise the package must
    still carry the action's adjacent-token pairs, which is what survives a
    stylistic edit and is what a *different* action destroys: drop the action
    and the score is near zero, swap its verb and the pairs around that verb
    go with it. `verbatim` says which of those happened, so a paraphrase is
    recorded rather than passed off as a quotation.
    """

    action_text = action.strip()
    if not action_text:
        return True, True
    if action_text.casefold() in positive.casefold():
        return True, True
    wanted = _action_tokens(action_text)
    if not wanted:
        return True, True
    written = _action_tokens(positive)
    if _contains_run(written, wanted):
        return True, True
    if len(wanted) == 1:
        return wanted[0] in written, False
    pairs = {(wanted[index], wanted[index + 1]) for index in range(len(wanted) - 1)}
    seen = {(written[index], written[index + 1]) for index in range(len(written) - 1)}
    return (len(pairs & seen) / len(pairs)) >= _ACTION_COVERAGE_FLOOR, False
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


class ContinuityGateSource(Protocol):
    """The continuity stage's standing verdict on one shot's handoff.

    ``pending_escalation`` returns the Skill-driven ESCALATE the shot still
    awaits a human decision on - the decision id, the review, whether its
    inputs have changed since - or None when nothing blocks the shot. The
    compiler consults it because its own pipeline position is *after*
    Continuity approval: a package compiled around an escalated handoff is a
    package nobody approved.
    """

    def pending_escalation(self, shot_id: str) -> dict[str, Any] | None: ...


class ContinuityApprovalRequired(ValueError):
    """The Continuity Skill escalated this shot's handoff and nobody has approved it.

    Raised by the generation-path compile so no request can be prepared for a
    shot whose handoff the Skill sent to the Director. Carries the decision a
    human must acknowledge - or the review must be run again - to release it.
    """

    reason_code = "CONTINUITY_APPROVAL_REQUIRED"

    def __init__(self, shot_id: str, pending: dict[str, Any]):
        self.shot_id = shot_id
        self.pending = dict(pending)
        self.decision_id = str(pending.get("decision_id") or "")
        review = pending.get("review") if isinstance(pending.get("review"), dict) else {}
        named = [
            str(item)
            for item in [*(review.get("unresolved") or []), *(review.get("evidence") or [])]
            if str(item).strip()
        ][:3]
        stale = " (its inputs changed since; run the review again)" if pending.get("stale") else ""
        super().__init__(
            "the continuity review escalated this shot's handoff and awaits approval"
            + (": " + "; ".join(named) if named else "")
            + f"; acknowledge decision {self.decision_id} or run the review again"
            + stale
        )

    def as_detail(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "reason_code": self.reason_code,
            "shot_id": self.shot_id,
            **{key: value for key, value in self.pending.items() if key != "shot_id"},
            "acknowledge": f"POST /v1/shots/{self.shot_id}/continuity/review/acknowledge",
            "review_again": f"POST /v1/shots/{self.shot_id}/continuity/review",
        }


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
    #: The envelope hash the package was compiled for; replay-stable, unlike
    #: the record id, so a generation request may carry it.
    input_hash: str = ""

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
    #: Subjects and props the envelope took from the shot's OUTPUT state
    #: because its input state had none (the first shot of a scripted
    #: episode). Recorded on the compilation, never written into the prompt:
    #: the prompt says what the frame contains, the record says where the
    #: compiler learned it.
    derived_from_output_state: dict[str, list[str]] = field(default_factory=dict)
    #: What the envelope did with the cinematography plan beyond the camera
    #: and light: the subject placements it applied (and the ones it kept from
    #: an explicit approved state), the plan names it could not match, and the
    #: unresolved entries it carried into the constraints. On the record.
    cinematography_notes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _FreshPackage:
    """The newest Skill-produced package for an envelope, and which Skill produced it."""

    row: PromptCompilation
    output: PromptCompilerOutput
    producing_version: str | None
    producing_hash: str | None
    #: Produced by the Skill that is installed now. A package from an earlier
    #: Skill is not fresh: reusing it would record the new version over the
    #: old Skill's work.
    current: bool


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
        continuity: ContinuityGateSource | None = None,
    ):
        self.database = database
        self.skills = skills
        self.styles = styles
        self.ledger = ledger
        self.dependencies = dependencies
        #: The continuity stage's standing verdicts. A shot whose handoff the
        #: Continuity Skill escalated is not compiled until a human approves
        #: it or the stage clears it; without a source nothing is gated.
        self.continuity = continuity
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

    @classmethod
    def _state_subjects(
        cls,
        characters: object,
        *,
        binding_by_character: dict[str, dict[str, Any]],
        director_gaze: str,
    ) -> list[CanonicalSubjectSpec]:
        """Build the subjects one timeline state's character map describes, bindings folded in by id."""

        subjects: list[CanonicalSubjectSpec] = []
        if not isinstance(characters, dict):
            return subjects
        for state_key, state in characters.items():
            state = state if isinstance(state, dict) else {}
            explicit_character_id = state.get("character_id")
            normalized_state_key = cls._uuid_key(state_key)
            key_character_id = (
                str(state_key) if str(state_key) in binding_by_character else normalized_state_key
            )
            character_id = (
                cls._uuid_key(explicit_character_id) or str(explicit_character_id)
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
                    screen_position=str(state.get("screen_position") or state.get("position") or "center"),
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
                            f"hair: {binding.get('hair_signature')}" if binding.get("hair_signature") else "",
                            f"wardrobe: {binding.get('costume_signature')}"
                            if binding.get("costume_signature")
                            else "",
                        )
                        if constraint
                    ],
                )
            )
        return subjects

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
        binding_by_character = {
            self._uuid_key(character_id) or str(character_id): binding
            for binding in character_bindings
            if (character_id := binding.get("character_id")) is not None
        }
        subjects = self._state_subjects(
            start_state.get("characters", {}),
            binding_by_character=binding_by_character,
            director_gaze=director_gaze,
        )
        # The narrative compiler adds a character to the timeline only when it
        # acts, so the first shot of a scripted episode starts with an empty
        # cast and its actor exists only in the output state - though it is in
        # frame for the whole shot. Read the cast from there and record the
        # provenance on the compilation, not in the prompt: a prompt line about
        # "entering" would be a second action the script does not contain. The
        # timeline state itself stays as written. Bindings keep their own
        # fallback below, and a start state that has a cast is never widened
        # from the end state.
        derived_from_output_state: dict[str, list[str]] = {}
        if not subjects and not character_bindings:
            subjects = self._state_subjects(
                end_state.get("characters", {}),
                binding_by_character=binding_by_character,
                director_gaze=director_gaze,
            )
            if subjects:
                derived_from_output_state["subjects"] = [subject.name for subject in subjects]
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
        # Only a plan the Cinematography Skill wrote carries decisions of its
        # own beyond the camera and light: a deterministic plan is the
        # compiler's old defaults with a fallback note, and that note is a
        # record, not an unresolved creative field.
        plan_skill_driven = cinematography.get("execution_mode") == EXECUTION_MODEL
        plan_unresolved = _plan_unresolved_entries(plan) if plan_skill_driven else []
        camera_values = {**state_camera, **plan_camera, **(camera or {})}
        # The design fields exist only where a stage designed them: a caller's
        # explicit override counts, a Skill-driven plan counts, a deterministic
        # fallback's contract defaults do not.
        design_values = {**(plan_camera if plan_skill_driven else {}), **(camera or {})}
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
            # The rest of the Skill's camera design, and only the Skill's:
            # `deterministic_plan` validates a CinematographyPlan, so a
            # fallback plan carries the contract's own defaults ("subject eye
            # height", "natural perspective", "moderate"). Rendering those as
            # a design would tell the provider a stage decided something no
            # stage decided. Empty otherwise, and printed only when set.
            height=str(design_values.get("height") or ""),
            lens_intent=str(design_values.get("lens_intent") or design_values.get("lens") or ""),
            depth_of_field=str(design_values.get("depth_of_field") or design_values.get("dof") or ""),
        )
        lighting_values = {
            **(start_state.get("lighting", {}) if isinstance(start_state.get("lighting"), dict) else {}),
            **{
                key: value
                for key, value in plan_lighting.items()
                if key
                in {
                    "direction",
                    "quality",
                    "contrast",
                    "color_temperature",
                    "practicals",
                    # Designed, not defaulted: a fallback plan's empty
                    # motivation and exposure intent say nothing anyway, and a
                    # future default would not become a decision by accident.
                    *({"motivation", "exposure_intent"} if plan_skill_driven else set()),
                }
            },
            **(lighting or {}),
        }
        lighting_spec = CanonicalLightingSpec.model_validate(lighting_values or {})
        designed = plan if plan_skill_driven else {}
        atmosphere = " ".join(str(designed.get("atmosphere") or "").split())
        composition = {
            key: " ".join(str(designed.get(f"{key}_composition") or "").split())
            for key in ("start", "end")
            if " ".join(str(designed.get(f"{key}_composition") or "").split())
        }
        # The Skill's subject placements (screen left / centre / right,
        # foreground / midground / background) are its authority over where a
        # subject sits in the frame. They fill a subject whose approved state
        # has no explicit screen position - the narrative `position` is
        # blocking, not framing - and never override one that has. Names are
        # matched the way the identity lock matches them; what was applied,
        # kept and unmatched goes on the record.
        cinematography_notes: dict[str, Any] = {}
        plan_positions = (
            plan.get("subject_positions") if isinstance(plan.get("subject_positions"), dict) else {}
        )
        if plan_positions and subjects:
            explicit: set[str] = set()
            for source in (start_state.get("characters"), end_state.get("characters")):
                if not isinstance(source, dict):
                    continue
                for key, state in source.items():
                    if isinstance(state, dict) and state.get("screen_position"):
                        explicit.update({identity_key(key), identity_key(state.get("name"))})
            explicit.discard("")
            binding_names = {
                identity_key(binding.get("character_id")): identity_key(binding.get("name"))
                for binding in character_bindings
                if binding.get("character_id")
            }
            by_key: dict[str, CanonicalSubjectSpec] = {}
            for subject in subjects:
                for candidate in (
                    subject.name,
                    subject.asset_id,
                    binding_names.get(identity_key(subject.asset_id), ""),
                ):
                    key = identity_key(candidate)
                    if key:
                        by_key.setdefault(key, subject)
            applied: dict[str, dict[str, str]] = {}
            kept: dict[str, dict[str, str]] = {}
            unmatched: list[str] = []
            for name, placement in plan_positions.items():
                placement_text = " ".join(str(placement or "").split())
                subject = by_key.get(identity_key(name))
                if subject is None or not placement_text:
                    unmatched.append(str(name))
                    continue
                if identity_key(subject.name) in explicit or identity_key(subject.asset_id) in explicit:
                    kept[subject.name] = {"state": subject.screen_position, "plan": placement_text}
                    continue
                if subject.screen_position != placement_text:
                    applied[subject.name] = {"from": subject.screen_position, "to": placement_text}
                    subject.screen_position = placement_text
            for key, value in (
                ("subject_position_overrides", applied),
                ("state_positions_kept", kept),
                ("unmatched_subject_positions", unmatched),
            ):
                if value:
                    cinematography_notes[key] = value
        if plan_unresolved:
            cinematography_notes["unresolved"] = [entry for _index, entry in plan_unresolved]
        # A speaking shot's line is in the compiled state; a line spoken during
        # an action shot rides on the director intent, because the narrative
        # compiler read the action line and the line beside it never entered
        # the state.
        dialogue = str(
            end_state.get("dialogue") or start_state.get("dialogue") or director.get("dialogue") or ""
        )
        props = self._canonical_props(start_state.get("props", []))
        if not props:
            # The same gap as the cast: a prop the narrative compiler first
            # meets in this shot's action is in the output state only. It is
            # carried through as the state recorded it - no claim about where
            # it was at the shot's start, which the action may contradict
            # ("takes the phone out of her pocket") - and its provenance goes
            # on the compilation record.
            props = self._canonical_props(end_state.get("props", []))
            if props:
                derived_from_output_state["props"] = [
                    str(prop.get("name") or prop.get("asset_id") or "") for prop in props
                ]
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
        # A Skill-driven cinematography plan's own unresolved entries: the
        # stage said it could not decide these, so the compiler must not
        # render around them. They enter the envelope (the hash covers them)
        # and the deterministic preflight refuses them, naming the stage,
        # before any model call - the same refusal the compiler Skill gave a
        # hedged plan in the live run, now on every path.
        constraints.extend(
            f"{UNRESOLVED_PREFIX} cinematography[{index}]: {entry}" for index, entry in plan_unresolved
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
            atmosphere=atmosphere,
            composition=composition,
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
            derived_from_output_state=derived_from_output_state,
            cinematography_notes=cinematography_notes,
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
        skill = self.skills.resolve("prompt-compiler")
        # The compiler's position is after Continuity approval: a handoff the
        # Continuity Skill escalated is not compiled around, whichever path
        # asked, until a human approves it or the stage clears it.
        pending = self._pending_continuity_escalation(shot_id)
        if pending is not None:
            raise ContinuityApprovalRequired(shot_id, pending)
        fresh = self._fresh_skill_compilation(envelope, skill)
        if fresh is not None:
            if fresh.output.status != "COMPILED":
                # A Skill refusal of exactly this envelope stands whatever
                # Skill is installed now: the refusal is about the unchanged
                # shot, and only a newer Skill verdict for it replaces it.
                raise ValueError(
                    "the prompt compiler Skill judged this shot not compilable: "
                    + str(fresh.output.review_reason or "unspecified")
                )
            if fresh.current:
                recorded = dict(fresh.row.diff_json.get("skill_invocation") or {})
                return self._record(
                    envelope,
                    fresh.output,
                    recorded,
                    EXECUTION_MODEL,
                    skill=skill,
                    reused_from=_original_record_id(fresh.row),
                )
        output = self.compile_input(envelope.compiler_input)
        if output.status != "COMPILED":
            raise ValueError(output.review_reason or "approved shot is not compilable")
        fallback = self._deterministic_invocation(
            "NO_FRESH_SKILL_COMPILATION",
            extra_codes=(
                (f"SKILL_VERSION_CHANGED:{fresh.producing_version}",) if fresh is not None else ()
            ),
        )
        return self._record(envelope, output, fallback.as_json(), EXECUTION_DETERMINISTIC, skill=skill)

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
        skill = self.skills.resolve("prompt-compiler")
        pending = self._pending_continuity_escalation(shot_id)
        if pending is not None:
            # Recorded, not raised: the stage runner and the compile route
            # report a blocked shot as a verdict. No package is reused and no
            # model is paid for a handoff nobody approved.
            blocked = ContinuityApprovalRequired(shot_id, pending)
            output = PromptCompilerOutput(
                status="NOT_COMPILABLE",
                missing_fields=["continuity.approval"],
                review_reason=str(blocked),
            )
            invocation = self._deterministic_invocation(
                blocked.reason_code, extra_codes=(f"CONTINUITY_DECISION:{blocked.decision_id}",)
            )
            return self._record(
                envelope,
                output,
                invocation.as_json(),
                EXECUTION_DETERMINISTIC,
                skill=skill,
                continuity_gate=pending,
            )
        superseded: str | None = None
        if reuse_fresh:
            fresh = self._fresh_skill_compilation(envelope, skill)
            if fresh is not None and fresh.current:
                recorded = dict(fresh.row.diff_json.get("skill_invocation") or {})
                return self._record(
                    envelope,
                    fresh.output,
                    recorded,
                    EXECUTION_MODEL,
                    skill=skill,
                    reused_from=_original_record_id(fresh.row),
                )
            if fresh is not None:
                superseded = fresh.producing_version
        output, invocation = await self.compile_input_with_skill(
            envelope.compiler_input, project_id=envelope.project_id, model_roles=model_roles
        )
        if superseded:
            # The Skill changed since the last package for this envelope: this
            # call is the new Skill's answer, and the record says why it paid.
            invocation.reason_codes.append(f"SKILL_VERSION_CHANGED:{superseded}")
        return self._record(envelope, output, invocation.as_json(), invocation.execution_mode, skill=skill)

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
            codes = [f"PREFLIGHT:{path}" for path in deterministic.missing_fields[:8]]
            if any(path.startswith("cinematography.") for path in deterministic.missing_fields):
                codes.insert(0, "CINEMATOGRAPHY_UNRESOLVED")
            invocation = self._deterministic_invocation("PREFLIGHT_NOT_COMPILABLE", extra_codes=tuple(codes))
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

    def _deterministic_invocation(self, reason: str, *, extra_codes: tuple[str, ...] = ()) -> SkillInvocation:
        if self.runtime is not None:
            invocation = self.runtime.deterministic(SkillOperation.PROMPT_COMPILATION, reason=reason)
            invocation.reason_codes.extend(extra_codes)
            return invocation
        invocation = SkillInvocation(
            operation=SkillOperation.PROMPT_COMPILATION.value,
            fallback_reason=reason,
            reason_codes=[reason, *extra_codes],
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

    def _pending_continuity_escalation(self, shot_id: str) -> dict[str, Any] | None:
        if self.continuity is None:
            return None
        return self.continuity.pending_escalation(shot_id)

    def _fresh_skill_compilation(self, envelope: _Envelope, skill: ResolvedSkill) -> _FreshPackage | None:
        """The newest Skill-produced package for exactly this envelope, and whether it is current.

        Fresh means the same envelope hash *and* the same Skill: the producing
        identity is the invocation the row recorded (a reuse copies it
        verbatim, so a chain of reuses still names the Skill that wrote the
        package), never the version installed when the row was written.
        """

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
                    invocation = (
                        diff.get("skill_invocation") if isinstance(diff.get("skill_invocation"), dict) else {}
                    )
                    producing_hash = str(
                        invocation.get("content_hash") or diff.get("skill_content_hash") or ""
                    )
                    producing_version = str(
                        invocation.get("version") or (row.skill_versions or {}).get("prompt-compiler") or ""
                    )
                    session.expunge(row)
                    return _FreshPackage(
                        row=row,
                        output=PromptCompilerOutput.model_validate(diff["prompt_compiler_output"]),
                        producing_version=producing_version or None,
                        producing_hash=producing_hash or None,
                        current=bool(producing_hash) and producing_hash == skill.content_hash,
                    )
        return None

    def _record(
        self,
        envelope: _Envelope,
        output: PromptCompilerOutput,
        invocation: dict[str, Any],
        execution_mode: str,
        *,
        skill: ResolvedSkill,
        reused_from: str | None = None,
        continuity_gate: dict[str, Any] | None = None,
    ) -> PromptCompilerResult:
        # The Skill on the row is the one that produced the package: on a
        # Skill-driven result that is the recorded invocation (a reuse carries
        # the producing Skill's identity, not the installed one); on a
        # deterministic result it is the Skill that was resolved and not run.
        skill_driven = execution_mode == EXECUTION_MODEL
        producing_name = str(invocation.get("name") or skill.name) if skill_driven else skill.name
        producing_version = str(invocation.get("version") or skill.version) if skill_driven else skill.version
        producing_hash = (
            str(invocation.get("content_hash") or skill.content_hash) if skill_driven else skill.content_hash
        )
        neutral_prompt = output.positive_prompt or ""
        cinematography_record: dict[str, Any] = (
            {
                "execution_mode": envelope.cinematography.get("execution_mode"),
                "skill_version": (envelope.cinematography.get("skill_invocation") or {}).get("version"),
                "input_hash": envelope.cinematography.get("input_hash"),
                **envelope.cinematography_notes,
            }
            if envelope.cinematography
            else {"execution_mode": None, "fallback_reason": "NOT_DESIGNED"}
        )
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
                skill_versions={"prompt-compiler": producing_version},
                diff_json={
                    "canonical_shot_spec": envelope.spec.model_dump(mode="json"),
                    "prompt_compiler_input": envelope.compiler_input.model_dump(mode="json"),
                    "prompt_compiler_output": output.model_dump(mode="json"),
                    "skill_content_hash": producing_hash,
                    "installed_skill_version": skill.version,
                    "skill_invocation": invocation,
                    "execution_mode": execution_mode,
                    "skill_driven": skill_driven,
                    "input_hash": envelope.input_hash,
                    "reused_from": reused_from,
                    "cinematography": cinematography_record,
                    **({"continuity_gate": dict(continuity_gate)} if continuity_gate is not None else {}),
                    "preserved_facts": [envelope.action],
                    "derived_from_output_state": dict(envelope.derived_from_output_state),
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
                skill_name=producing_name,
                skill_version=producing_version,
                execution_mode=execution_mode,
                skill_invocation=dict(invocation),
                input_hash=envelope.input_hash,
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
            text = str(constraint).strip()
            if not text.lower().startswith(UNRESOLVED_PREFIX):
                continue
            plan_entry = _CINEMATOGRAPHY_UNRESOLVED.match(text)
            if plan_entry is not None:
                # The cinematography stage's own entry: reported under its
                # path, so the record says which stage left the decision open.
                missing.append(f"cinematography.unresolved[{plan_entry.group(1)}]")
                reasons.append(
                    "the cinematography stage left a decision unresolved: " + text[plan_entry.end() :].strip()
                )
            else:
                missing.append(f"constraints[{index}]")
                reasons.append(f"a stage left a decision unresolved: {constraint}")

        # Photographic fields: a stage that has not decided one writes a
        # marker value or hedges inside it ("provisional medium shot"). Both
        # refuse compilation here; the compiler never completes them. Prose
        # fields (the action, the line, a pose, the staging) are never hedge-
        # scanned: "a tentative step" is a performance, not an open decision.
        def hedged(value: Any) -> bool:
            text = " ".join(str(value or "").split())
            return bool(_HEDGE_TOKENS.search(text)) or any(token in text for token in _HEDGE_TOKENS_CJK)

        photographic: list[tuple[str, Any]] = [
            (f"camera.{key}", value) for key, value in spec.camera.model_dump().items()
        ]
        lighting_fields = spec.lighting.model_dump()
        photographic.extend(
            (f"lighting.{key}", value) for key, value in lighting_fields.items() if key != "practicals"
        )
        photographic.extend(
            (f"lighting.practicals[{index}]", item)
            for index, item in enumerate(lighting_fields.get("practicals") or [])
        )
        for index, subject in enumerate(spec.subjects):
            if unresolved(subject.name):
                missing.append(f"subjects[{index}].name")
                reasons.append(f"subjects[{index}].name is unresolved")
            photographic.extend(
                (f"subjects[{index}].{key}", getattr(subject, key))
                for key in ("screen_position", "body_orientation", "eyeline_target")
            )
        for index, prop in enumerate(spec.props):
            photographic.extend(
                (f"props[{index}].{key}", value) for key, value in prop.items() if isinstance(value, str)
            )
        photographic.append(("aspect_ratio", spec.aspect_ratio))
        photographic.append(("atmosphere", spec.atmosphere))
        photographic.extend((f"composition.{key}", value) for key, value in spec.composition.items())
        for path, value in photographic:
            if unresolved(value) or hedged(value):
                missing.append(path)
                reasons.append(f"{path} is unresolved")
        if not str(spec.aspect_ratio).strip():
            missing.append("aspect_ratio")
            reasons.append("aspect_ratio is empty")
        return list(dict.fromkeys(missing)), list(dict.fromkeys(reasons))

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
                _pruned_lighting(spec.lighting.model_dump(mode="json")),
                ensure_ascii=False,
                sort_keys=True,
            ),
            *([f"atmosphere={spec.atmosphere}"] if spec.atmosphere else []),
            *(
                [f"composition_{key}={value}" for key, value in spec.composition.items() if value]
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
        ordered: dict[str, Any] = {
            "intent": payload["intent"],
            "subjects": payload["subjects"],
            "props": payload["props"],
            "start_state": payload["start_state"],
            "dominant_action": payload["dominant_action"],
            "blocking": payload["blocking"],
            "camera": _pruned_camera(payload["camera"]),
            "lighting": _pruned_lighting(payload["lighting"]),
        }
        # The cinematography design beyond camera and light, when a stage
        # decided it; a shot nobody designed renders exactly as before.
        if payload.get("atmosphere"):
            ordered["atmosphere"] = payload["atmosphere"]
        if any((payload.get("composition") or {}).values()):
            ordered["composition"] = {
                key: value for key, value in payload["composition"].items() if value
            }
        ordered.update(
            {
                "dialogue": payload["dialogue"],
                "end_state": payload["end_state"],
                "continuity": payload["continuity"],
                "style_lock": payload["style_lock"],
                "constraints": payload["constraints"],
            }
        )
        return json.dumps(ordered, ensure_ascii=False, indent=2)


def _pruned_camera(camera: dict[str, Any]) -> dict[str, Any]:
    """The camera dict without the optional design fields a stage left empty."""

    return {
        key: value
        for key, value in camera.items()
        if not (key in ("height", "lens_intent", "depth_of_field") and not value)
    }


def _pruned_lighting(lighting: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in lighting.items()
        if not (key in ("motivation", "exposure_intent") and not value)
    }


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
    carried, _verbatim = action_preserved(spec.dominant_action, output.positive_prompt or "")
    if not carried:
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
) -> PromptCompilerOutput | Validated:
    output = PromptCompilerOutput.model_validate(raw)
    if output.status != "COMPILED":
        return output
    contract, authority = verify_compiled_package(spec, value, output)
    if authority:
        raise AuthorityViolation(authority)
    if contract:
        raise ValueError("compiled package failed re-verification: " + ", ".join(contract))
    _carried, verbatim = action_preserved(spec.dominant_action, output.positive_prompt or "")
    if not verbatim:
        # The action survived but was not quoted. The package ships; the row
        # says the wording is the compiler's rendering of the action rather
        # than the director's own sentence.
        return Validated(output, reason_codes=["ACTION_PARAPHRASED"])
    return output


# Import compatibility only: both names resolve to the one implementation above.
VideoShotPromptCompiler = PromptCompilerService
VideoPromptCompilation = PromptCompilerResult
