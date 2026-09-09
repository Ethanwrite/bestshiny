"""The unified Skill runtime: resolve -> bind -> load -> snapshot -> invoke -> validate -> track.

A registered Skill is not an invoked Skill. This module is the only path by
which a Skill body reaches a model, and it makes four states distinct and
recorded on every call:

- ``resolved``: the runtime operation mapped to exactly one installed Skill;
- ``loaded``: that Skill's body became the system prompt of a model call
  (and no other Skill's body did - injecting the whole catalogue is refused);
- ``model_invoked``: the model was actually called under the Skill's role;
- ``execution_mode``: ``MODEL`` when a validated model output is the outcome,
  ``MODEL_WITHOUT_SKILL`` when a model answered under a generic prompt
  because the Skill could not be loaded, ``DETERMINISTIC`` when the caller's
  deterministic path produced the outcome - with ``fallback_reason`` saying
  why. Only ``MODEL`` is Skill-driven; a deterministic fallback is never
  reported as the Skill's work.

Every invocation carries the Skill's name, version, content hash and role, so
the audit of what a stage was told is exact, and every output is validated
against the stage's structured contract before it counts.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .registry import REQUIRED_SECTIONS, SkillDefinition, SkillRegistry, SkillRegistryError

logger = logging.getLogger(__name__)

# Reason codes shared with the creative director's own vocabulary, so a row
# written by either reads the same way.
SKILL_LOADED = "SKILL_LOADED"
SKILL_UNAVAILABLE = "SKILL_UNAVAILABLE"
SKILL_STAGE_DISABLED = "SKILL_STAGE_DISABLED"
MODEL_RUNTIME_NOT_CONFIGURED = "MODEL_RUNTIME_NOT_CONFIGURED"
MODEL_BUDGET_REFUSED = "MODEL_BUDGET_REFUSED"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
MODEL_CALL_ERROR = "MODEL_CALL_ERROR"
MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
MODEL_REPLY = "MODEL_REPLY"
AUTHORITY_VIOLATION = "AUTHORITY_VIOLATION"
DETERMINISTIC_FALLBACK = "DETERMINISTIC_FALLBACK"

EXECUTION_MODEL = "MODEL"
EXECUTION_MODEL_WITHOUT_SKILL = "MODEL_WITHOUT_SKILL"
EXECUTION_DETERMINISTIC = "DETERMINISTIC"


class SkillOperation(StrEnum):
    """The runtime operations the platform resolves a Skill for.

    Resolution is by operation, never by "load everything": each operation
    binds to exactly one Skill, and a creative task that is not one of these
    does not get a Skill by default - not even the Director.
    """

    CREATIVE_CONVERSATION = "creative_conversation"
    STORY_GENERATION = "story_generation"
    STORY_REVISION = "story_revision"
    SHOT_DECOMPOSITION = "shot_decomposition"
    SHOT_REVISION = "shot_revision"
    CINEMATOGRAPHY_DESIGN = "cinematography_design"
    CONTINUITY_REVIEW = "continuity_review"
    PROMPT_COMPILATION = "prompt_compilation"


class SkillStage(StrEnum):
    STORY = "STORY"
    SHOT_PLANNING = "SHOT_PLANNING"
    CINEMATOGRAPHY = "CINEMATOGRAPHY"
    CONTINUITY = "CONTINUITY"
    PROMPT_COMPILATION = "PROMPT_COMPILATION"


#: What the platform expects the installed registry to declare: which runtime
#: role answers for each operation. The registry's metadata is the binding;
#: this table is the contract the binding must satisfy, checked at startup so
#: a Skill that stops declaring an operation fails the build, not a turn.
EXPECTED_BINDINGS: dict[SkillOperation, str] = {
    SkillOperation.CREATIVE_CONVERSATION: "director",
    SkillOperation.STORY_GENERATION: "director",
    SkillOperation.STORY_REVISION: "director",
    SkillOperation.SHOT_DECOMPOSITION: "shot_planner",
    SkillOperation.SHOT_REVISION: "shot_planner",
    SkillOperation.CINEMATOGRAPHY_DESIGN: "cinematography",
    SkillOperation.CONTINUITY_REVIEW: "continuity",
    SkillOperation.PROMPT_COMPILATION: "prompt_compiler",
}


@dataclass(frozen=True)
class CallSite:
    """Where an operation is invoked, what validates it, what it falls back to."""

    operation: SkillOperation
    service: str
    contract: str
    fallback: str
    record: str


#: The runtime call sites, one per operation. The integration matrix reads
#: this table; the test suite proves each entry by driving the call site with
#: a scripted model and checking the Skill body, version and hash on the call.
CALL_SITES: dict[SkillOperation, CallSite] = {
    SkillOperation.CREATIVE_CONVERSATION: CallSite(
        SkillOperation.CREATIVE_CONVERSATION,
        "creative_director_core.CreativeDirectorService._reason_turn",
        "DirectorTurnResult",
        "deterministic rules engine reply (reasoner=DETERMINISTIC)",
        "creative_turns.skill_version / skill_content_hash / context_json.skill_invocation",
    ),
    SkillOperation.STORY_GENERATION: CallSite(
        SkillOperation.STORY_GENERATION,
        "creative_director_core.CreativeDirectorService._reason_story",
        "StoryDraft",
        "deterministic scaffold screenplay (reasoner=DETERMINISTIC)",
        "creative_screenplays.skill_version / content_json._context.skill_invocations.story",
    ),
    SkillOperation.STORY_REVISION: CallSite(
        SkillOperation.STORY_REVISION,
        "creative_director_core.CreativeDirectorService._reason_story",
        "StoryDraft",
        "deterministic scaffold screenplay (reasoner=DETERMINISTIC)",
        "creative_screenplays.skill_version / content_json._context.skill_invocations.story",
    ),
    SkillOperation.SHOT_DECOMPOSITION: CallSite(
        SkillOperation.SHOT_DECOMPOSITION,
        "creative_director_core.CreativeDirectorService._reason_shot_plan",
        "ShotPlan",
        "deterministic one-line-per-shot decomposition (reasoner=…+DETERMINISTIC:SHOT_PLANNER)",
        "creative_screenplays.content_json._context.skill_invocations.shots",
    ),
    SkillOperation.SHOT_REVISION: CallSite(
        SkillOperation.SHOT_REVISION,
        "creative_director_core.CreativeDirectorService._reason_shot_plan",
        "ShotPlan",
        "deterministic one-line-per-shot decomposition (reasoner=…+DETERMINISTIC:SHOT_PLANNER)",
        "creative_screenplays.content_json._context.skill_invocations.shots",
    ),
    SkillOperation.CINEMATOGRAPHY_DESIGN: CallSite(
        SkillOperation.CINEMATOGRAPHY_DESIGN,
        "director_production.CinematographyDesigner.design_shot",
        "CinematographyPlan",
        "deterministic shot-type defaults (camera locked-off, lighting preserved)",
        "shots.cinematography_json.skill_invocation / decision_records(CINEMATOGRAPHY_DESIGN)",
    ),
    SkillOperation.CONTINUITY_REVIEW: CallSite(
        SkillOperation.CONTINUITY_REVIEW,
        "continuity_core.ContinuityReviewer.review_pair",
        "ContinuityReview",
        "deterministic end-state / start-state comparison",
        "decision_records(CONTINUITY_REVIEW).input_features.skill_invocation",
    ),
    SkillOperation.PROMPT_COMPILATION: CallSite(
        SkillOperation.PROMPT_COMPILATION,
        "video_prompt_core.PromptCompilerService.compile_input_with_skill",
        "PromptCompilerOutput",
        "deterministic compile_input (the same eight-field contract)",
        "prompt_compilations.skill_versions / diff_json.skill_invocation",
    ),
}


class SkillRuntimeError(RuntimeError):
    pass


class SkillUnavailable(SkillRuntimeError):
    """The registry cannot supply the Skill an operation is bound to."""


class AuthorityViolation(ValueError):
    """A Skill output claimed a decision outside the Skill's authority."""

    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations) or "authority violation")
        self.violations = list(violations)


@dataclass
class Validated:
    """What a validator may return when it has more to say than the output."""

    output: Any
    reason_codes: list[str] = field(default_factory=list)
    authority_violations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LoadedSkill:
    definition: SkillDefinition
    operation: SkillOperation

    @property
    def system_prompt(self) -> str:
        return self.definition.system_prompt

    @property
    def version(self) -> str:
        return self.definition.version

    @property
    def content_hash(self) -> str:
        return self.definition.content_hash


@dataclass
class SkillInvocation:
    """The audit of one runtime operation, whatever produced its outcome."""

    operation: str
    name: str | None = None
    version: str | None = None
    content_hash: str | None = None
    role: str | None = None
    stage: str | None = None
    model_role: str | None = None
    resolved: bool = False
    loaded: bool = False
    model_invoked: bool = False
    execution_mode: str = EXECUTION_DETERMINISTIC
    fallback_reason: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    execution_record_id: str | None = None
    decision_record_id: str | None = None
    context_hash: str | None = None
    validation_errors: list[str] = field(default_factory=list)
    authority_violations: list[str] = field(default_factory=list)
    output: Any = None
    raw: dict[str, Any] | None = None

    @property
    def skill_driven(self) -> bool:
        return self.execution_mode == EXECUTION_MODEL and self.loaded

    @property
    def retryable(self) -> bool:
        return self.fallback_reason in {
            MODEL_BUDGET_REFUSED,
            MODEL_UNAVAILABLE,
            MODEL_CALL_ERROR,
            MODEL_OUTPUT_INVALID,
        }

    def as_json(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "name": self.name,
            "version": self.version,
            "content_hash": self.content_hash,
            "role": self.role,
            "stage": self.stage,
            "model_role": self.model_role,
            "resolved": self.resolved,
            "loaded": self.loaded,
            "model_invoked": self.model_invoked,
            "execution_mode": self.execution_mode,
            "skill_driven": self.skill_driven,
            "fallback_reason": self.fallback_reason,
            "reason_codes": list(self.reason_codes),
            "execution_record_id": self.execution_record_id,
            "decision_record_id": self.decision_record_id,
            "context_hash": self.context_hash,
            "validation_errors": list(self.validation_errors),
            "authority_violations": list(self.authority_violations),
        }


def first_choice_json(response: dict[str, Any]) -> dict[str, Any]:
    """The JSON object in a chat response's first choice; tolerant of fences and prose."""

    choices = response.get("choices") if isinstance(response, dict) else None
    if not isinstance(choices, list) or not choices:
        raise ValueError("chat response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise
            parsed = json.loads(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("chat response is not a JSON object")


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SkillRuntime:
    """Resolve a runtime operation to its one Skill, and run it accountably."""

    version = "skill-runtime-v1"

    def __init__(
        self,
        registry: SkillRegistry,
        model_roles: Any | None = None,
        *,
        enabled: bool = True,
    ):
        self.registry = registry
        self.model_roles = model_roles
        self.enabled = enabled
        #: Per-operation counts observed in this process; the integration
        #: matrix shows them beside the static call-site table.
        self.ledger: dict[str, dict[str, int]] = {}

    # ------------------------------------------------------------ resolution
    def resolve(self, operation: SkillOperation | str) -> SkillDefinition:
        """Exactly one Skill per operation, and the role the platform expects."""

        op = SkillOperation(operation)
        try:
            definition = self.registry.resolve_operation(op.value)
        except (LookupError, ValueError, OSError) as exc:
            raise SkillUnavailable(f"{op.value}: {exc}") from exc
        expected = EXPECTED_BINDINGS[op]
        if definition.role != expected:
            raise SkillUnavailable(
                f"{op.value} resolved to Skill {definition.name!r} with role {definition.role!r}; "
                f"the platform binds this operation to role {expected!r}"
            )
        return definition

    def load(self, operation: SkillOperation | str) -> LoadedSkill:
        return LoadedSkill(self.resolve(operation), SkillOperation(operation))

    def validate(self, *, strict_sections: bool = True) -> list[str]:
        """Startup integrity: every expected operation binds, once, to a complete Skill."""

        problems = list(self.registry.validate_bindings())
        for operation, role in EXPECTED_BINDINGS.items():
            try:
                definition = self.registry.resolve_operation(operation.value)
            except (LookupError, SkillRegistryError) as exc:
                problems.append(f"{operation.value}: {exc}")
                continue
            if definition.role != role:
                problems.append(
                    f"{operation.value}: bound to {definition.name} (role {definition.role}), "
                    f"expected role {role}"
                )
            if not definition.model_role:
                problems.append(f"{operation.value}: {definition.name} declares no model_role")
        if strict_sections:
            for definition in self.registry.list_skills():
                missing = definition.missing_sections
                if missing:
                    problems.append(
                        f"{definition.name}: missing sections {', '.join(missing)} "
                        f"(required: {', '.join(REQUIRED_SECTIONS)})"
                    )
        return problems

    # ------------------------------------------------------------- snapshots
    def snapshot_reference(self, name: str, *, reason: str) -> dict[str, Any]:
        """The honest record for a Skill that was consulted but never injected."""

        try:
            definition = self.registry.resolve(name)
        except (LookupError, ValueError, OSError):
            return {
                "name": name,
                "version": None,
                "content_hash": None,
                "role": None,
                "resolved": False,
                "loaded": False,
                "model_invoked": False,
                "execution_mode": EXECUTION_DETERMINISTIC,
                "skill_driven": False,
                "fallback_reason": SKILL_UNAVAILABLE,
            }
        return {
            **definition.snapshot(),
            "resolved": True,
            "loaded": False,
            "model_invoked": False,
            "execution_mode": EXECUTION_DETERMINISTIC,
            "skill_driven": False,
            "fallback_reason": reason,
        }

    def deterministic(self, operation: SkillOperation | str, *, reason: str) -> SkillInvocation:
        """An invocation record for an operation the caller decided not to run."""

        op = SkillOperation(operation)
        invocation = SkillInvocation(operation=op.value, fallback_reason=reason, reason_codes=[reason])
        try:
            definition = self.resolve(op)
        except SkillUnavailable:
            invocation.reason_codes.insert(0, SKILL_UNAVAILABLE)
        else:
            self._stamp(invocation, definition)
            invocation.resolved = True
        self._count(invocation)
        return invocation

    # ------------------------------------------------------------ invocation
    async def invoke(  # noqa: PLR0913 - one call carries everything the audit records
        self,
        operation: SkillOperation | str,
        *,
        project_id: str,
        protocol: str,
        messages: list[dict[str, Any]],
        validator: Callable[[dict[str, Any]], Any],
        parameters: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        model_roles: Any | None = None,
        fallback_system_prompt: str | None = None,
        asset_criticality: str | None = None,
        on_call: Callable[[list[dict[str, Any]]], Awaitable[None] | None] | None = None,
    ) -> SkillInvocation:
        """Run one operation through its Skill; degrade loudly, never silently.

        ``protocol`` is the application contract appended to the Skill body
        (the JSON shape the stage expects). ``messages`` are the conversation
        and the request; the runtime owns the system message so no caller can
        inject a second Skill. ``validator`` turns the model's JSON object into
        the stage's structured output or raises; an ``AuthorityViolation`` is
        recorded as such. The returned invocation says exactly what happened.
        """

        op = SkillOperation(operation)
        invocation = SkillInvocation(operation=op.value)
        system_prompt: str
        try:
            definition = self.resolve(op)
        except SkillUnavailable as exc:
            logger.warning("skill unavailable for %s: %s", op.value, exc)
            invocation.reason_codes.append(SKILL_UNAVAILABLE)
            if fallback_system_prompt is None:
                invocation.fallback_reason = SKILL_UNAVAILABLE
                self._count(invocation)
                return invocation
            system_prompt = f"{fallback_system_prompt}\n\n{protocol}".strip()
            model_role = None
        else:
            self._stamp(invocation, definition)
            invocation.resolved = True
            invocation.reason_codes.append(SKILL_LOADED)
            system_prompt = f"{definition.system_prompt}\n\n{protocol}".strip()
            model_role = definition.model_role
            self._refuse_foreign_bodies(definition, messages)
        reasoner = model_roles if model_roles is not None else self.model_roles
        if not self.enabled:
            invocation.fallback_reason = SKILL_STAGE_DISABLED
            invocation.reason_codes.append(SKILL_STAGE_DISABLED)
            self._count(invocation)
            return invocation
        if reasoner is None:
            invocation.fallback_reason = MODEL_RUNTIME_NOT_CONFIGURED
            invocation.reason_codes.append(MODEL_RUNTIME_NOT_CONFIGURED)
            self._count(invocation)
            return invocation
        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        invocation.loaded = invocation.resolved
        invocation.context_hash = _hash({"messages": full_messages})
        call_parameters: dict[str, Any] = {"response_format": {"type": "json_object"}}
        if parameters:
            call_parameters.update(parameters)
        if max_tokens:
            call_parameters["max_tokens"] = max_tokens
        if on_call is not None:
            maybe = on_call(full_messages)
            if maybe is not None:
                await maybe
        execution, failure = await self._call(
            reasoner,
            project_id,
            model_role or EXPECTED_MODEL_ROLES.get(op, "DIRECTOR"),
            full_messages,
            call_parameters,
            asset_criticality,
        )
        if failure is not None:
            invocation.fallback_reason = failure[0]
            invocation.reason_codes.extend(failure)
            self._count(invocation)
            return invocation
        invocation.model_invoked = True
        invocation.execution_record_id = getattr(execution, "execution_record_id", None)
        invocation.decision_record_id = getattr(execution, "decision_record_id", None)
        try:
            raw = first_choice_json(getattr(execution, "response", None) or {})
        except (ValueError, TypeError) as exc:
            invocation.fallback_reason = MODEL_OUTPUT_INVALID
            invocation.reason_codes.extend([MODEL_OUTPUT_INVALID, type(exc).__name__])
            invocation.validation_errors.append(str(exc)[:400])
            self._count(invocation)
            return invocation
        invocation.raw = raw
        try:
            validated = validator(raw)
        except AuthorityViolation as exc:
            invocation.fallback_reason = AUTHORITY_VIOLATION
            invocation.authority_violations = list(exc.violations)
            invocation.reason_codes.extend(
                [AUTHORITY_VIOLATION, *(f"{AUTHORITY_VIOLATION}:{item}" for item in exc.violations[:8])]
            )
            self._count(invocation)
            return invocation
        except Exception as exc:  # noqa: BLE001 - every validator failure is a recorded outcome
            invocation.fallback_reason = MODEL_OUTPUT_INVALID
            invocation.reason_codes.extend([MODEL_OUTPUT_INVALID, type(exc).__name__])
            details = getattr(exc, "details", None)
            if isinstance(details, list):
                invocation.validation_errors.extend(str(item)[:200] for item in details[:10])
            else:
                invocation.validation_errors.append(str(exc)[:400])
            self._count(invocation)
            return invocation
        if isinstance(validated, Validated):
            invocation.output = validated.output
            invocation.reason_codes.extend(validated.reason_codes)
            invocation.authority_violations = list(validated.authority_violations)
        else:
            invocation.output = validated
        invocation.execution_mode = EXECUTION_MODEL if invocation.loaded else EXECUTION_MODEL_WITHOUT_SKILL
        invocation.reason_codes.append(MODEL_REPLY)
        self._count(invocation)
        return invocation

    # ---------------------------------------------------------------- matrix
    def bindings(self) -> list[dict[str, Any]]:
        return [definition.binding_json() for definition in self.registry.list_skills()]

    def integration_matrix(self) -> list[dict[str, Any]]:
        """Skill | Registered | Runtime Bound | Body Injected | Structured Validated | Fallback Tracked.

        A Skill counts as integrated only when it is runtime-bound *and* its
        body is injected: registration alone is a file on disk.
        """

        rows: list[dict[str, Any]] = []
        for definition in self.registry.list_skills():
            sites = [
                CALL_SITES[SkillOperation(op)]
                for op in definition.operations
                if op in {item.value for item in SkillOperation} and SkillOperation(op) in CALL_SITES
            ]
            bound = definition.is_runtime and bool(sites) and all(
                EXPECTED_BINDINGS[site.operation] == definition.role for site in sites
            )
            observed = {
                op: dict(self.ledger.get(op, {}))
                for op in definition.operations
                if op in self.ledger
            }
            rows.append(
                {
                    "skill": definition.name,
                    "role": definition.role,
                    "stage": definition.stage,
                    "registered": True,
                    "runtime_bound": bound,
                    "body_injected": bound,
                    "structured_validated": sorted({site.contract for site in sites}) if bound else [],
                    "fallback_tracked": bound,
                    "integrated": bound,
                    "operations": list(definition.operations),
                    "model_role": definition.model_role,
                    "runtime": definition.runtime_kind,
                    "bound_to": definition.bound_to,
                    "call_sites": [site.service for site in sites],
                    "version": definition.version,
                    "content_hash": definition.content_hash,
                    "observed": observed,
                }
            )
        return rows

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _stamp(invocation: SkillInvocation, definition: SkillDefinition) -> None:
        invocation.name = definition.name
        invocation.version = definition.version
        invocation.content_hash = definition.content_hash
        invocation.role = definition.role
        invocation.stage = definition.stage
        invocation.model_role = definition.model_role

    def _refuse_foreign_bodies(self, definition: SkillDefinition, messages: list[dict[str, Any]]) -> None:
        """One invocation, one Skill: any other installed body in the messages is refused."""

        joined = "\n".join(str(message.get("content") or "") for message in messages)
        if not joined:
            return
        for other in self.registry.list_skills():
            if other.name == definition.name:
                continue
            probe = other.system_prompt[:400]
            if probe and probe in joined:
                raise SkillRuntimeError(
                    f"invocation of {definition.name} carries the body of Skill {other.name}; "
                    "one operation loads one Skill"
                )

    def _count(self, invocation: SkillInvocation) -> None:
        entry = self.ledger.setdefault(
            invocation.operation,
            {"total": 0, "model": 0, "model_without_skill": 0, "deterministic": 0, "body_injected": 0},
        )
        entry["total"] += 1
        entry[invocation.execution_mode.lower()] += 1
        if invocation.loaded:
            entry["body_injected"] += 1

    @staticmethod
    async def _call(
        reasoner: Any,
        project_id: str,
        model_role: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any],
        asset_criticality: str | None,
    ) -> tuple[Any, list[str] | None]:
        """One model call under a role. Returns (execution, None) or (None, failure codes)."""

        from entitlement_core.canary import LiveCanaryConflict, LiveSpendDenied
        from model_registry_core import ModelRole
        from provider_sdk import ProviderError, ProviderTrustViolation

        kwargs: dict[str, Any] = {"messages": messages, "parameters": parameters}
        if asset_criticality:
            kwargs["asset_criticality"] = asset_criticality
        try:
            execution = await reasoner.execute_chat(project_id, ModelRole(model_role), **kwargs)
        except (LiveCanaryConflict, LiveSpendDenied) as exc:
            return None, [MODEL_BUDGET_REFUSED, type(exc).__name__]
        except (LookupError, ProviderError, ProviderTrustViolation, TypeError, ValueError) as exc:
            return None, [MODEL_UNAVAILABLE, type(exc).__name__]
        except Exception as exc:  # noqa: BLE001 - recorded, never silent
            logger.warning("skill model call failed (%s): %s", model_role, exc, exc_info=True)
            return None, [MODEL_CALL_ERROR, type(exc).__name__]
        return execution, None


#: The model role each operation runs under when the Skill is unavailable and
#: a caller supplied a fallback prompt: the platform's own role table, so a
#: missing Skill never changes which model is paid for.
EXPECTED_MODEL_ROLES: dict[SkillOperation, str] = {
    SkillOperation.CREATIVE_CONVERSATION: "DIRECTOR",
    SkillOperation.STORY_GENERATION: "DIRECTOR",
    SkillOperation.STORY_REVISION: "DIRECTOR",
    SkillOperation.SHOT_DECOMPOSITION: "SHOT_PLANNER",
    SkillOperation.SHOT_REVISION: "SHOT_PLANNER",
    SkillOperation.CINEMATOGRAPHY_DESIGN: "CINEMATOGRAPHY_REASONING",
    SkillOperation.CONTINUITY_REVIEW: "CONTINUITY_REASONER",
    SkillOperation.PROMPT_COMPILATION: "PROMPT_COMPILER",
}
