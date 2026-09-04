"""The stateful creative director.

The service owns dialogue state, brief revisions, screenplay revisions, the
visual bible and the beat plan. It emits **structured actions** for anything
that spends money or touches the production chain - key visual generation,
identity and style locks, episode creation, ledger writes - and never reaches
a provider itself; the API layer executes generation actions through the
existing admission / credit / router / gateway path, and the locks run
through the platform's own ``CharacterIdentityService`` and
``ProjectStyleService``. Model reasoning goes through ``ModelRoleRuntime``
with the Director Skill as its system prompt and degrades to the deterministic
rules engine with the degradation recorded on the row, never silently.

Every dialogue round is one transaction: the user's message, the director's
reply, the brief operations it applied, the question states it moved and the
brief revision it produced are written together or not at all - a model
failure can never leave an orphan user message, and the FREE dialogue budget
counts only rounds that landed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from platform_database import Database
from production_domain.models import (
    Character,
    CreativeAction,
    CreativeActionStatus,
    CreativeAnchorStatus,
    CreativeBeat,
    CreativeBriefRevision,
    CreativeFormat,
    CreativeScreenplayRevision,
    CreativeSession,
    CreativeSessionStatus,
    CreativeShotLineage,
    CreativeTurn,
    CreativeVisualAnchor,
    GenerationJob,
    JobStatus,
    Project,
    ProjectStyleLock,
    VisualBibleVersion,
    Workspace,
)
from pydantic import ValidationError
from sqlalchemy import func, select

from . import evidence as evidence_module
from .beats import render_script
from .brief import (
    BriefEngine,
    OperationActor,
    apply_operations,
    brief_hash,
    character_key,
    get_path,
    is_assumed,
    reconcile_questions,
    set_path,
)
from .director_context import (
    SkillText,
    build_screenplay_messages,
    build_turn_messages,
)
from .evidence import UserTextIndex, UserUtterance
from .schemas import (
    ASSUMED_SOURCES,
    COMMERCE_FORMATS,
    FORMAT_DEFAULTS,
    MAX_CAST,
    MAX_PROP_ANCHORS,
    MAX_SCENE_ANCHORS,
    RETRYABLE_REASON_CODES,
    SPECS_BY_CODE,
    BriefOperation,
    BriefOperationKind,
    DirectorTurnResult,
    ProvenanceSource,
    QuestionStatus,
    ReasonCode,
    Screenplay,
    StructuredActionKind,
)
from .screenplay import (
    AnchorDerivation,
    ScreenplayInvalid,
    anchor_keys_for_shot,
    apply_beat_edits,
    beats_from_screenplay,
    derive_anchors,
    deterministic_screenplay,
    screenplay_hash,
    script_name,
    validate_screenplay,
)
from .screenplay_brief import ScreenplayBriefValidator

logger = logging.getLogger(__name__)


class CreativeSessionConflict(ValueError):
    """The request contradicts the session's recorded state."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str | None = None,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.reason_code = reason_code
        self.details = details or {}
        self.retryable = retryable

    def as_detail(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "reason_code": self.reason_code,
            "retryable": self.retryable,
            **self.details,
        }


class CreativeTurnLimitReached(CreativeSessionConflict):
    """The FREE plan's per-session dialogue budget is spent.

    A hard server-side gate: the browser can neither see past it nor widen it.
    Upgrading the workspace plan lifts it; the session itself stays readable
    and its approvals keep working.
    """


class ModelReasoner(Protocol):
    """The slice of ModelRoleRuntime the director uses."""

    async def execute_chat(
        self,
        project_id: str,
        role: Any,
        *,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> Any: ...


class EpisodeCompiler(Protocol):
    """The slice of AgentOrchestrator the director hands a shot plan to."""

    def compile_episode(self, episode_id: str) -> Any: ...


class SeriesLedger(Protocol):
    def establish_fact(
        self,
        project_id: str,
        *,
        fact_key: str,
        summary: str,
        episode: int,
        scene_sequence: int = 0,
        shot_sequence: int = 0,
        shot_id: str | None = None,
        subject_character_ids: list[str] | None = None,
        disclose_to: list[str] | None = None,
    ) -> str: ...

    def open_obligation(
        self,
        project_id: str,
        *,
        obligation_key: str,
        promise: str,
        episode: int,
        scene_sequence: int = 0,
        shot_sequence: int = 0,
        shot_id: str | None = None,
        category: str = "GENERIC",
    ) -> str: ...


class SkillResolver(Protocol):
    """The slice of SkillRegistry the director uses."""

    def resolve(self, name: str) -> Any: ...


class IdentityLocker(Protocol):
    """The slice of CharacterIdentityService the bible lock uses."""

    def confirm_identity(
        self,
        character_id: str,
        master_asset_id: str,
        *,
        references: dict[str, str | None] | None = None,
        hair_signature: str = "",
        costume_signature: str = "",
    ) -> Any: ...


class StyleLocker(Protocol):
    """The slice of ProjectStyleService the bible lock uses."""

    def lock(
        self,
        project_id: str,
        style_version_id: str,
        *,
        locked_by_user_id: str,
        reason: str,
        explicit_confirmation: bool,
    ) -> Any: ...


class LogicalAssets(Protocol):
    """The slice of AssetRegistry the bible lock uses."""

    def list(
        self, project_id: str, *, asset_type: Any = None, include_archived: bool = False
    ) -> list[Any]: ...

    def create(
        self,
        project_id: str,
        asset_type: Any,
        name: str,
        *,
        description: str = "",
        canonical_metadata: Any = None,
        created_by_user_id: str | None = None,
    ) -> Any: ...

    def add_version(
        self,
        asset_id: str,
        *,
        primary_media_asset_id: str | None = None,
        label: str = "",
        metadata: Any = None,
        source: str = "USER_UPLOAD",
        created_by_user_id: str | None = None,
    ) -> Any: ...

    def promote(
        self,
        asset_id: str,
        version_id: str,
        *,
        promoted_by_user_id: str | None = None,
        reason: str = "",
    ) -> Any: ...

    def annotate(self, asset_id: str, *, canonical_metadata: Any) -> Any: ...


DIRECTOR_SKILL_NAME = "director"

#: The system prompt used only when the Skill registry cannot supply the
#: Director Skill. Recorded as SKILL_UNAVAILABLE on the turn; never silent.
_SKILL_FALLBACK_PROMPT = (
    "You are BestShiny Director, a film and commercial creative director in conversation with a "
    "client. Lock the client's facts verbatim, state the promise, ask only for what is missing and "
    "high-value, and never invent an answer to an open question."
)

_CLOSED_STATUSES = {
    CreativeSessionStatus.COMPILED.value,
    CreativeSessionStatus.ABANDONED.value,
}
_DIALOGUE_STATUSES = {
    CreativeSessionStatus.INTAKE.value,
    CreativeSessionStatus.CLARIFYING.value,
    CreativeSessionStatus.BRIEF_PROPOSED.value,
}
#: Anchor kinds whose READY key visual becomes a canonical logical asset when
#: the visual bible locks. CHARACTER goes through CharacterIdentityService and
#: STYLE through ProjectStyleService, so both are handled on their own paths.
_CANONICAL_ANCHOR_KINDS = ("SCENE", "PRODUCT", "PROP")

#: The only stages a screenplay revision may be written from. Once the
#: screenplay is approved and its key visuals are derived, a redraft that was
#: started earlier is stale by definition.
_SCREENPLAY_DRAFT_STATUSES = frozenset(
    {
        CreativeSessionStatus.BRIEF_APPROVED.value,
        CreativeSessionStatus.SCREENPLAY_PROPOSED.value,
    }
)


@dataclass(frozen=True)
class DirectorReply:
    """One director turn as the UI renders it."""

    session_id: str
    status: str
    message: str
    questions: list[dict[str, Any]]
    brief_revision: int
    proposable: bool
    reasoner: str
    reason_codes: list[str] = field(default_factory=list)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    blocking: list[dict[str, Any]] = field(default_factory=list)
    creative_notes: list[str] = field(default_factory=list)
    retryable: bool = False
    turn_sequence: int = 0
    replayed: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "message": self.message,
            "questions": self.questions,
            "brief_revision": self.brief_revision,
            "proposable": self.proposable,
            "reasoner": self.reasoner,
            "reason_codes": list(self.reason_codes),
            "assumptions": list(self.assumptions),
            "blocking": list(self.blocking),
            "creative_notes": list(self.creative_notes),
            "retryable": self.retryable,
            "turn_sequence": self.turn_sequence,
            "replayed": self.replayed,
        }


@dataclass(frozen=True)
class CreativeSessionState:
    session: dict[str, Any]
    brief: dict[str, Any] | None
    turns: list[dict[str, Any]]
    anchors: list[dict[str, Any]]
    bible: dict[str, Any] | None
    beats: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    screenplay: dict[str, Any] | None = None
    screenplays: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _TurnReasoning:
    result: DirectorTurnResult | None
    reasoner: str
    reason_codes: list[str]
    audit: dict[str, Any]
    execution_record_id: str | None
    retryable: bool
    skill_version: str | None
    skill_content_hash: str | None
    fallback_message: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _is_cjk_text(value: str) -> bool:
    return any("一" <= char <= "鿿" for char in value)


class CreativeDirectorService:
    version = "creative-director-v2"

    def __init__(
        self,
        database: Database,
        *,
        orchestrator: EpisodeCompiler | None = None,
        ledger: SeriesLedger | None = None,
        model_roles: ModelReasoner | None = None,
        skills: SkillResolver | None = None,
        characters: IdentityLocker | None = None,
        styles: StyleLocker | None = None,
        asset_registry: LogicalAssets | None = None,
        free_plan_turn_limit: int = 10,
    ):
        self.database = database
        self.orchestrator = orchestrator
        self.ledger = ledger
        self.model_roles = model_roles
        self.skills = skills
        self.characters = characters
        self.styles = styles
        self.asset_registry = asset_registry
        self.free_plan_turn_limit = max(0, int(free_plan_turn_limit))
        self.briefs = BriefEngine()
        #: Compares every screenplay revision with the brief the user approved.
        self.brief_validator = ScreenplayBriefValidator()

    # ------------------------------------------------------------- the skill
    def _skill(self) -> tuple[SkillText, list[str]]:
        """The Director Skill text, content-addressed; a missing skill is recorded."""

        if self.skills is None:
            return SkillText(_SKILL_FALLBACK_PROMPT, None, None), [ReasonCode.SKILL_UNAVAILABLE.value]
        try:
            definition = self.skills.resolve(DIRECTOR_SKILL_NAME)
        except (LookupError, ValueError, OSError) as exc:
            logger.warning("director skill unavailable: %s", exc)
            return SkillText(_SKILL_FALLBACK_PROMPT, None, None), [ReasonCode.SKILL_UNAVAILABLE.value]
        return (
            SkillText(
                str(definition.system_prompt),
                str(getattr(definition, "version", "") or None),
                str(getattr(definition, "content_hash", "") or None),
            ),
            [ReasonCode.SKILL_LOADED.value],
        )

    # ------------------------------------------------------------ reasoning
    async def _reason_turn(
        self,
        project_id: str,
        *,
        turns: list[dict[str, Any]],
        fields: dict[str, Any],
        provenance: dict[str, Any],
        question_states: dict[str, Any],
        stage: str,
        format_value: str,
        content: str,
        gap_candidates: list[dict[str, Any]],
    ) -> _TurnReasoning:
        """Ask the DIRECTOR role, through the Skill, for one validated turn; degrade loudly."""

        if self.model_roles is None:
            return _TurnReasoning(
                None,
                "DETERMINISTIC",
                [ReasonCode.MODEL_RUNTIME_NOT_CONFIGURED.value],
                {},
                None,
                False,
                None,
                None,
            )
        skill, skill_codes = self._skill()
        messages, audit = build_turn_messages(
            skill=skill,
            turns=turns,
            fields=fields,
            provenance=provenance,
            question_states=question_states,
            stage=stage,
            format_value=format_value,
            latest_user_message=content,
            approved={},
            analysis_questions=gap_candidates,
        )
        codes = list(skill_codes)
        if audit.compressed:
            codes.append(ReasonCode.CONTEXT_COMPRESSED.value)
        execution, failure = await self._call_model(project_id, messages)
        if failure is not None:
            return _TurnReasoning(
                None,
                "DETERMINISTIC",
                codes + failure,
                audit.as_json(),
                None,
                True,
                skill.version,
                skill.content_hash,
            )
        execution_id = getattr(execution, "execution_record_id", None)
        try:
            raw = _first_choice_json(execution.response)
            result, parse_codes = _parse_turn_result(raw)
        except (ValueError, TypeError, ValidationError) as exc:
            return _TurnReasoning(
                None,
                "DETERMINISTIC",
                codes + [ReasonCode.MODEL_OUTPUT_INVALID.value, type(exc).__name__],
                audit.as_json(),
                execution_id,
                True,
                skill.version,
                skill.content_hash,
            )
        return _TurnReasoning(
            result,
            "MODEL:DIRECTOR",
            codes + parse_codes,
            audit.as_json(),
            execution_id,
            False,
            skill.version,
            skill.content_hash,
        )

    async def _call_model(
        self, project_id: str, messages: list[dict[str, Any]], *, max_tokens: int | None = None
    ) -> tuple[Any, list[str] | None]:
        """One DIRECTOR call. Returns (execution, None) or (None, failure reason codes)."""

        from entitlement_core.canary import LiveCanaryConflict, LiveSpendDenied
        from model_registry_core import ModelRole
        from provider_sdk import ProviderError, ProviderTrustViolation

        parameters: dict[str, Any] = {"response_format": {"type": "json_object"}}
        if max_tokens:
            parameters["max_tokens"] = max_tokens
        assert self.model_roles is not None
        try:
            execution = await self.model_roles.execute_chat(
                project_id, ModelRole.DIRECTOR, messages=messages, parameters=parameters
            )
        except (LiveCanaryConflict, LiveSpendDenied) as exc:
            # A refused live-spend reservation is a budget refusal, not a
            # platform fault: the turn degrades loudly instead of failing the
            # user's whole request with a 500.
            return None, [ReasonCode.MODEL_BUDGET_REFUSED.value, type(exc).__name__]
        except (LookupError, ProviderError, ProviderTrustViolation, TypeError, ValueError) as exc:
            return None, [ReasonCode.MODEL_UNAVAILABLE.value, type(exc).__name__]
        except Exception as exc:  # noqa: BLE001 - recorded, never silent
            logger.warning("director model call failed: %s", exc, exc_info=True)
            return None, [ReasonCode.MODEL_CALL_ERROR.value, type(exc).__name__]
        return execution, None

    # ------------------------------------------------------------- dialogue
    async def start_session(
        self,
        project_id: str,
        *,
        idea: str,
        workspace_id: str | None = None,
        format_hint: str | None = None,
        title: str = "",
        client_turn_id: str | None = None,
    ) -> DirectorReply:
        with self.database.session() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise LookupError("project not found")
            row = CreativeSession(
                project_id=project_id,
                workspace_id=workspace_id or project.workspace_id,
                title=title or (idea.strip()[:200] or "Untitled session"),
            )
            session.add(row)
            session.flush()
            session_id = row.id
        return await self._user_turn(session_id, idea, format_hint=format_hint, client_turn_id=client_turn_id)

    async def post_message(
        self,
        session_id: str,
        content: str,
        *,
        client_turn_id: str | None = None,
        expected_brief_revision: int | None = None,
    ) -> DirectorReply:
        """One dialogue round. The pre-flight read takes the same lock phase 3 will.

        Reading the row unlocked let a session close (or be approved) between
        the check and the model call, so the user paid for a call phase 3 then
        refused. ``expected_brief_revision`` lets a client pin the revision it
        was looking at and be told the head moved rather than have its message
        rebased onto someone else's newer brief.
        """

        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            if row.status in _CLOSED_STATUSES:
                raise CreativeSessionConflict(
                    f"session is {row.status}; the dialogue is closed", reason_code="DIALOGUE_CLOSED"
                )
            if row.status not in _DIALOGUE_STATUSES:
                raise CreativeSessionConflict(
                    f"the brief is no longer in dialogue; session is {row.status}",
                    reason_code=ReasonCode.SESSION_STAGE_CHANGED.value,
                    details={"status": row.status},
                )
            if (
                expected_brief_revision is not None
                and expected_brief_revision != row.current_brief_revision
            ):
                raise CreativeSessionConflict(
                    f"brief revision {expected_brief_revision} is superseded by "
                    f"{row.current_brief_revision}",
                    reason_code=ReasonCode.BRIEF_REVISION_CHANGED.value,
                    details={
                        "expected_brief_revision": expected_brief_revision,
                        "brief_revision": row.current_brief_revision,
                    },
                    retryable=True,
                )
        return await self._user_turn(
            session_id,
            content,
            format_hint=None,
            client_turn_id=client_turn_id,
            expected_brief_revision=expected_brief_revision,
        )

    def _assert_turn_budget(self, session, row) -> None:  # type: ignore[no-untyped-def]
        """FREE workspaces get a bounded number of dialogue rounds per session."""

        if not row.workspace_id:
            return
        workspace = session.get(Workspace, row.workspace_id)
        if workspace is None or workspace.plan_tier != "FREE":
            return
        used = session.scalar(
            select(func.count())
            .select_from(CreativeTurn)
            .where(CreativeTurn.session_id == row.id, CreativeTurn.speaker == "USER")
        )
        if int(used or 0) >= self.free_plan_turn_limit:
            raise CreativeTurnLimitReached(
                f"the Free plan includes {self.free_plan_turn_limit} director rounds per "
                "session; upgrade to Pro to keep the conversation going",
                reason_code="FREE_TURN_LIMIT",
            )

    def _replay(self, session: Any, row: CreativeSession, client_turn_id: str | None) -> DirectorReply | None:
        if not client_turn_id:
            return None
        user_turn = session.scalar(
            select(CreativeTurn).where(
                CreativeTurn.session_id == row.id,
                CreativeTurn.speaker == "USER",
                CreativeTurn.client_turn_id == client_turn_id,
            )
        )
        if user_turn is None:
            return None
        director_turn = session.scalar(
            select(CreativeTurn).where(
                CreativeTurn.session_id == row.id,
                CreativeTurn.speaker == "DIRECTOR",
                CreativeTurn.sequence == user_turn.sequence + 1,
            )
        )
        if director_turn is None:
            return None
        brief = self._brief_at(session, row.id, director_turn.brief_revision)
        completeness = dict(brief.completeness_json) if brief is not None else {}
        result = dict(director_turn.result_json or {})
        return DirectorReply(
            session_id=row.id,
            status=row.status,
            message=director_turn.content,
            questions=list(director_turn.questions_json or []),
            brief_revision=director_turn.brief_revision,
            proposable=bool(completeness.get("proposable")),
            reasoner=director_turn.reasoner,
            reason_codes=[*director_turn.reason_codes, ReasonCode.IDEMPOTENT_REPLAY.value],
            assumptions=list(completeness.get("assumptions") or []),
            blocking=list(completeness.get("blocking") or []),
            creative_notes=list(result.get("creative_notes") or []),
            retryable=False,
            turn_sequence=director_turn.sequence,
            replayed=True,
        )

    async def _user_turn(
        self,
        session_id: str,
        content: str,
        *,
        format_hint: str | None,
        client_turn_id: str | None,
        expected_brief_revision: int | None = None,
    ) -> DirectorReply:
        """One dialogue round: read, reason outside any transaction, then write.

        Phase 2 is deliberately outside a transaction - a model failure must
        never leave a user message without its result - so phase 3 has to
        assume the world moved. It re-reads the head brief under the row lock
        and re-applies the model's operations to *that* revision under the same
        provenance rules, rather than writing the phase-1 snapshot back over a
        concurrent edit. The rebase is recorded as BRIEF_REBASED; a session
        that left the dialogue stage during the call is refused outright, so an
        in-flight turn can never un-approve an approved brief.
        """

        # Phase 1 - read. Nothing is written until the director has answered.
        with self.database.session() as session:
            row = self._session(session, session_id)
            replay = self._replay(session, row, client_turn_id)
            if replay is not None:
                return replay
            self._assert_turn_budget(session, row)
            project_id = row.project_id
            status_before = row.status
            brief_revision_at_read = row.current_brief_revision
            head = self._head_brief(session, row)
            fields = dict(head.fields_json) if head is not None else {}
            provenance = dict(head.provenance_json) if head is not None else {}
            question_states = dict(head.question_state_json) if head is not None else {}
            turns = self._turn_views(session, session_id)

        hint_operations: list[BriefOperation] = []
        if format_hint:
            hint_operations.append(
                BriefOperation(
                    op=BriefOperationKind.REPLACE if fields.get("format") else BriefOperationKind.SET,
                    path="format",
                    value=CreativeFormat(format_hint).value,
                    evidence="format chosen by the client",
                    confidence="USER_STATED",
                )
            )
        format_value = str(fields.get("format") or CreativeFormat.UNSPECIFIED.value)
        preview = self.briefs.analyze(fields, provenance, question_states, format_value=format_value)
        gap_candidates = [
            {"code": gap.code, "question": gap.question, "weight": gap.weight, "status": gap.status}
            for gap in preview.questions
        ]

        # Phase 2 - reason, outside any transaction.
        reasoning = await self._reason_turn(
            project_id,
            turns=turns,
            fields=fields,
            provenance=provenance,
            question_states=question_states,
            stage=status_before,
            format_value=format_value,
            content=content,
            gap_candidates=gap_candidates,
        )

        # Phase 3 - write everything, or nothing.
        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            replay = self._replay(session, row, client_turn_id)
            if replay is not None:
                return replay
            self._assert_turn_budget(session, row)
            if row.status in _CLOSED_STATUSES:
                raise CreativeSessionConflict(
                    f"session is {row.status}; the dialogue is closed", reason_code="DIALOGUE_CLOSED"
                )
            if row.status not in _DIALOGUE_STATUSES:
                # The brief was approved (or the session moved further) while
                # the director was thinking. Writing this turn would supersede
                # the APPROVED revision and drag the session back to
                # BRIEF_PROPOSED - a state no single-threaded path produces.
                raise CreativeSessionConflict(
                    f"the session moved from {status_before} to {row.status} while the director "
                    "was answering; the brief is no longer in dialogue",
                    reason_code=ReasonCode.SESSION_STAGE_CHANGED.value,
                    details={"expected_status": status_before, "status": row.status},
                )
            if (
                expected_brief_revision is not None
                and expected_brief_revision != row.current_brief_revision
            ):
                raise CreativeSessionConflict(
                    f"brief revision {expected_brief_revision} is superseded by "
                    f"{row.current_brief_revision}",
                    reason_code=ReasonCode.BRIEF_REVISION_CHANGED.value,
                    details={
                        "expected_brief_revision": expected_brief_revision,
                        "brief_revision": row.current_brief_revision,
                    },
                    retryable=True,
                )
            if row.current_brief_revision != brief_revision_at_read:
                # Someone else's edit landed during the model call. Re-base on
                # it: `apply_operations` will refuse anything the model may not
                # move now that the fact belongs to the user.
                head = self._head_brief(session, row)
                fields = dict(head.fields_json) if head is not None else {}
                provenance = dict(head.provenance_json) if head is not None else {}
                question_states = dict(head.question_state_json) if head is not None else {}
                rebased_from = brief_revision_at_read
            else:
                rebased_from = None
            user_sequence = self._next_turn_sequence(session, session_id)
            user_turn = CreativeTurn(
                session_id=session_id,
                sequence=user_sequence,
                speaker="USER",
                content=content,
                reasoner="USER",
                client_turn_id=client_turn_id,
            )
            session.add(user_turn)
            session.flush()
            director_sequence = user_sequence + 1
            revision = row.current_brief_revision + 1
            now = _now().isoformat()
            result = reasoning.result
            reason_codes = list(reasoning.reason_codes)
            if rebased_from is not None:
                reason_codes.append(ReasonCode.BRIEF_REBASED.value)

            # The user's own words for this session, newest last: what a
            # USER_STATED claim has to be found in. Read under the lock, not
            # from the phase-1 snapshot, so a turn that landed during the model
            # call still counts as something the user said.
            evidence_index = UserTextIndex(
                UserUtterance(turn_id=turn.id, turn_sequence=turn.sequence, text=turn.content)
                for turn in session.scalars(
                    select(CreativeTurn)
                    .where(CreativeTurn.session_id == session_id, CreativeTurn.speaker == "USER")
                    .order_by(CreativeTurn.sequence)
                )
            )
            # The format the client chose in the request body is the client's
            # own act, like a brief-editor edit - not a quote to be found in
            # prose. It is applied under its own actor so verification cannot
            # demote it.
            fields1, provenance1, hint_outcome = apply_operations(
                fields,
                provenance,
                hint_operations,
                OperationActor(
                    reasoner="USER",
                    turn_id=user_turn.id,
                    turn_sequence=user_sequence,
                    revision=revision,
                    at=now,
                    direct_user_edit=True,
                ),
            )
            actor = OperationActor(
                reasoner=reasoning.reasoner,
                turn_id=user_turn.id,
                turn_sequence=user_sequence,
                revision=revision,
                at=now,
                evidence_index=evidence_index,
            )
            operations: list[BriefOperation] = list(result.brief_operations) if result is not None else []
            fields2, provenance2, outcome = apply_operations(fields1, provenance1, operations, actor)
            applied = [*hint_outcome.applied, *outcome.applied]
            rejected = [*hint_outcome.rejected, *outcome.rejected]
            outcome.answered_codes |= hint_outcome.answered_codes
            if any(
                item.get("claimed") == ProvenanceSource.USER_STATED.value for item in outcome.rejected
            ):
                reason_codes.append(ReasonCode.EVIDENCE_UNVERIFIED.value)
            if result is not None:
                assumption_ops = [
                    BriefOperation(
                        op=BriefOperationKind.SET,
                        path=item.path,
                        value=item.value,
                        evidence=item.rationale,
                        confidence="INFERRED",
                    )
                    for item in result.assumptions
                    if item.value is not None
                ]
                fields2, provenance2, assumed_outcome = apply_operations(
                    fields2, provenance2, assumption_ops, actor
                )
                applied.extend(assumed_outcome.applied)
                if outcome.applied:
                    reason_codes.append(ReasonCode.MODEL_OPERATIONS_APPLIED.value)
            # The rules engine fills what is still empty from the user's own
            # words; when it is the only reasoner it also proposes the logline.
            deterministic_ops = self.briefs.extract_operations(
                content, fields2, include_logline=result is None
            )
            if deterministic_ops:
                fields2, provenance2, fill_outcome = apply_operations(
                    fields2,
                    provenance2,
                    deterministic_ops,
                    OperationActor(
                        reasoner="DETERMINISTIC",
                        turn_id=user_turn.id,
                        turn_sequence=user_sequence,
                        revision=revision,
                        at=now,
                    ),
                )
                if fill_outcome.applied:
                    applied.extend(fill_outcome.applied)
                    reason_codes.append(ReasonCode.DETERMINISTIC_FILL.value)
            if rejected:
                reason_codes.append(ReasonCode.OPERATIONS_REJECTED.value)

            format_value = str(fields2.get("format") or CreativeFormat.UNSPECIFIED.value)
            claimed_skips = list(result.skipped_question_codes) if result is not None else []
            skipped, refused_skips = self._verified_skips(
                result, question_states, evidence_index
            )
            if refused_skips:
                reason_codes.append(ReasonCode.SKIP_UNVERIFIED.value)
                rejected.extend(refused_skips)
            states2 = reconcile_questions(
                question_states,
                fields2,
                provenance2,
                asked_now=[],
                skipped_now=skipped,
                turn_sequence=director_sequence,
            )
            proposed_codes = (
                [question.code.upper() for question in result.unresolved_questions]
                if result is not None
                else None
            )
            analysis = self.briefs.analyze(
                fields2, provenance2, states2, format_value=format_value, proposed_questions=proposed_codes
            )
            if analysis.proposable and analysis.applied_defaults:
                for path, value in analysis.applied_defaults.items():
                    if get_path(fields2, path) is None:
                        set_path(fields2, path, value)
                        provenance2[path] = {
                            "source": ProvenanceSource.DEFAULT.value,
                            "operation": "SET",
                            "reasoner": "DEFAULT",
                            "turn_id": user_turn.id,
                            "turn_sequence": user_sequence,
                            "evidence": f"format default for {format_value}",
                            "revision": revision,
                            "at": now,
                        }
                states2 = reconcile_questions(
                    states2,
                    fields2,
                    provenance2,
                    asked_now=[],
                    skipped_now=[],
                    turn_sequence=director_sequence,
                )
                analysis = self.briefs.analyze(
                    fields2,
                    provenance2,
                    states2,
                    format_value=format_value,
                    proposed_questions=proposed_codes,
                )
            asked_codes = [gap.code for gap in analysis.questions] if not analysis.proposable else []
            states3 = reconcile_questions(
                states2,
                fields2,
                provenance2,
                asked_now=asked_codes,
                skipped_now=[],
                turn_sequence=director_sequence,
            )
            model_wording = (
                {
                    question.code.upper(): question.question
                    for question in result.unresolved_questions
                    if question.question
                }
                if result is not None
                else {}
            )
            questions = (
                [
                    {
                        "code": gap.code,
                        "question": model_wording.get(gap.code) or gap.question,
                        "weight": gap.weight,
                        "status": (states3.get(gap.code) or {}).get("status"),
                    }
                    for gap in analysis.questions
                ]
                if not analysis.proposable
                else []
            )

            if result is not None and result.assistant_message:
                message = result.assistant_message
            elif reasoning.fallback_message:
                message = reasoning.fallback_message
            else:
                message = _deterministic_message(content, proposable=analysis.proposable)

            row.format = format_value
            previous = session.scalar(
                select(CreativeBriefRevision).where(
                    CreativeBriefRevision.session_id == session_id,
                    CreativeBriefRevision.status == "PROPOSED",
                )
            )
            if previous is not None:
                previous.status = "SUPERSEDED"
            brief_row = CreativeBriefRevision(
                session_id=session_id,
                revision=revision,
                fields_json=fields2,
                completeness_json=analysis.completeness(),
                content_hash=brief_hash(fields2),
                provenance_json=provenance2,
                question_state_json=states3,
                source="TURN",
                turn_id=user_turn.id,
            )
            session.add(brief_row)
            row.current_brief_revision = revision
            row.status = (
                CreativeSessionStatus.BRIEF_PROPOSED.value
                if analysis.proposable
                else CreativeSessionStatus.CLARIFYING.value
            )
            director_turn = CreativeTurn(
                session_id=session_id,
                sequence=director_sequence,
                speaker="DIRECTOR",
                content=message,
                questions_json=questions,
                extracted_json=applied,
                reasoner=reasoning.reasoner,
                reason_codes=reason_codes,
                brief_revision=revision,
                skill_version=reasoning.skill_version,
                skill_content_hash=reasoning.skill_content_hash,
                model_execution_record_id=reasoning.execution_record_id,
                context_json=reasoning.audit,
                result_json={
                    "assumptions": analysis.assumptions,
                    "blocking": analysis.blocking,
                    "unresolved_questions": [q.model_dump() for q in result.unresolved_questions]
                    if result
                    else [],
                    "answered_question_codes": sorted(outcome.answered_codes),
                    "skipped_question_codes": skipped,
                    "claimed_skipped_question_codes": claimed_skips,
                    "refused_skips": refused_skips,
                    "creative_notes": list(result.creative_notes) if result else [],
                    "rejected_operations": rejected,
                    "retryable": reasoning.retryable,
                },
            )
            session.add(director_turn)
            session.flush()
            return DirectorReply(
                session_id=session_id,
                status=row.status,
                message=message,
                questions=questions,
                brief_revision=revision,
                proposable=analysis.proposable,
                reasoner=reasoning.reasoner,
                reason_codes=reason_codes,
                assumptions=analysis.assumptions,
                blocking=analysis.blocking,
                creative_notes=list(result.creative_notes) if result else [],
                retryable=reasoning.retryable,
                turn_sequence=director_sequence,
            )

    @staticmethod
    def _session_prohibitions(turns: list[dict[str, Any]]) -> list[str]:
        """Every sentence in which the user forbade something, verbatim.

        The director is already shown these; the validator checks the finished
        screenplay against them so a prohibition is enforced, not just quoted.
        """

        found: list[str] = []
        for turn in turns:
            if turn.get("speaker") != "USER":
                continue
            found.extend(BriefEngine.prohibitions(str(turn.get("content") or "")))
        return found

    @staticmethod
    def _verified_skips(
        result: DirectorTurnResult | None,
        question_states: dict[str, Any],
        evidence_index: UserTextIndex,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Which claimed skips the user's own record actually supports.

        A skip silences a gap for good and can make an incomplete brief
        proposable, so the model's word alone never carries one. A code is
        honoured when the question was genuinely asked *and* the model quotes
        the user declining it; everything else is recorded as refused, with the
        claim preserved so the audit shows what was asserted.
        """

        if result is None:
            return [], []
        honoured: list[str] = []
        refused: list[dict[str, Any]] = []
        proofs = {claim.code: claim for claim in result.skipped_questions}
        for code in dict.fromkeys(result.skipped_question_codes):
            state = question_states.get(code)
            asked = bool(isinstance(state, dict) and state.get("asked_turns"))
            claim = proofs.get(code)
            if not asked:
                refused.append({"op": "SKIP", "path": code, "reason": "QUESTION_WAS_NEVER_ASKED"})
                continue
            if claim is None:
                refused.append({"op": "SKIP", "path": code, "reason": evidence_module.NO_EVIDENCE})
                continue
            verdict = evidence_index.verify(claim.evidence, turn_id=claim.evidence_turn_id)
            if not verdict.verified:
                refused.append(
                    {
                        "op": "SKIP",
                        "path": code,
                        "reason": verdict.reason,
                        "evidence": claim.evidence[:300],
                    }
                )
                continue
            honoured.append(code)
        return honoured, refused

    # ------------------------------------------------------------ brief edits
    def edit_brief(self, session_id: str, operations: list[dict[str, Any]], *, actor: str) -> dict[str, Any]:
        """The brief editor: the user's own operations, through the same provenance path."""

        try:
            parsed = [BriefOperation.model_validate(item) for item in operations]
        except ValidationError as exc:
            raise ValueError(f"invalid brief operations: {exc.errors()[:3]}") from exc
        if not parsed:
            raise ValueError("no brief operations supplied")
        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            if row.status not in _DIALOGUE_STATUSES:
                raise CreativeSessionConflict(
                    f"the brief can only be edited before approval; session is {row.status}",
                    reason_code="BRIEF_NOT_EDITABLE",
                )
            head = self._head_brief(session, row)
            fields = dict(head.fields_json) if head is not None else {}
            provenance = dict(head.provenance_json) if head is not None else {}
            states = dict(head.question_state_json) if head is not None else {}
            revision = row.current_brief_revision + 1
            actor_context = OperationActor(
                reasoner="USER",
                turn_id=None,
                turn_sequence=None,
                revision=revision,
                at=_now().isoformat(),
                direct_user_edit=True,
            )
            fields2, provenance2, outcome = apply_operations(fields, provenance, parsed, actor_context)
            brief_row = self._write_brief_revision(
                session, row, fields2, provenance2, states, source="USER_EDIT", turn_id=None
            )
            view = self._brief_view(brief_row)
            view["applied"] = outcome.applied
            view["rejected"] = outcome.rejected
            view["session_status"] = row.status
            _ = actor
            return view

    def resolve_question(
        self,
        session_id: str,
        *,
        code: str,
        action: str,
        value: Any = None,
        actor: str,
    ) -> dict[str, Any]:
        """The user accepts the director's assumption for a gap, or declines to answer it."""

        code = code.strip().upper()
        spec = SPECS_BY_CODE.get(code)
        if spec is None:
            raise ValueError(f"unknown question code {code!r}")
        action = action.strip().upper()
        if action not in {"ACCEPT_ASSUMPTION", "SKIP"}:
            raise ValueError("action must be ACCEPT_ASSUMPTION or SKIP")
        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            if row.status not in _DIALOGUE_STATUSES:
                raise CreativeSessionConflict(
                    f"questions can only be resolved before approval; session is {row.status}",
                    reason_code="BRIEF_NOT_EDITABLE",
                )
            head = self._head_brief(session, row)
            fields = dict(head.fields_json) if head is not None else {}
            provenance = dict(head.provenance_json) if head is not None else {}
            states = dict(head.question_state_json) if head is not None else {}
            revision = row.current_brief_revision + 1
            now = _now().isoformat()
            format_value = str(fields.get("format") or CreativeFormat.UNSPECIFIED.value)
            state = states.setdefault(code, {"status": QuestionStatus.UNASKED.value, "asked_turns": []})
            current_value = get_path(fields, spec.path)
            if action == "ACCEPT_ASSUMPTION":
                chosen = value if value is not None else current_value
                if chosen is None:
                    chosen = FORMAT_DEFAULTS.get(format_value, {}).get(spec.path)
                if chosen is None:
                    raise CreativeSessionConflict(
                        f"there is no assumption to accept for {code}; answer it instead",
                        reason_code="NO_ASSUMPTION",
                    )
                operation = BriefOperation(
                    op=BriefOperationKind.REPLACE if current_value is not None else BriefOperationKind.SET,
                    path=spec.path,
                    value=chosen,
                    evidence=f"assumption accepted by {actor}",
                    confidence="USER_STATED",
                )
                actor_context = OperationActor(
                    reasoner="USER",
                    turn_id=None,
                    turn_sequence=None,
                    revision=revision,
                    at=now,
                    direct_user_edit=True,
                )
                fields, provenance, outcome = apply_operations(fields, provenance, [operation], actor_context)
                if outcome.rejected and not outcome.applied:
                    raise ValueError(f"assumption value rejected: {outcome.rejected[0]['reason']}")
                key = (
                    spec.path
                    if spec.path != "characters"
                    else character_key(
                        str((chosen[0] if isinstance(chosen, list) and chosen else {}).get("name", ""))
                    )
                )
                record = provenance.get(key)
                if record is not None:
                    record["source"] = ProvenanceSource.ASSUMPTION_ACCEPTED.value
                    record["accepted_by"] = actor
                state["status"] = QuestionStatus.ASSUMPTION_ACCEPTED.value
                state["accepted_value"] = chosen
            else:
                state["status"] = QuestionStatus.SKIPPED_BY_USER.value
                default = FORMAT_DEFAULTS.get(format_value, {}).get(spec.path)
                if default is not None and get_path(fields, spec.path) is None:
                    set_path(fields, spec.path, default)
                    provenance[spec.path] = {
                        "source": ProvenanceSource.DEFAULT.value,
                        "operation": "SET",
                        "reasoner": "DEFAULT",
                        "turn_id": None,
                        "turn_sequence": None,
                        "evidence": f"format default for {format_value}, after the client skipped {code}",
                        "revision": revision,
                        "at": now,
                    }
            brief_row = self._write_brief_revision(
                session, row, fields, provenance, states, source="ASSUMPTION", turn_id=None
            )
            view = self._brief_view(brief_row)
            view["session_status"] = row.status
            return view

    def _write_brief_revision(
        self,
        session: Any,
        row: CreativeSession,
        fields: dict[str, Any],
        provenance: dict[str, Any],
        states: dict[str, Any],
        *,
        source: str,
        turn_id: str | None,
    ) -> CreativeBriefRevision:
        """Append a PROPOSED revision from edited state and move the session status."""

        revision = row.current_brief_revision + 1
        format_value = str(fields.get("format") or CreativeFormat.UNSPECIFIED.value)
        states2 = reconcile_questions(
            states, fields, provenance, asked_now=[], skipped_now=[], turn_sequence=None
        )
        analysis = self.briefs.analyze(fields, provenance, states2, format_value=format_value)
        if analysis.proposable and analysis.applied_defaults:
            for path, value in analysis.applied_defaults.items():
                if get_path(fields, path) is None:
                    set_path(fields, path, value)
                    provenance[path] = {
                        "source": ProvenanceSource.DEFAULT.value,
                        "operation": "SET",
                        "reasoner": "DEFAULT",
                        "turn_id": turn_id,
                        "turn_sequence": None,
                        "evidence": f"format default for {format_value}",
                        "revision": revision,
                        "at": _now().isoformat(),
                    }
            states2 = reconcile_questions(
                states2, fields, provenance, asked_now=[], skipped_now=[], turn_sequence=None
            )
            analysis = self.briefs.analyze(fields, provenance, states2, format_value=format_value)
        previous = session.scalar(
            select(CreativeBriefRevision).where(
                CreativeBriefRevision.session_id == row.id,
                CreativeBriefRevision.status == "PROPOSED",
            )
        )
        if previous is not None:
            previous.status = "SUPERSEDED"
        brief_row = CreativeBriefRevision(
            session_id=row.id,
            revision=revision,
            fields_json=fields,
            completeness_json=analysis.completeness(),
            content_hash=brief_hash(fields),
            provenance_json=provenance,
            question_state_json=states2,
            source=source,
            turn_id=turn_id,
        )
        session.add(brief_row)
        row.current_brief_revision = revision
        row.format = format_value
        row.status = (
            CreativeSessionStatus.BRIEF_PROPOSED.value
            if analysis.proposable
            else CreativeSessionStatus.CLARIFYING.value
        )
        session.flush()
        return brief_row

    # ------------------------------------------------------------- approval
    def approve_brief(
        self,
        session_id: str,
        *,
        revision: int,
        actor: str,
        accept_assumptions: bool = False,
    ) -> dict[str, Any]:
        """Freeze the brief. Enforced here, not by a hidden button.

        Refused while CLARIFYING, while any CRITICAL field is neither answered
        nor an accepted assumption, and while assumed values stand
        unconfirmed. Approval writes an APPROVED revision whose assumed fields
        become ASSUMPTION_ACCEPTED under the approver's name.
        """

        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            if row.status == CreativeSessionStatus.CLARIFYING.value:
                raise CreativeSessionConflict(
                    "the brief is still being clarified; answer the director's questions or "
                    "accept its assumptions before approving",
                    reason_code=ReasonCode.BRIEF_NOT_PROPOSED.value,
                )
            if row.status != CreativeSessionStatus.BRIEF_PROPOSED.value:
                raise CreativeSessionConflict(
                    f"brief is not approvable from {row.status}", reason_code="INVALID_TRANSITION"
                )
            brief = self._brief_at(session, session_id, revision)
            if brief is None:
                raise LookupError("brief revision not found")
            if brief.revision != row.current_brief_revision:
                raise CreativeSessionConflict(
                    f"revision {revision} is superseded by {row.current_brief_revision}",
                    reason_code=ReasonCode.REVISION_SUPERSEDED.value,
                )
            fields = dict(brief.fields_json)
            provenance = dict(brief.provenance_json)
            states = dict(brief.question_state_json)
            format_value = str(fields.get("format") or CreativeFormat.UNSPECIFIED.value)
            analysis = self.briefs.analyze(fields, provenance, states, format_value=format_value)
            if analysis.blocking:
                raise CreativeSessionConflict(
                    "critical fields are still unanswered: "
                    + ", ".join(item["code"] for item in analysis.blocking),
                    reason_code=ReasonCode.CRITICAL_UNANSWERED.value,
                    details={"blocking": analysis.blocking},
                )
            if analysis.assumptions and not accept_assumptions:
                raise CreativeSessionConflict(
                    "the brief carries assumed values that need your confirmation: "
                    + ", ".join(item["path"] for item in analysis.assumptions),
                    reason_code=ReasonCode.ASSUMPTIONS_UNCONFIRMED.value,
                    details={"assumptions": analysis.assumptions},
                )
            now = _now()
            for key, record in provenance.items():
                if isinstance(record, dict) and str(record.get("source")) in ASSUMED_SOURCES:
                    record["source"] = ProvenanceSource.ASSUMPTION_ACCEPTED.value
                    record["accepted_by"] = actor
                    record["accepted_at"] = now.isoformat()
                    _ = key
            states2 = reconcile_questions(
                states, fields, provenance, asked_now=[], skipped_now=[], turn_sequence=None
            )
            for item in analysis.assumptions:
                state = states2.setdefault(
                    item["code"], {"status": QuestionStatus.UNASKED.value, "asked_turns": []}
                )
                state["status"] = QuestionStatus.ASSUMPTION_ACCEPTED.value
                state["accepted_value"] = item["value"]
            final_analysis = self.briefs.analyze(fields, provenance, states2, format_value=format_value)
            brief.status = "SUPERSEDED"
            approved = CreativeBriefRevision(
                session_id=session_id,
                revision=row.current_brief_revision + 1,
                status="APPROVED",
                fields_json=fields,
                completeness_json=final_analysis.completeness(),
                content_hash=brief_hash(fields),
                approved_at=now,
                provenance_json=provenance,
                question_state_json=states2,
                source="APPROVAL",
                turn_id=None,
            )
            session.add(approved)
            row.current_brief_revision = approved.revision
            row.status = CreativeSessionStatus.BRIEF_APPROVED.value
            session.flush()
            view = self._brief_view(approved)
            view["session_status"] = row.status
            view["approved_by"] = actor
            return view

    # ------------------------------------------------------------ screenplay
    async def propose_screenplay(
        self, session_id: str, *, notes: str = "", actor: str = ""
    ) -> dict[str, Any]:
        """The director writes (or rewrites) the screenplay from the approved brief.

        The model call is slow and an approval is not, so everything this
        redraft assumes - the stage, the approved brief and its hash, the
        revision it branches from - is captured here and re-checked under the
        row lock before anything is written. A redraft that comes back after
        the old screenplay was approved and its key visuals were derived is
        refused, not applied.
        """

        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            if row.status not in _SCREENPLAY_DRAFT_STATUSES:
                raise CreativeSessionConflict(
                    f"a screenplay cannot be drafted from {row.status}", reason_code="INVALID_TRANSITION"
                )
            brief = self._approved_brief(session, row)
            project_id = row.project_id
            fields = dict(brief.fields_json)
            provenance = dict(brief.provenance_json)
            format_value = row.format
            turns = self._turn_views(session, session_id)
            current = self._screenplay_at(session, session_id, row.current_screenplay_revision)
            previous_content = dict(current.content_json) if current is not None else None
            previous_revision = current.revision if current is not None else None
            brief_id = brief.id
            brief_hash = brief.content_hash
            brief_provenance = dict(brief.provenance_json)
            expected_revision = row.current_screenplay_revision
            prohibitions = self._session_prohibitions(turns)

        screenplay, reasoner, reason_codes, audit, execution_id, skill = await self._reason_screenplay(
            project_id,
            turns=turns,
            fields=fields,
            provenance=provenance,
            format_value=format_value,
            previous=previous_content,
            notes=notes,
        )
        return self._write_screenplay(
            session_id,
            screenplay,
            brief_id=brief_id,
            reasoner=reasoner,
            reason_codes=reason_codes,
            parent_revision=previous_revision,
            user_notes=notes,
            skill=skill,
            execution_id=execution_id,
            audit=audit,
            expected_status=_SCREENPLAY_DRAFT_STATUSES,
            expected_revision=expected_revision,
            expected_brief_hash=brief_hash,
            brief_fields=fields,
            brief_provenance=brief_provenance,
            prohibitions=prohibitions,
        )

    async def _reason_screenplay(
        self,
        project_id: str,
        *,
        turns: list[dict[str, Any]],
        fields: dict[str, Any],
        provenance: dict[str, Any],
        format_value: str,
        previous: dict[str, Any] | None,
        notes: str,
    ) -> tuple[Screenplay, str, list[str], dict[str, Any], str | None, SkillText]:
        skill, skill_codes = self._skill()
        if self.model_roles is None:
            reason = ReasonCode.MODEL_RUNTIME_NOT_CONFIGURED.value
            return (
                deterministic_screenplay(fields, format_value=format_value, reason=reason),
                "DETERMINISTIC",
                [*skill_codes, ReasonCode.DETERMINISTIC_FALLBACK.value, reason],
                {},
                None,
                skill,
            )
        messages, audit = build_screenplay_messages(
            skill=skill,
            turns=turns,
            fields=fields,
            provenance=provenance,
            format_value=format_value,
            previous_screenplay=previous,
            user_notes=notes,
        )
        codes = list(skill_codes)
        if audit.compressed:
            codes.append(ReasonCode.CONTEXT_COMPRESSED.value)
        execution, failure = await self._call_model(project_id, messages, max_tokens=6000)
        if failure is not None:
            return (
                deterministic_screenplay(fields, format_value=format_value, reason=" ".join(failure)),
                "DETERMINISTIC",
                codes + [ReasonCode.DETERMINISTIC_FALLBACK.value, *failure],
                audit.as_json(),
                None,
                skill,
            )
        execution_id = getattr(execution, "execution_record_id", None)
        try:
            raw = _first_choice_json(execution.response)
            screenplay = validate_screenplay(raw)
        except ScreenplayInvalid as exc:
            return (
                deterministic_screenplay(
                    fields,
                    format_value=format_value,
                    reason=f"{ReasonCode.MODEL_OUTPUT_INVALID.value}: {exc.details[:3]}",
                ),
                "DETERMINISTIC",
                codes
                + [
                    ReasonCode.DETERMINISTIC_FALLBACK.value,
                    ReasonCode.MODEL_OUTPUT_INVALID.value,
                    *exc.details[:5],
                ],
                audit.as_json(),
                execution_id,
                skill,
            )
        except (ValueError, TypeError) as exc:
            return (
                deterministic_screenplay(fields, format_value=format_value, reason=type(exc).__name__),
                "DETERMINISTIC",
                codes
                + [
                    ReasonCode.DETERMINISTIC_FALLBACK.value,
                    ReasonCode.MODEL_OUTPUT_INVALID.value,
                    type(exc).__name__,
                ],
                audit.as_json(),
                execution_id,
                skill,
            )
        return (
            screenplay,
            "MODEL:DIRECTOR",
            codes + [ReasonCode.MODEL_REPLY.value],
            audit.as_json(),
            execution_id,
            skill,
        )

    def edit_screenplay(self, session_id: str, content: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """The user's own revision of the current screenplay; same schema, new revision."""

        try:
            screenplay = validate_screenplay(content)
        except ScreenplayInvalid as exc:
            raise ValueError(f"screenplay rejected: {'; '.join(exc.details[:5])}") from exc
        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            if row.status != CreativeSessionStatus.SCREENPLAY_PROPOSED.value:
                raise CreativeSessionConflict(
                    f"the screenplay can only be edited while proposed; session is {row.status}",
                    reason_code="INVALID_TRANSITION",
                )
            current = self._screenplay_at(session, session_id, row.current_screenplay_revision)
            brief = self._approved_brief(session, row)
            brief_id = brief.id
            brief_hash = brief.content_hash
            brief_fields = dict(brief.fields_json)
            brief_provenance = dict(brief.provenance_json)
            prohibitions = self._session_prohibitions(self._turn_views(session, session_id))
            parent = current.revision if current is not None else None
        return self._write_screenplay(
            session_id,
            screenplay,
            brief_id=brief_id,
            reasoner="USER_EDIT",
            reason_codes=["USER_EDIT"],
            parent_revision=parent,
            user_notes=f"edited by {actor}",
            skill=None,
            execution_id=None,
            audit={},
            expected_status=frozenset({CreativeSessionStatus.SCREENPLAY_PROPOSED.value}),
            expected_revision=parent,
            expected_brief_hash=brief_hash,
            brief_fields=brief_fields,
            brief_provenance=brief_provenance,
            prohibitions=prohibitions,
            refuse_blocking=True,
        )

    def _write_screenplay(  # noqa: PLR0913 - one guarded writer for every screenplay revision
        self,
        session_id: str,
        screenplay: Screenplay,
        *,
        brief_id: str,
        reasoner: str,
        reason_codes: list[str],
        parent_revision: int | None,
        user_notes: str,
        skill: SkillText | None,
        execution_id: str | None,
        audit: dict[str, Any],
        status: str = "PROPOSED",
        expected_status: frozenset[str],
        expected_revision: int | None,
        expected_brief_hash: str | None = None,
        brief_fields: dict[str, Any],
        brief_provenance: dict[str, Any],
        prohibitions: list[str] | None = None,
        refuse_blocking: bool = False,
    ) -> dict[str, Any]:
        """Append one screenplay revision, but only onto the state it was written for.

        A redraft takes seconds of model time; an approval takes none. Without
        these checks a slow redraft landing after an approval superseded
        nothing (the loop below only touches PROPOSED rows), moved the head
        past the APPROVED revision and reset the session to
        SCREENPLAY_PROPOSED - from which approving again derives and pays for a
        second full set of key visuals. The preconditions are therefore
        re-validated under the row lock, and a stale write is refused with the
        reason code that says which one moved.
        """

        content = screenplay.model_dump(by_alias=True)
        beats = beats_from_screenplay(screenplay)
        script, _intents = render_script(beats)
        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            if row.status not in expected_status:
                raise CreativeSessionConflict(
                    f"a screenplay cannot be written from {row.status}; it was drafted for "
                    + " or ".join(sorted(expected_status)),
                    reason_code=ReasonCode.SCREENPLAY_STAGE_CHANGED.value,
                    details={"status": row.status, "expected_status": sorted(expected_status)},
                )
            if expected_revision is not None and expected_revision != row.current_screenplay_revision:
                raise CreativeSessionConflict(
                    f"screenplay revision {expected_revision} is superseded by "
                    f"{row.current_screenplay_revision}",
                    reason_code=ReasonCode.SCREENPLAY_REVISION_CHANGED.value,
                    details={
                        "expected_revision": expected_revision,
                        "screenplay_revision": row.current_screenplay_revision,
                    },
                    retryable=True,
                )
            approved_brief = self._approved_brief(session, row)
            if approved_brief.id != brief_id or (
                expected_brief_hash is not None and approved_brief.content_hash != expected_brief_hash
            ):
                raise CreativeSessionConflict(
                    "the approved brief moved while the screenplay was being written",
                    reason_code=ReasonCode.SCREENPLAY_BRIEF_CHANGED.value,
                    details={"brief_id": approved_brief.id, "expected_brief_id": brief_id},
                    retryable=True,
                )
            conformance = self.brief_validator.validate(
                screenplay,
                brief_fields,
                format_value=row.format,
                provenance=brief_provenance,
                prohibitions=prohibitions,
            )
            reason_codes = list(reason_codes)
            if conformance.blocking:
                reason_codes.append(ReasonCode.SCREENPLAY_CONTRADICTS_BRIEF.value)
                if refuse_blocking:
                    # A screenplay the *user* wrote or edited is refused rather
                    # than recorded: the user is the one who can fix it now.
                    raise CreativeSessionConflict(
                        "this screenplay contradicts the approved brief: "
                        + ", ".join(sorted({item.brief_path for item in conformance.blocking})),
                        reason_code=ReasonCode.SCREENPLAY_CONTRADICTS_BRIEF.value,
                        details={"violations": [item.as_json() for item in conformance.blocking]},
                    )
            if conformance.advisory:
                reason_codes.append(ReasonCode.SCREENPLAY_BRIEF_ADVISORY.value)
            for stale in session.scalars(
                select(CreativeScreenplayRevision).where(
                    CreativeScreenplayRevision.session_id == session_id,
                    CreativeScreenplayRevision.status == "PROPOSED",
                )
            ):
                stale.status = "SUPERSEDED"
            revision = row.current_screenplay_revision + 1
            screenplay_row = CreativeScreenplayRevision(
                session_id=session_id,
                revision=revision,
                status=status,
                brief_id=brief_id,
                reasoner=reasoner,
                reason_codes=reason_codes,
                parent_revision=parent_revision,
                user_notes=user_notes[:4000],
                skill_version=skill.version if skill else None,
                skill_content_hash=skill.content_hash if skill else None,
                model_execution_record_id=execution_id,
                content_json={**content, "_context": audit} if audit else content,
                script_text=script,
                content_hash=screenplay_hash(content),
            )
            session.add(screenplay_row)
            row.current_screenplay_revision = revision
            if status == "PROPOSED":
                row.status = CreativeSessionStatus.SCREENPLAY_PROPOSED.value
            session.flush()
            view = self._screenplay_view(screenplay_row)
            view["brief_conformance"] = conformance.as_json()
            return view

    def approve_screenplay(
        self,
        session_id: str,
        *,
        revision: int,
        actor: str,
        accept_deterministic: bool = False,
        accept_brief_violations: bool = False,
    ) -> dict[str, Any]:
        """Approve exactly one screenplay revision; derive and emit the key visuals.

        This is the last gate before real money: approval derives the key
        visuals and emits their paid generation actions. A screenplay that
        contradicts a fact the user established in the approved brief is
        refused here with the conflicting paths, the brief's value and the
        screenplay's, so the user can redraft rather than pay for a story they
        did not approve.
        """

        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            if row.status != CreativeSessionStatus.SCREENPLAY_PROPOSED.value:
                raise CreativeSessionConflict(
                    f"screenplay is not approvable from {row.status}", reason_code="INVALID_TRANSITION"
                )
            screenplay_row = self._screenplay_at(session, session_id, revision)
            if screenplay_row is None:
                raise LookupError("screenplay revision not found")
            if screenplay_row.revision != row.current_screenplay_revision:
                raise CreativeSessionConflict(
                    f"screenplay revision {revision} is superseded by {row.current_screenplay_revision}",
                    reason_code=ReasonCode.REVISION_SUPERSEDED.value,
                )
            if screenplay_row.reasoner == "DETERMINISTIC" and not accept_deterministic:
                raise CreativeSessionConflict(
                    "this screenplay is the deterministic scaffold, not the director's writing; "
                    "redraft with the director, or approve it explicitly",
                    reason_code=ReasonCode.DETERMINISTIC_SCREENPLAY_UNCONFIRMED.value,
                    details={"reason_codes": list(screenplay_row.reason_codes)},
                )
            for other in session.scalars(
                select(CreativeScreenplayRevision).where(
                    CreativeScreenplayRevision.session_id == session_id,
                    CreativeScreenplayRevision.status == "APPROVED",
                )
            ):
                other.status = "SUPERSEDED"
            screenplay_row.status = "APPROVED"
            screenplay_row.approved_at = _now()
            brief = self._approved_brief(session, row)
            screenplay = validate_screenplay(_content_without_audit(screenplay_row.content_json))
            conformance = self.brief_validator.validate(
                screenplay,
                dict(brief.fields_json),
                format_value=row.format,
                provenance=dict(brief.provenance_json),
                prohibitions=self._session_prohibitions(self._turn_views(session, session_id)),
            )
            if conformance.blocking and not accept_brief_violations:
                raise CreativeSessionConflict(
                    "this screenplay contradicts the approved brief: "
                    + ", ".join(sorted({item.brief_path for item in conformance.blocking})),
                    reason_code=ReasonCode.SCREENPLAY_CONTRADICTS_BRIEF.value,
                    details={"violations": [item.as_json() for item in conformance.blocking]},
                )
            row.status = CreativeSessionStatus.SCREENPLAY_APPROVED.value
            anchors, derivation = self._derive_anchors(
                session, row, brief, screenplay_row, screenplay
            )
            actions = self._emit_visual_actions(session, row, anchors, dict(brief.fields_json))
            row.status = CreativeSessionStatus.VISUALS_IN_PROGRESS.value
            session.flush()
            _ = actor
            return {
                "screenplay": self._screenplay_view(screenplay_row),
                "actions": actions,
                "anchors": [_anchor_view(anchor) for anchor in anchors],
                "coverage": derivation.coverage_json(),
                "brief_conformance": conformance.as_json(),
                "session_status": row.status,
            }

    # --------------------------------------------------------------- anchors
    def _derive_anchors(
        self,
        session: Any,
        row: CreativeSession,
        brief: CreativeBriefRevision,
        screenplay_row: CreativeScreenplayRevision,
        screenplay: Screenplay,
    ) -> tuple[list[CreativeVisualAnchor], AnchorDerivation]:
        """Anchors implied by brief and screenplay, versioned by content.

        A key whose depiction changed gets a new version and the old row is
        SUPERSEDED; a key that is no longer implied is SUPERSEDED too. Character
        anchors materialize project Character rows under the script name the
        narrative compiler will parse, so the compile binds the same entity.
        Returns the anchors together with the derivation's coverage report, so
        the caller can record which screenplay elements deliberately get no key
        visual and why.
        """

        derivation: AnchorDerivation = derive_anchors(dict(brief.fields_json), screenplay)
        specs = list(derivation.specs)
        rows = list(
            session.scalars(select(CreativeVisualAnchor).where(CreativeVisualAnchor.session_id == row.id))
        )
        current: dict[str, CreativeVisualAnchor] = {
            anchor.anchor_key: anchor
            for anchor in rows
            if anchor.status != CreativeAnchorStatus.SUPERSEDED.value
        }
        max_version: dict[str, int] = {}
        for anchor in rows:
            max_version[anchor.anchor_key] = max(max_version.get(anchor.anchor_key, 0), anchor.version)
        result: list[CreativeVisualAnchor] = []
        wanted_keys = {spec.anchor_key for spec in specs}
        for spec in specs:
            existing = current.get(spec.anchor_key)
            if existing is not None and existing.prompt_hash == spec.prompt_hash:
                existing.required = spec.required
                existing.brief_id = brief.id
                existing.screenplay_id = screenplay_row.id
                result.append(existing)
                continue
            if existing is not None:
                existing.status = CreativeAnchorStatus.SUPERSEDED.value
            anchor = CreativeVisualAnchor(
                session_id=row.id,
                anchor_key=spec.anchor_key,
                version=max_version.get(spec.anchor_key, 0) + 1,
                kind=spec.kind,
                title=spec.title[:200],
                prompt_json=spec.prompt,
                prompt_hash=spec.prompt_hash,
                required=spec.required,
                brief_id=brief.id,
                screenplay_id=screenplay_row.id,
            )
            if spec.kind == "CHARACTER" and spec.character_name:
                anchor.character_id = self._ensure_character(
                    session, row.project_id, script_name(spec.character_name), spec.character_name
                )
            session.add(anchor)
            session.flush()
            result.append(anchor)
        for key, anchor in current.items():
            if key not in wanted_keys:
                anchor.status = CreativeAnchorStatus.SUPERSEDED.value
        session.flush()
        result.sort(key=lambda anchor: (not anchor.required, anchor.kind, anchor.anchor_key))
        return result, derivation

    def _emit_visual_actions(
        self,
        session: Any,
        row: CreativeSession,
        anchors: list[CreativeVisualAnchor],
        fields: dict[str, Any],
    ) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for anchor in anchors:
            if anchor.status not in {CreativeAnchorStatus.PENDING.value, CreativeAnchorStatus.FAILED.value}:
                continue
            payload = {
                "anchor_key": anchor.anchor_key,
                "anchor_id": anchor.id,
                "anchor_version": anchor.version,
                "required": anchor.required,
                "prompt": _compose_anchor_prompt(anchor.kind, anchor.prompt_json),
                "aspect_ratio": "1:1" if anchor.kind in {"CHARACTER", "PRODUCT", "PROP"} else _aspect(fields),
                "image_count": 1,
            }
            actions.append(
                self._emit_action(
                    session,
                    row,
                    StructuredActionKind.GENERATE_KEY_VISUAL,
                    payload,
                    idempotency_key=f"creative:{row.id}:visual:{anchor.anchor_key}:v{anchor.version}",
                )
            )
        return actions

    @staticmethod
    def _ensure_character(session: Any, project_id: str, name: str, display_name: str) -> str:
        found = session.scalar(
            select(Character).where(
                Character.project_id == project_id, func.lower(Character.name) == name.lower()
            )
        )
        if found is not None:
            return found.id
        character = Character(
            project_id=project_id,
            name=name,
            description=display_name if display_name != name else "",
            status="DRAFT",
        )
        session.add(character)
        session.flush()
        return character.id

    # ------------------------------------------------------------- actions
    def _emit_action(
        self,
        session: Any,
        row: CreativeSession,
        kind: StructuredActionKind,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None,
        status: str = CreativeActionStatus.PROPOSED.value,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if idempotency_key is not None:
            existing = session.scalar(
                select(CreativeAction).where(CreativeAction.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return _action_view(existing)
        sequence = (
            session.scalar(
                select(func.coalesce(func.max(CreativeAction.sequence), 0)).where(
                    CreativeAction.session_id == row.id
                )
            )
            + 1
        )
        action = CreativeAction(
            session_id=row.id,
            sequence=sequence,
            kind=kind.value,
            payload_json=payload,
            idempotency_key=idempotency_key,
            status=status,
            result_json=result or {},
            executed_at=_now() if status != CreativeActionStatus.PROPOSED.value else None,
        )
        session.add(action)
        session.flush()
        return _action_view(action)

    def pending_actions(
        self,
        session_id: str,
        *,
        kind: str | None = None,
        include_failed: bool = False,
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            statuses = [CreativeActionStatus.PROPOSED.value]
            if include_failed:
                statuses.append(CreativeActionStatus.FAILED.value)
            query = (
                select(CreativeAction)
                .where(
                    CreativeAction.session_id == session_id,
                    CreativeAction.status.in_(statuses),
                )
                .order_by(CreativeAction.sequence)
            )
            if kind is not None:
                query = query.where(CreativeAction.kind == kind)
            actions = [_action_view(action) for action in session.scalars(query)]
            if kind == StructuredActionKind.GENERATE_KEY_VISUAL.value:
                # Only anchors that are still current and not skipped keep their actions live.
                live = {
                    anchor.id
                    for anchor in session.scalars(
                        select(CreativeVisualAnchor).where(CreativeVisualAnchor.session_id == session_id)
                    )
                    if anchor.status
                    not in {CreativeAnchorStatus.SUPERSEDED.value, CreativeAnchorStatus.SKIPPED.value}
                }
                actions = [action for action in actions if action["payload"].get("anchor_id") in live]
            return actions

    def record_action_result(
        self,
        action_id: str,
        *,
        status: str,
        result: dict[str, Any],
    ) -> None:
        """The executor's callback: what actually happened to one action."""

        with self.database.session() as session:
            action = session.get(CreativeAction, action_id)
            if action is None:
                raise LookupError("creative action not found")
            action.status = CreativeActionStatus(status).value
            action.result_json = result
            action.executed_at = _now()
            if action.kind == StructuredActionKind.GENERATE_KEY_VISUAL.value:
                anchor = None
                if action.payload_json.get("anchor_id"):
                    anchor = session.get(CreativeVisualAnchor, action.payload_json["anchor_id"])
                if anchor is None:
                    anchor = session.scalar(
                        select(CreativeVisualAnchor).where(
                            CreativeVisualAnchor.session_id == action.session_id,
                            CreativeVisualAnchor.anchor_key == action.payload_json.get("anchor_key"),
                            CreativeVisualAnchor.status != CreativeAnchorStatus.SUPERSEDED.value,
                        )
                    )
                if anchor is not None and anchor.status != CreativeAnchorStatus.SUPERSEDED.value:
                    if status == CreativeActionStatus.EXECUTED.value:
                        anchor.status = CreativeAnchorStatus.GENERATING.value
                        anchor.generation_job_id = result.get("job_id")
                        anchor.failure_code = None
                    else:
                        anchor.status = CreativeAnchorStatus.FAILED.value
                        anchor.failure_code = str(result.get("error") or "EXECUTION_FAILED")[:240]
            session.flush()

    def sync_visuals(self, session_id: str) -> dict[str, Any]:
        """Bind finished generation jobs to their anchors. Idempotent."""

        with self.database.session() as session:
            row = self._session(session, session_id)
            anchors = self._current_anchors(session, session_id)
            for anchor in anchors:
                if anchor.status != CreativeAnchorStatus.GENERATING.value:
                    continue
                job = (
                    session.get(GenerationJob, anchor.generation_job_id) if anchor.generation_job_id else None
                )
                if job is None:
                    continue
                if job.status == JobStatus.COMPLETED.value and job.output_asset_id:
                    anchor.status = CreativeAnchorStatus.READY.value
                    anchor.media_asset_id = job.output_asset_id
                elif job.status in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
                    anchor.status = CreativeAnchorStatus.FAILED.value
                    anchor.failure_code = (job.error_code or job.status)[:240]
            session.flush()
            return {"session_status": row.status, **_anchor_summary(anchors)}

    def skip_anchor(self, session_id: str, anchor_id: str, *, reason: str, actor: str) -> dict[str, Any]:
        """The user goes without one optional key visual; recorded, never inferred."""

        with self.database.session() as session:
            row = self._session(session, session_id)
            anchor = session.get(CreativeVisualAnchor, anchor_id)
            if anchor is None or anchor.session_id != session_id:
                raise LookupError("anchor not found")
            if anchor.status == CreativeAnchorStatus.SUPERSEDED.value:
                raise CreativeSessionConflict(
                    "this anchor version is superseded", reason_code=ReasonCode.ANCHOR_SUPERSEDED.value
                )
            if anchor.required:
                raise CreativeSessionConflict(
                    f"{anchor.title} is a required key visual and cannot be skipped; retry it instead",
                    reason_code="REQUIRED_ANCHOR",
                )
            if anchor.status not in {CreativeAnchorStatus.FAILED.value, CreativeAnchorStatus.PENDING.value}:
                raise CreativeSessionConflict(
                    f"only a failed or pending anchor can be skipped; {anchor.title} is {anchor.status}",
                    reason_code="INVALID_TRANSITION",
                )
            anchor.status = CreativeAnchorStatus.SKIPPED.value
            anchor.skip_reason = (
                f"{actor}: {reason.strip()}"[:240] if reason.strip() else f"skipped by {actor}"[:240]
            )
            for action in session.scalars(
                select(CreativeAction).where(
                    CreativeAction.session_id == session_id,
                    CreativeAction.kind == StructuredActionKind.GENERATE_KEY_VISUAL.value,
                    CreativeAction.status.in_(
                        [CreativeActionStatus.PROPOSED.value, CreativeActionStatus.FAILED.value]
                    ),
                )
            ):
                if action.payload_json.get("anchor_id") == anchor.id:
                    action.status = CreativeActionStatus.SKIPPED.value
                    action.result_json = {**action.result_json, "skipped": anchor.skip_reason}
                    action.executed_at = _now()
            session.flush()
            _ = row
            return _anchor_view(anchor)

    def regenerate_anchor(
        self, session_id: str, anchor_id: str, *, direction: str, actor: str
    ) -> dict[str, Any]:
        """The user wants a different image: a new anchor version with their direction.

        Allowed until the bible is locked. The old version is SUPERSEDED (its
        image is never re-used under the new version); a DRAFT bible built on
        the old set is superseded too and the session returns to key visuals.
        """

        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            anchor = self._anchor_for_change(session, row, anchor_id)
            brief = self._approved_brief(session, row)
            note = " ".join(direction.split())[:600]
            prompt = {**dict(anchor.prompt_json), "user_direction": note, "regenerated_by": actor}
            replacement = self._supersede_anchor(session, row, anchor, prompt, status=None)
            actions = self._emit_visual_actions(session, row, [replacement], dict(brief.fields_json))
            self._unpropose_bible(session, row)
            session.flush()
            return {"anchor": _anchor_view(replacement), "actions": actions, "session_status": row.status}

    def replace_anchor_image(
        self, session_id: str, anchor_id: str, *, media_asset_id: str, actor: str
    ) -> dict[str, Any]:
        """The user supplies the image: a READY anchor version bound to their asset.

        The asset must belong to the project and be a verified image. No
        generation is spent; the version records who supplied it.
        """

        from production_domain.models import MediaAsset

        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            anchor = self._anchor_for_change(session, row, anchor_id)
            media = session.get(MediaAsset, media_asset_id)
            if media is None or media.project_id != row.project_id:
                raise LookupError("media asset not found in this project")
            if not str(media.mime_type or "").startswith("image/"):
                raise CreativeSessionConflict(
                    "a key visual must be an image", reason_code="ANCHOR_MEDIA_NOT_IMAGE"
                )
            if media.verification_status != "READY":
                raise CreativeSessionConflict(
                    f"the uploaded image is {media.verification_status}; wait for verification",
                    reason_code="ANCHOR_MEDIA_NOT_READY",
                    retryable=True,
                )
            prompt = {**dict(anchor.prompt_json), "user_supplied_media_id": media.id, "supplied_by": actor}
            replacement = self._supersede_anchor(
                session, row, anchor, prompt, status=CreativeAnchorStatus.READY.value
            )
            replacement.media_asset_id = media.id
            self._unpropose_bible(session, row)
            session.flush()
            return {"anchor": _anchor_view(replacement), "actions": [], "session_status": row.status}

    def _anchor_for_change(self, session: Any, row: CreativeSession, anchor_id: str) -> CreativeVisualAnchor:
        if row.status not in {
            CreativeSessionStatus.VISUALS_IN_PROGRESS.value,
            CreativeSessionStatus.BIBLE_PROPOSED.value,
        }:
            raise CreativeSessionConflict(
                f"key visuals can only change before the bible is locked; session is {row.status}",
                reason_code="INVALID_TRANSITION",
            )
        anchor = session.get(CreativeVisualAnchor, anchor_id)
        if anchor is None or anchor.session_id != row.id:
            raise LookupError("anchor not found")
        if anchor.status == CreativeAnchorStatus.SUPERSEDED.value:
            raise CreativeSessionConflict(
                "this anchor version is superseded", reason_code=ReasonCode.ANCHOR_SUPERSEDED.value
            )
        if anchor.status == CreativeAnchorStatus.GENERATING.value:
            raise CreativeSessionConflict(
                f"{anchor.title} is still generating; wait for it or refresh",
                reason_code="ANCHOR_GENERATING",
                retryable=True,
            )
        return anchor

    def _supersede_anchor(
        self,
        session: Any,
        row: CreativeSession,
        anchor: CreativeVisualAnchor,
        prompt: dict[str, Any],
        *,
        status: str | None,
    ) -> CreativeVisualAnchor:
        anchor.status = CreativeAnchorStatus.SUPERSEDED.value
        for action in session.scalars(
            select(CreativeAction).where(
                CreativeAction.session_id == row.id,
                CreativeAction.kind == StructuredActionKind.GENERATE_KEY_VISUAL.value,
                CreativeAction.status.in_(
                    [CreativeActionStatus.PROPOSED.value, CreativeActionStatus.FAILED.value]
                ),
            )
        ):
            if action.payload_json.get("anchor_id") == anchor.id:
                action.status = CreativeActionStatus.SKIPPED.value
                action.result_json = {**action.result_json, "superseded_by_user": True}
                action.executed_at = _now()
        max_version = session.scalar(
            select(func.coalesce(func.max(CreativeVisualAnchor.version), 0)).where(
                CreativeVisualAnchor.session_id == row.id,
                CreativeVisualAnchor.anchor_key == anchor.anchor_key,
            )
        )
        replacement = CreativeVisualAnchor(
            session_id=row.id,
            anchor_key=anchor.anchor_key,
            version=int(max_version or 0) + 1,
            kind=anchor.kind,
            title=anchor.title,
            prompt_json=prompt,
            prompt_hash=screenplay_hash({"version": "creative-anchor-v2", **prompt}),
            required=anchor.required,
            character_id=anchor.character_id,
            brief_id=anchor.brief_id,
            screenplay_id=anchor.screenplay_id,
            status=status or CreativeAnchorStatus.PENDING.value,
        )
        session.add(replacement)
        session.flush()
        return replacement

    @staticmethod
    def _unpropose_bible(session: Any, row: CreativeSession) -> None:
        """A changed key visual invalidates a DRAFT bible; a LOCKED one cannot be reached here."""

        if row.status != CreativeSessionStatus.BIBLE_PROPOSED.value:
            return
        draft = session.scalar(
            select(VisualBibleVersion).where(
                VisualBibleVersion.session_id == row.id, VisualBibleVersion.status == "DRAFT"
            )
        )
        if draft is not None:
            draft.status = "SUPERSEDED"
        row.status = CreativeSessionStatus.VISUALS_IN_PROGRESS.value

    @staticmethod
    def _current_anchors(session: Any, session_id: str) -> list[CreativeVisualAnchor]:
        anchors = [
            anchor
            for anchor in session.scalars(
                select(CreativeVisualAnchor)
                .where(CreativeVisualAnchor.session_id == session_id)
                .order_by(CreativeVisualAnchor.created_at)
            )
            if anchor.status != CreativeAnchorStatus.SUPERSEDED.value
        ]
        anchors.sort(key=lambda anchor: (not anchor.required, anchor.kind, anchor.anchor_key))
        return anchors

    # ---------------------------------------------------------- visual bible
    def propose_bible(self, session_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            if row.status not in {
                CreativeSessionStatus.VISUALS_IN_PROGRESS.value,
                CreativeSessionStatus.BIBLE_PROPOSED.value,
            }:
                raise CreativeSessionConflict(
                    f"bible cannot be proposed from {row.status}", reason_code="INVALID_TRANSITION"
                )
            brief = self._approved_brief(session, row)
            screenplay_row = self._approved_screenplay(session, row)
            anchors = self._current_anchors(session, session_id)
            summary = _anchor_summary(anchors)
            if summary["required_not_ready"]:
                raise CreativeSessionConflict(
                    "required key visuals are not ready: "
                    + ", ".join(
                        f"{item['title']} ({item['status']})" for item in summary["required_not_ready"]
                    ),
                    reason_code=ReasonCode.REQUIRED_ANCHORS_NOT_READY.value,
                    details={"anchors": summary["required_not_ready"]},
                )
            if summary["optional_not_terminal"]:
                raise CreativeSessionConflict(
                    "optional key visuals are still pending or failed; retry or skip them: "
                    + ", ".join(
                        f"{item['title']} ({item['status']})" for item in summary["optional_not_terminal"]
                    ),
                    reason_code=ReasonCode.OPTIONAL_ANCHORS_NOT_TERMINAL.value,
                    details={"anchors": summary["optional_not_terminal"]},
                )
            fields = dict(brief.fields_json)
            screenplay_content = _content_without_audit(screenplay_row.content_json)
            content = {
                "logline": fields.get("logline"),
                "format": row.format,
                "style": fields.get("visual_style") or {},
                "tone": fields.get("tone") or [],
                "aspect_ratio": _aspect(fields),
                "visual_direction": (screenplay_content.get("treatment") or {}).get("visual_direction"),
                "brief_revision": brief.revision,
                "brief_hash": brief.content_hash,
                "screenplay_revision": screenplay_row.revision,
                "screenplay_hash": screenplay_row.content_hash,
                "anchors": [
                    {
                        "anchor_id": anchor.id,
                        "anchor_key": anchor.anchor_key,
                        "version": anchor.version,
                        "kind": anchor.kind,
                        "title": anchor.title,
                        "required": anchor.required,
                        "prompt_hash": anchor.prompt_hash,
                        "media_asset_id": anchor.media_asset_id,
                        "character_id": anchor.character_id,
                        "status": anchor.status,
                        "skip_reason": anchor.skip_reason,
                    }
                    for anchor in anchors
                ],
                # Which screenplay elements deliberately carry no key visual,
                # and why. A background-only character is a decision on record,
                # never an element that quietly fell off a slice.
                "coverage": derive_anchors(fields, validate_screenplay(screenplay_content)).coverage_json(),
                "rules": {
                    "palette": get_path(fields, "visual_style.palette") or "",
                    "medium": get_path(fields, "visual_style.medium") or "",
                    "never": ["change character identity", "switch rendering medium mid-story"],
                },
            }
            draft = session.scalar(
                select(VisualBibleVersion).where(
                    VisualBibleVersion.session_id == session_id,
                    VisualBibleVersion.status == "DRAFT",
                )
            )
            if draft is not None:
                draft.status = "SUPERSEDED"
            version = row.current_bible_version + 1
            bible = VisualBibleVersion(
                session_id=session_id,
                project_id=row.project_id,
                version=version,
                brief_id=brief.id,
                screenplay_id=screenplay_row.id,
                content_json=content,
                content_hash=brief_hash(content),
                lineage_json={"lock_status": "NOT_LOCKED"},
            )
            session.add(bible)
            row.current_bible_version = version
            row.status = CreativeSessionStatus.BIBLE_PROPOSED.value
            session.flush()
            return _bible_view(bible)

    def approve_bible(
        self,
        session_id: str,
        *,
        version: int,
        actor: str,
        actor_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Lock one bible version and bind it through the platform's own locks.

        Character anchors become ``CharacterIdentityVersion`` rows through
        ``CharacterIdentityService.confirm_identity``; the style anchor becomes
        a canonical STYLE asset version locked through
        ``ProjectStyleService.lock``. A failure leaves the bible DRAFT with the
        failure recorded in its lineage and blocks compilation; nothing here
        writes those tables directly.
        """

        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            bible = session.scalar(
                select(VisualBibleVersion).where(
                    VisualBibleVersion.session_id == session_id,
                    VisualBibleVersion.version == version,
                )
            )
            if bible is None:
                raise LookupError("visual bible version not found")
            if bible.status == "LOCKED":
                return _bible_view(bible)
            if bible.status != "DRAFT" or version != row.current_bible_version:
                raise CreativeSessionConflict(
                    f"bible version {version} is superseded; lock the current draft",
                    reason_code=ReasonCode.REVISION_SUPERSEDED.value,
                )
            anchors = self._current_anchors(session, session_id)
            summary = _anchor_summary(anchors)
            if summary["required_not_ready"]:
                raise CreativeSessionConflict(
                    "required key visuals are not ready",
                    reason_code=ReasonCode.REQUIRED_ANCHORS_NOT_READY.value,
                    details={"anchors": summary["required_not_ready"]},
                )
            project_id = row.project_id
            bible_id = bible.id
            lineage = dict(bible.lineage_json or {})
            title = row.title
            style_anchor = next(
                (a for a in anchors if a.kind == "STYLE" and a.status == CreativeAnchorStatus.READY.value),
                None,
            )
            character_anchors = [
                a for a in anchors if a.kind == "CHARACTER" and a.status == CreativeAnchorStatus.READY.value
            ]
            # Scene, product and prop key visuals are canon too: without them
            # the frame-anchor planner has no location plate to reconstruct
            # from and the product a commerce film is about never reaches a
            # reference set. Only READY anchors qualify - a SKIPPED optional
            # anchor is a decision to go without, never a canonical asset.
            supporting_anchors = [
                {
                    "id": a.id,
                    "anchor_key": a.anchor_key,
                    "version": a.version,
                    "kind": a.kind,
                    "title": a.title,
                    "media_asset_id": a.media_asset_id,
                    "subject": str((a.prompt_json or {}).get("subject") or a.title),
                }
                for a in anchors
                if a.kind in _CANONICAL_ANCHOR_KINDS
                and a.status == CreativeAnchorStatus.READY.value
                and a.media_asset_id
            ]
            anchor_snapshot = [
                {
                    "id": a.id,
                    "anchor_key": a.anchor_key,
                    "version": a.version,
                    "media_asset_id": a.media_asset_id,
                    "character_id": a.character_id,
                    "title": a.title,
                    "look": str((a.prompt_json or {}).get("look") or ""),
                }
                for a in character_anchors
            ]
            style_snapshot = (
                {
                    "id": style_anchor.id,
                    "media_asset_id": style_anchor.media_asset_id,
                    "version": style_anchor.version,
                }
                if style_anchor is not None
                else None
            )
            approved_brief = self._approved_brief(session, row)
            fields = dict(approved_brief.fields_json)
            brief_id = approved_brief.id
            screenplay_id = self._approved_screenplay(session, row).id

        if self.styles is None or self.characters is None or self.asset_registry is None:
            raise CreativeSessionConflict(
                "identity and style lock services are not configured; the bible cannot be locked",
                reason_code=ReasonCode.LOCK_SERVICES_UNAVAILABLE.value,
            )
        if not actor_user_id:
            raise CreativeSessionConflict(
                "locking the project style requires a signed-in user",
                reason_code=ReasonCode.STYLE_LOCK_REQUIRES_USER.value,
            )
        if style_snapshot is None or not style_snapshot["media_asset_id"]:
            raise CreativeSessionConflict(
                "the style key plate is not ready", reason_code=ReasonCode.REQUIRED_ANCHORS_NOT_READY.value
            )

        lineage.setdefault("identities", {})
        codes: list[str] = []
        try:
            self._lock_style(
                session_id,
                project_id,
                bible_id,
                version,
                title,
                fields,
                style_snapshot,
                lineage,
                codes,
                actor_user_id,
            )
            self._lock_identities(
                session_id, project_id, bible_id, version, anchor_snapshot, lineage, codes, actor_user_id
            )
            self._lock_supporting_assets(
                session_id,
                project_id,
                bible_id,
                version,
                brief_id,
                screenplay_id,
                supporting_anchors,
                lineage,
                actor_user_id,
            )
        except Exception as exc:  # noqa: BLE001 - every lock failure is recorded, then refused
            lineage["lock_status"] = "FAILED"
            lineage["error"] = str(exc)[:500]
            lineage["error_type"] = type(exc).__name__
            lineage["failed_at"] = _now().isoformat()
            with self.database.session() as session:
                bible = session.get(VisualBibleVersion, bible_id)
                if bible is not None:
                    bible.lineage_json = lineage
                    session.flush()
            raise CreativeSessionConflict(
                f"visual bible lock failed: {exc}",
                reason_code=ReasonCode.LOCK_FAILED.value,
                details={"error_type": type(exc).__name__, "lineage": lineage},
                retryable=True,
            ) from exc

        with self.database.session() as session:
            row = self._session(session, session_id)
            bible = session.get(VisualBibleVersion, bible_id)
            if bible is None:
                raise LookupError("visual bible version not found")
            lineage["lock_status"] = "LOCKED"
            lineage["reason_codes"] = codes
            bible.lineage_json = lineage
            bible.status = "LOCKED"
            bible.locked_at = _now()
            bible.locked_by = actor
            row.status = CreativeSessionStatus.BIBLE_LOCKED.value
            session.flush()
            return _bible_view(bible)

    def _lock_style(  # noqa: PLR0913
        self,
        session_id: str,
        project_id: str,
        bible_id: str,
        version: int,
        title: str,
        fields: dict[str, Any],
        style_snapshot: dict[str, Any],
        lineage: dict[str, Any],
        codes: list[str],
        actor_user_id: str,
    ) -> None:
        assert self.styles is not None and self.asset_registry is not None
        if lineage.get("style_lock_id"):
            return
        with self.database.session() as session:
            existing = session.scalar(
                select(ProjectStyleLock).where(ProjectStyleLock.project_id == project_id)
            )
            existing_id = existing.id if existing is not None else None
            existing_version = existing.style_version_id if existing is not None else None
        if existing_id is not None:
            # One style per project is the platform rule; a later session
            # inherits the lock and says so.
            lineage["style_lock_id"] = existing_id
            lineage["style_version_id"] = existing_version
            lineage["style_inherited"] = True
            codes.append(ReasonCode.STYLE_LOCK_INHERITED.value)
            self._record_lock_action(
                session_id,
                StructuredActionKind.LOCK_PROJECT_STYLE,
                {"bible_id": bible_id, "style_lock_id": existing_id, "inherited": True},
                idempotency_key=f"creative:{session_id}:lock:style:b{version}",
                status=CreativeActionStatus.EXECUTED.value,
            )
            return
        logical = next(
            (
                item
                for item in self.asset_registry.list(project_id, asset_type="STYLE")
                if (item.canonical_metadata or {}).get("creative_session_id") == session_id
            ),
            None,
        )
        if logical is None:
            logical = self.asset_registry.create(
                project_id,
                "STYLE",
                f"{title[:180]} — style",
                canonical_metadata={
                    "creative_session_id": session_id,
                    "constraints": [
                        item
                        for item in [
                            str(get_path(fields, "visual_style.medium") or ""),
                            str(get_path(fields, "visual_style.palette") or ""),
                        ]
                        if item
                    ],
                },
                created_by_user_id=actor_user_id,
            )
        style_version = self.asset_registry.add_version(
            logical.id,
            primary_media_asset_id=style_snapshot["media_asset_id"],
            label=f"Visual bible v{version}",
            source="CREATIVE_KEY_VISUAL",
            metadata={
                "creative_session_id": session_id,
                "bible_id": bible_id,
                "anchor_id": style_snapshot["id"],
                "anchor_version": style_snapshot["version"],
            },
            created_by_user_id=actor_user_id,
        )
        self.asset_registry.promote(
            logical.id,
            style_version.id,
            promoted_by_user_id=actor_user_id,
            reason=f"BestShiny Director visual bible v{version} approval",
        )
        lock = self.styles.lock(
            project_id,
            style_version.id,
            locked_by_user_id=actor_user_id,
            reason=f"BestShiny Director visual bible v{version} (session {session_id})",
            explicit_confirmation=True,
        )
        lineage["style_lock_id"] = lock.id
        lineage["style_asset_id"] = logical.id
        lineage["style_version_id"] = style_version.id
        lineage["style_inherited"] = False
        self._record_lock_action(
            session_id,
            StructuredActionKind.LOCK_PROJECT_STYLE,
            {
                "bible_id": bible_id,
                "style_lock_id": lock.id,
                "style_asset_id": logical.id,
                "style_version_id": style_version.id,
                "anchor_id": style_snapshot["id"],
            },
            idempotency_key=f"creative:{session_id}:lock:style:b{version}",
            status=CreativeActionStatus.EXECUTED.value,
        )

    def _lock_identities(  # noqa: PLR0913
        self,
        session_id: str,
        project_id: str,
        bible_id: str,
        version: int,
        anchor_snapshot: list[dict[str, Any]],
        lineage: dict[str, Any],
        codes: list[str],
        actor_user_id: str,
    ) -> None:
        assert self.characters is not None and self.asset_registry is not None
        identities: dict[str, Any] = lineage.setdefault("identities", {})
        for anchor in anchor_snapshot:
            recorded = identities.get(anchor["anchor_key"])
            if (
                recorded
                and recorded.get("media_asset_id") == anchor["media_asset_id"]
                and recorded.get("identity_version_id")
            ):
                continue
            if not anchor["character_id"] or not anchor["media_asset_id"]:
                raise LookupError(f"character anchor {anchor['title']} has no character or media")
            identity = self.characters.confirm_identity(
                anchor["character_id"],
                anchor["media_asset_id"],
                costume_signature=anchor["look"],
            )
            logical = next(
                (
                    item
                    for item in self.asset_registry.list(project_id, asset_type="CHARACTER")
                    if (item.canonical_metadata or {}).get("character_id") == anchor["character_id"]
                ),
                None,
            )
            if logical is None:
                logical = self.asset_registry.create(
                    project_id,
                    "CHARACTER",
                    anchor["title"],
                    canonical_metadata={"character_id": anchor["character_id"]},
                    created_by_user_id=actor_user_id,
                )
            asset_version = self.asset_registry.add_version(
                logical.id,
                primary_media_asset_id=anchor["media_asset_id"],
                label=f"Identity v{identity.version}",
                source="CHARACTER_IDENTITY_CONFIRMATION",
                metadata={
                    "character_identity_version_id": identity.id,
                    "creative_session_id": session_id,
                    "bible_id": bible_id,
                    "anchor_id": anchor["id"],
                    "costume_signature": anchor["look"],
                },
                created_by_user_id=actor_user_id,
            )
            self.asset_registry.promote(
                logical.id,
                asset_version.id,
                promoted_by_user_id=actor_user_id,
                reason="BestShiny Director visual bible approval",
            )
            identities[anchor["anchor_key"]] = {
                "identity_version_id": identity.id,
                "identity_version": identity.version,
                "character_id": anchor["character_id"],
                "media_asset_id": anchor["media_asset_id"],
                "anchor_id": anchor["id"],
                "anchor_version": anchor["version"],
                "logical_asset_version_id": asset_version.id,
            }
            self._record_lock_action(
                session_id,
                StructuredActionKind.LOCK_CHARACTER_IDENTITY,
                {
                    "bible_id": bible_id,
                    "anchor_id": anchor["id"],
                    "character_id": anchor["character_id"],
                    "identity_version_id": identity.id,
                },
                idempotency_key=f"creative:{session_id}:lock:identity:{anchor['anchor_key']}:v{anchor['version']}:b{version}",
                status=CreativeActionStatus.EXECUTED.value,
            )
            # Persist progress after each identity so a later failure retries
            # only what is missing instead of minting duplicate versions.
            with self.database.session() as session:
                bible = session.get(VisualBibleVersion, bible_id)
                if bible is not None:
                    bible.lineage_json = {**lineage, "lock_status": "PARTIAL"}
                    session.flush()
        _ = codes

    def _lock_supporting_assets(  # noqa: PLR0913 - the lineage this records needs every id
        self,
        session_id: str,
        project_id: str,
        bible_id: str,
        version: int,
        brief_id: str,
        screenplay_id: str,
        anchors: list[dict[str, Any]],
        lineage: dict[str, Any],
        actor_user_id: str,
    ) -> None:
        """Promote the READY scene, product and prop key visuals into Canon.

        Through the AssetRegistry's own create / add_version / promote, never
        by writing ``assets`` directly. One logical asset per anchor key, so a
        later bible whose depiction changed appends a *new* version and
        promotes it rather than overwriting the old one - the old image stays
        exactly what it was, and every version records the anchor, the anchor
        version, the brief, the screenplay, the bible and the media it came
        from.
        """

        assert self.asset_registry is not None
        recorded: dict[str, Any] = lineage.setdefault("assets", {})
        existing_by_key: dict[tuple[str, str], Any] = {}
        for kind in _CANONICAL_ANCHOR_KINDS:
            for item in self.asset_registry.list(project_id, asset_type=kind):
                anchor_key = (item.canonical_metadata or {}).get("creative_anchor_key")
                if anchor_key:
                    existing_by_key[(kind, str(anchor_key))] = item
        for anchor in anchors:
            key = anchor["anchor_key"]
            already = recorded.get(key)
            if already and already.get("media_asset_id") == anchor["media_asset_id"]:
                continue
            logical = existing_by_key.get((anchor["kind"], key))
            if logical is None:
                logical = self.asset_registry.create(
                    project_id,
                    anchor["kind"],
                    anchor["title"][:180] or anchor["subject"][:180] or key,
                    canonical_metadata={
                        "creative_anchor_key": key,
                        "creative_session_id": session_id,
                        "subject": anchor["subject"],
                    },
                    created_by_user_id=actor_user_id,
                )
            asset_version = self.asset_registry.add_version(
                logical.id,
                primary_media_asset_id=anchor["media_asset_id"],
                label=f"Visual bible v{version}",
                source="CREATIVE_KEY_VISUAL",
                metadata={
                    "creative_session_id": session_id,
                    "anchor_id": anchor["id"],
                    "anchor_key": key,
                    "anchor_version": anchor["version"],
                    "brief_id": brief_id,
                    "screenplay_id": screenplay_id,
                    "bible_id": bible_id,
                    "media_asset_id": anchor["media_asset_id"],
                },
                created_by_user_id=actor_user_id,
            )
            self.asset_registry.promote(
                logical.id,
                asset_version.id,
                promoted_by_user_id=actor_user_id,
                reason=f"BestShiny Director visual bible v{version} (session {session_id})",
            )
            recorded[key] = {
                "kind": anchor["kind"],
                "asset_id": logical.id,
                "asset_version_id": asset_version.id,
                "anchor_id": anchor["id"],
                "anchor_version": anchor["version"],
                "media_asset_id": anchor["media_asset_id"],
                "subject": anchor["subject"],
                "brief_id": brief_id,
                "screenplay_id": screenplay_id,
                "bible_id": bible_id,
            }
            with self.database.session() as session:
                bible = session.get(VisualBibleVersion, bible_id)
                if bible is not None:
                    bible.lineage_json = {**lineage, "lock_status": "PARTIAL"}
                    session.flush()

    def _record_lock_action(
        self,
        session_id: str,
        kind: StructuredActionKind,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        status: str,
    ) -> None:
        with self.database.session() as session:
            row = self._session(session, session_id)
            self._emit_action(
                session, row, kind, payload, idempotency_key=idempotency_key, status=status, result=payload
            )

    # ---------------------------------------------------------------- beats
    def propose_beats(self, session_id: str) -> list[dict[str, Any]]:
        with self.database.session() as session:
            row = self._session(session, session_id)
            if row.status not in {
                CreativeSessionStatus.BIBLE_LOCKED.value,
                CreativeSessionStatus.BEATS_PROPOSED.value,
            }:
                raise CreativeSessionConflict(
                    "beats require a locked visual bible; current status is " + row.status,
                    reason_code="INVALID_TRANSITION",
                )
            brief = self._approved_brief(session, row)
            screenplay_row = self._approved_screenplay(session, row)
            screenplay = validate_screenplay(_content_without_audit(screenplay_row.content_json))
            planned = self._materialize_beats(screenplay, dict(brief.fields_json))
            for stale_row in session.scalars(
                select(CreativeBeat).where(
                    CreativeBeat.session_id == session_id, CreativeBeat.status == "PROPOSED"
                )
            ):
                stale_row.status = "SUPERSEDED"
            plan_revision = row.current_beat_revision + 1
            for beat in planned:
                session.add(
                    CreativeBeat(
                        session_id=session_id,
                        plan_revision=plan_revision,
                        sequence=int(beat["sequence"]),
                        beat_json=beat,
                        screenplay_id=screenplay_row.id,
                    )
                )
            row.current_beat_revision = plan_revision
            row.status = CreativeSessionStatus.BEATS_PROPOSED.value
            session.flush()
            return self._beats_view(session, session_id, plan_revision)

    @staticmethod
    def _materialize_beats(screenplay: Screenplay, fields: dict[str, Any]) -> list[dict[str, Any]]:
        beats = beats_from_screenplay(screenplay)
        product = get_path(fields, "product.name")
        for beat in beats:
            for shot in beat["shots"]:
                shot["anchors"] = anchor_keys_for_shot(shot, beat, str(product) if product else None)
        return beats

    def approve_beats(
        self,
        session_id: str,
        *,
        plan_revision: int,
        actor: str,
        episode_title: str | None = None,
        edited_beats: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compile the approved screenplay into the existing production chain.

        The user's beat/shot edits become a new APPROVED screenplay revision
        first, so the compiled episode always corresponds to one exact
        revision. The compile refuses a bible whose locks did not complete,
        creates the episode idempotently, runs the same narrative compiler and
        frame anchor planner every scripted episode uses, applies the shot
        intents, writes per-shot lineage and opens the screenplay's
        obligations in the series ledger.
        """

        if self.orchestrator is None:
            raise CreativeSessionConflict("no episode compiler is configured", reason_code="NO_COMPILER")
        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            if row.status == CreativeSessionStatus.COMPILED.value and row.compiled_episode_id:
                return self._compiled_view(session, row)
            if row.status != CreativeSessionStatus.BEATS_PROPOSED.value:
                raise CreativeSessionConflict(
                    f"beats are not approvable from {row.status}", reason_code="INVALID_TRANSITION"
                )
            if plan_revision != row.current_beat_revision:
                raise CreativeSessionConflict(
                    f"beat plan {plan_revision} is superseded by {row.current_beat_revision}",
                    reason_code=ReasonCode.REVISION_SUPERSEDED.value,
                )
            bible = session.scalar(
                select(VisualBibleVersion).where(
                    VisualBibleVersion.session_id == session_id,
                    VisualBibleVersion.version == row.current_bible_version,
                )
            )
            lineage = dict(bible.lineage_json or {}) if bible is not None else {}
            if bible is None or bible.status != "LOCKED" or lineage.get("lock_status") != "LOCKED":
                raise CreativeSessionConflict(
                    "the visual bible's identity and style locks are incomplete; compilation is blocked",
                    reason_code=ReasonCode.BIBLE_LOCK_INCOMPLETE.value,
                    details={"lineage": lineage},
                )
            brief = self._approved_brief(session, row)
            screenplay_row = self._approved_screenplay(session, row)
            screenplay = validate_screenplay(_content_without_audit(screenplay_row.content_json))
            beat_rows = list(
                session.scalars(
                    select(CreativeBeat)
                    .where(
                        CreativeBeat.session_id == session_id,
                        CreativeBeat.plan_revision == plan_revision,
                    )
                    .order_by(CreativeBeat.sequence)
                )
            )
            if not beat_rows:
                raise LookupError("beat plan is empty")
            fields = dict(brief.fields_json)
            brief_id = brief.id
            bible_id = bible.id
            project_id = row.project_id
            screenplay_id = screenplay_row.id
            skill = SkillText("", screenplay_row.skill_version, screenplay_row.skill_content_hash)
            parent_revision = screenplay_row.revision
            if edited_beats:
                try:
                    edited, changed = apply_beat_edits(screenplay, edited_beats)
                except ScreenplayInvalid as exc:
                    raise ValueError(f"edited beats rejected: {'; '.join(exc.details[:5])}") from exc
                if changed:
                    # A beat edit is a new screenplay revision, so it faces the
                    # same brief check every other revision does - the user
                    # cannot edit their way out of the brief they approved.
                    conformance = self.brief_validator.validate(
                        edited,
                        fields,
                        format_value=row.format,
                        provenance=dict(brief.provenance_json),
                        prohibitions=self._session_prohibitions(
                            self._turn_views(session, session_id)
                        ),
                    )
                    if conformance.blocking:
                        raise CreativeSessionConflict(
                            "the edited beats contradict the approved brief: "
                            + ", ".join(sorted({item.brief_path for item in conformance.blocking})),
                            reason_code=ReasonCode.SCREENPLAY_CONTRADICTS_BRIEF.value,
                            details={
                                "violations": [item.as_json() for item in conformance.blocking]
                            },
                        )
                    screenplay_row.status = "SUPERSEDED"
                    screenplay = edited
                    planned = self._materialize_beats(screenplay, fields)
                    script_text, _ = render_script(planned)
                    content = screenplay.model_dump(by_alias=True)
                    new_row = CreativeScreenplayRevision(
                        session_id=session_id,
                        revision=row.current_screenplay_revision + 1,
                        status="APPROVED",
                        brief_id=brief_id,
                        reasoner="USER_EDIT",
                        reason_codes=["USER_EDIT", "BEATS_EDITED_AT_APPROVAL"],
                        parent_revision=parent_revision,
                        user_notes=f"beats edited by {actor} at approval",
                        skill_version=skill.version,
                        skill_content_hash=skill.content_hash,
                        content_json=content,
                        script_text=script_text,
                        content_hash=screenplay_hash(content),
                        approved_at=_now(),
                    )
                    session.add(new_row)
                    session.flush()
                    row.current_screenplay_revision = new_row.revision
                    screenplay_id = new_row.id
                    for beat_row, beat in zip(beat_rows, planned, strict=False):
                        beat_row.beat_json = beat
                        beat_row.screenplay_id = new_row.id
            for beat_row in beat_rows:
                beat_row.status = "APPROVED"
            beats_json = [dict(beat_row.beat_json) for beat_row in beat_rows]
            approved_script = session.get(CreativeScreenplayRevision, screenplay_id)
            assert approved_script is not None
            script = approved_script.script_text
            obligations = [item.model_dump() for item in screenplay.obligations]
            current_anchors = self._current_anchors(session, session_id)
            anchor_ids_by_key = {anchor.anchor_key: anchor.id for anchor in current_anchors}
            #: The key visual behind each anchor, so a shot's declared anchors
            #: resolve to real reference media rather than staying names.
            anchor_media_by_key = {
                anchor.anchor_key: anchor.media_asset_id
                for anchor in current_anchors
                if anchor.media_asset_id
                and anchor.status == CreativeAnchorStatus.READY.value
            }
            session.flush()

        _script_check, ordered_intents = render_script(beats_json)
        if _script_check != script:
            raise CreativeSessionConflict(
                "the approved screenplay's script and its beat plan disagree; re-propose the beats",
                reason_code="SCRIPT_BEAT_MISMATCH",
            )
        with self.database.session() as session:
            from production_domain.models import Episode

            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            action_key = f"creative:{session_id}:episode:r{plan_revision}"
            existing_action = session.scalar(
                select(CreativeAction).where(CreativeAction.idempotency_key == action_key)
            )
            existing_episode_id = row.compiled_episode_id
            if existing_episode_id is None and existing_action is not None:
                existing_episode_id = existing_action.payload_json.get("episode_id")
            episode = session.get(Episode, existing_episode_id) if existing_episode_id else None
            if episode is None:
                next_number = (
                    session.scalar(
                        select(func.coalesce(func.max(Episode.episode_number), 0)).where(
                            Episode.project_id == project_id
                        )
                    )
                    + 1
                )
                episode = Episode(
                    project_id=project_id,
                    title=episode_title or (row.title[:200] or f"Episode {next_number}"),
                    episode_number=next_number,
                    script_source=script,
                )
                session.add(episode)
                session.flush()
            elif episode.project_id != project_id:
                raise CreativeSessionConflict("recorded episode belongs to another project")
            episode_id = episode.id
            episode_number = episode.episode_number
            row.compiled_episode_id = episode_id
            if existing_action is None:
                self._emit_action(
                    session,
                    row,
                    StructuredActionKind.CREATE_EPISODE,
                    {
                        "episode_id": episode_id,
                        "episode_number": episode_number,
                        "screenplay_id": screenplay_id,
                        "brief_id": brief_id,
                        "bible_id": bible_id,
                    },
                    idempotency_key=action_key,
                )
            elif existing_action.payload_json.get("episode_id") != episode_id:
                raise CreativeSessionConflict("episode action disagrees with the recorded episode")
            session.flush()

        result = self.orchestrator.compile_episode(episode_id)
        shot_ids = list(result.detail.get("shot_ids", []))
        self._apply_intents(shot_ids, ordered_intents, anchor_media_by_key, screenplay_id)
        # The frame-anchor plan reads shot type, state and references, all of
        # which the intents just changed; re-planning here is what makes the
        # director's staging reach the plan the generation preflight reuses.
        # Script compilation is what mints the Location rows, so a canonical
        # SCENE plate can only be bound to its location now. Do it before the
        # frame anchors are planned: `FrameAnchorPlanner._scene_asset_id` looks
        # the plate up by exactly this key.
        self._bind_scene_locations(project_id, episode_id, lineage)
        if bible_id is not None:
            with self.database.session() as session:
                bible_row = session.get(VisualBibleVersion, bible_id)
                if bible_row is not None:
                    bible_row.lineage_json = lineage
                    session.flush()
        replan = getattr(self.orchestrator, "plan_frame_anchors", None)
        if callable(replan):
            replan(episode_id)
        self._write_shot_lineage(
            session_id,
            episode_id=episode_id,
            brief_id=brief_id,
            screenplay_id=screenplay_id,
            bible_id=bible_id,
            lineage=lineage,
            beats_json=beats_json,
            shot_ids=shot_ids,
            ordered_intents=ordered_intents,
            anchor_ids_by_key=anchor_ids_by_key,
        )
        ledger_results = self._ledger_writes(
            session_id, project_id, episode_number, beats_json, plan_revision, obligations
        )

        with self.database.session() as session:
            row = self._session(session, session_id)
            self._emit_action(
                session,
                row,
                StructuredActionKind.COMPILE_EPISODE,
                {"episode_id": episode_id, "shot_ids": shot_ids, "screenplay_id": screenplay_id},
                idempotency_key=f"creative:{session_id}:compile:r{plan_revision}",
            )
            for action in session.scalars(
                select(CreativeAction).where(
                    CreativeAction.session_id == session_id,
                    CreativeAction.status == CreativeActionStatus.PROPOSED.value,
                    CreativeAction.kind.in_(
                        [
                            StructuredActionKind.CREATE_EPISODE.value,
                            StructuredActionKind.COMPILE_EPISODE.value,
                            StructuredActionKind.OPEN_OBLIGATION.value,
                            StructuredActionKind.ESTABLISH_FACT.value,
                        ]
                    ),
                )
            ):
                action.status = CreativeActionStatus.EXECUTED.value
                action.executed_at = _now()
            row.status = CreativeSessionStatus.COMPILED.value
            row.compiled_episode_id = episode_id
            session.flush()
            view = self._compiled_view(session, row)
            view["ledger"] = ledger_results
            return view

    def _apply_intents(
        self,
        shot_ids: list[str],
        intents: list[dict[str, Any]],
        anchor_media_by_key: dict[str, str] | None = None,
        screenplay_id: str | None = None,
    ) -> None:
        from .beats import ShotIntentMismatch, apply_shot_intents

        try:
            apply_shot_intents(
                self.database,
                shot_ids,
                intents,
                reference_asset_ids_by_anchor=anchor_media_by_key,
                screenplay_id=screenplay_id,
            )
        except ShotIntentMismatch as exc:
            raise CreativeSessionConflict(str(exc), reason_code="SHOT_INTENT_MISMATCH") from exc

    def _bind_scene_locations(
        self, project_id: str, episode_id: str, lineage: dict[str, Any]
    ) -> None:
        """Point each canonical SCENE asset at the Location the compiler made.

        The frame-anchor planner resolves a shot's scene plate by matching
        ``Asset.canonical_metadata["location_id"]`` against the scene's
        location; without this the planner finds no canonical scene reference
        and every RECONSTRUCT_FIRST_FRAME plan downgrades to a fresh start.
        """

        if self.asset_registry is None:
            return
        scenes = {
            key: item
            for key, item in (lineage.get("assets") or {}).items()
            if isinstance(item, dict) and item.get("kind") == "SCENE" and item.get("asset_id")
        }
        if not scenes:
            return
        from production_domain.models import Location, Scene

        with self.database.session() as session:
            location_ids = {
                str(name).casefold(): location_id
                for location_id, name in session.execute(
                    select(Location.id, Location.name).where(Location.project_id == project_id)
                ).tuples()
            }
            scene_location_ids = set(
                session.scalars(
                    select(Scene.location_id).where(
                        Scene.episode_id == episode_id, Scene.location_id.is_not(None)
                    )
                )
            )
        for key, item in scenes.items():
            location_id = location_ids.get(str(item.get("subject") or "").casefold())
            if location_id is None or location_id not in scene_location_ids:
                # The compiler named the location differently than the anchor
                # did; recorded as unbound rather than guessed at.
                item["location_id"] = None
                item["location_bound"] = False
                continue
            self.asset_registry.annotate(
                item["asset_id"], canonical_metadata={"location_id": location_id}
            )
            item["location_id"] = location_id
            item["location_bound"] = True
            _ = key

    def _write_shot_lineage(  # noqa: PLR0913
        self,
        session_id: str,
        *,
        episode_id: str,
        brief_id: str,
        screenplay_id: str,
        bible_id: str | None,
        lineage: dict[str, Any],
        beats_json: list[dict[str, Any]],
        shot_ids: list[str],
        ordered_intents: list[dict[str, Any]],
        anchor_ids_by_key: dict[str, str],
    ) -> None:
        positions: list[tuple[int, int]] = []
        for beat in beats_json:
            for index, shot in enumerate(beat.get("shots", []), 1):
                if str(shot.get("action") or "").strip():
                    positions.append((int(beat.get("sequence", 0)), index))
        identities = {
            key: value for key, value in (lineage.get("identities") or {}).items() if isinstance(value, dict)
        }
        with self.database.session() as session:
            existing = {
                item.shot_id
                for item in session.scalars(
                    select(CreativeShotLineage).where(CreativeShotLineage.session_id == session_id)
                )
            }
            for shot_id, intent, position in zip(shot_ids, ordered_intents, positions, strict=False):
                if shot_id in existing:
                    continue
                anchor_keys = [str(key) for key in intent.get("anchors") or []]
                unresolved = [key for key in anchor_keys if key not in anchor_ids_by_key]
                # Every character on screen must have a key visual behind it.
                # An optional scene or prop the user skipped is a recorded
                # decision; a character with no anchor is an unanchored face,
                # and compiling one silently is the defect this refuses.
                missing_characters = [
                    key
                    for key in unresolved
                    if key.startswith("character:")
                ] + [
                    key
                    for key in anchor_keys
                    if key.startswith("character:")
                    and key in anchor_ids_by_key
                    and not (identities.get(key) or {}).get("identity_version_id")
                ]
                if missing_characters:
                    raise CreativeSessionConflict(
                        "compiled shots reference characters with no locked identity: "
                        + ", ".join(sorted(set(missing_characters))),
                        reason_code=ReasonCode.CHARACTER_IDENTITY_NOT_COVERED.value,
                        details={"shot_id": shot_id, "anchor_keys": sorted(set(missing_characters))},
                    )
                session.add(
                    CreativeShotLineage(
                        session_id=session_id,
                        shot_id=shot_id,
                        episode_id=episode_id,
                        brief_id=brief_id,
                        screenplay_id=screenplay_id,
                        bible_id=bible_id,
                        beat_sequence=position[0],
                        shot_sequence=position[1],
                        anchor_ids=[
                            anchor_ids_by_key[key] for key in anchor_keys if key in anchor_ids_by_key
                        ],
                        identity_version_ids=[
                            identities[key]["identity_version_id"]
                            for key in anchor_keys
                            if key in identities and identities[key].get("identity_version_id")
                        ],
                        style_lock_id=lineage.get("style_lock_id"),
                        intent_json={**intent, "unresolved_anchor_keys": unresolved},
                    )
                )
            session.flush()

    def _ledger_writes(  # noqa: PLR0913
        self,
        session_id: str,
        project_id: str,
        episode_number: int,
        beats_json: list[dict[str, Any]],
        plan_revision: int,
        obligations: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Open the obligations the screenplay promises (cliffhanger, declared obligations)."""

        if self.ledger is None:
            return []
        results: list[dict[str, Any]] = []
        wanted: list[tuple[str, str, str]] = []
        cliffhanger = next((beat for beat in beats_json if beat.get("intent") == "CLIFFHANGER"), None)
        if cliffhanger is not None:
            promise = str(cliffhanger.get("summary") or "resolve the episode cliffhanger")
            dialogue = next(
                (shot.get("dialogue") for shot in cliffhanger.get("shots", []) if shot.get("dialogue")),
                None,
            )
            if dialogue:
                promise = f"resolve: {dialogue}"
            wanted.append((f"creative:{session_id}:ep{episode_number}:cliffhanger", promise, "CLIFFHANGER"))
        for item in obligations or []:
            key = str(item.get("key") or "").strip()
            promise = str(item.get("promise") or "").strip()
            if key and promise:
                wanted.append(
                    (
                        f"creative:{session_id}:ep{episode_number}:{key}"[:160],
                        promise,
                        str(item.get("category") or "GENERIC"),
                    )
                )
        for index, (obligation_key, promise, category) in enumerate(wanted):
            # `open_obligation` is idempotent on the key: an identical confirm
            # replay returns the existing row without raising. A ValueError
            # here therefore means a *real* conflict - a revised plan changed
            # the promise under the same key - and it is reported as one.
            try:
                obligation_id = self.ledger.open_obligation(
                    project_id,
                    obligation_key=obligation_key,
                    promise=promise,
                    episode=episode_number,
                    category=category,
                )
            except ValueError as exc:
                results.append(
                    {"kind": "OPEN_OBLIGATION", "key": obligation_key, "conflict": True, "error": str(exc)}
                )
                continue
            results.append({"kind": "OPEN_OBLIGATION", "id": obligation_id, "key": obligation_key})
            with self.database.session() as session:
                row = self._session(session, session_id)
                self._emit_action(
                    session,
                    row,
                    StructuredActionKind.OPEN_OBLIGATION,
                    {"obligation_key": obligation_key, "promise": promise, "category": category},
                    idempotency_key=f"creative:{session_id}:obligation:r{plan_revision}:{index}",
                )
        return results

    # ---------------------------------------------------------------- reads
    def session_state(self, session_id: str) -> CreativeSessionState:
        with self.database.session() as session:
            row = self._session(session, session_id)
            brief = self._head_brief(session, row)
            turns = self._turn_views(session, session_id)
            anchors = [_anchor_view(anchor) for anchor in self._current_anchors(session, session_id)]
            superseded = [
                _anchor_view(anchor)
                for anchor in session.scalars(
                    select(CreativeVisualAnchor).where(
                        CreativeVisualAnchor.session_id == session_id,
                        CreativeVisualAnchor.status == CreativeAnchorStatus.SUPERSEDED.value,
                    )
                )
            ]
            bible_row = session.scalar(
                select(VisualBibleVersion).where(
                    VisualBibleVersion.session_id == session_id,
                    VisualBibleVersion.version == row.current_bible_version,
                )
            )
            beats = self._beats_view(session, session_id, row.current_beat_revision)
            actions = [
                _action_view(action)
                for action in session.scalars(
                    select(CreativeAction)
                    .where(CreativeAction.session_id == session_id)
                    .order_by(CreativeAction.sequence)
                )
            ]
            screenplay_rows = list(
                session.scalars(
                    select(CreativeScreenplayRevision)
                    .where(CreativeScreenplayRevision.session_id == session_id)
                    .order_by(CreativeScreenplayRevision.revision)
                )
            )
            current_screenplay = next(
                (item for item in screenplay_rows if item.revision == row.current_screenplay_revision), None
            )
            brief_view = self._brief_view(brief) if brief is not None else None
            coverage: dict[str, Any] | None = None
            conformance_json: list[dict[str, Any]] = []
            approved_brief = None
            if row.status not in _DIALOGUE_STATUSES:
                approved_brief = session.scalar(
                    select(CreativeBriefRevision)
                    .where(
                        CreativeBriefRevision.session_id == session_id,
                        CreativeBriefRevision.status == "APPROVED",
                    )
                    .order_by(CreativeBriefRevision.revision.desc())
                )
            if current_screenplay is not None and brief is not None:
                try:
                    screenplay_model = validate_screenplay(
                        _content_without_audit(current_screenplay.content_json)
                    )
                except ScreenplayInvalid:
                    screenplay_model = None
                if screenplay_model is not None:
                    coverage = derive_anchors(dict(brief.fields_json), screenplay_model).coverage_json()
                    if approved_brief is not None:
                        conformance_json = self.brief_validator.validate(
                            screenplay_model,
                            dict(approved_brief.fields_json),
                            format_value=row.format,
                            provenance=dict(approved_brief.provenance_json),
                            prohibitions=self._session_prohibitions(turns),
                        ).as_json()
            return CreativeSessionState(
                session={
                    "id": row.id,
                    "project_id": row.project_id,
                    "title": row.title,
                    "status": row.status,
                    "format": row.format,
                    "brief_revision": row.current_brief_revision,
                    "screenplay_revision": row.current_screenplay_revision,
                    "bible_version": row.current_bible_version,
                    "beat_revision": row.current_beat_revision,
                    "compiled_episode_id": row.compiled_episode_id,
                    "superseded_anchors": superseded,
                    #: What the pipeline can carry end to end; the SPA reads
                    #: this rather than mirroring the number in JS.
                    "limits": {
                        "max_cast": MAX_CAST,
                        "max_scene_anchors": MAX_SCENE_ANCHORS,
                        "max_prop_anchors": MAX_PROP_ANCHORS,
                    },
                    #: Which screenplay elements deliberately have no key
                    #: visual, and why.
                    "anchor_coverage": coverage,
                    "shot_lineage_count": int(
                        session.scalar(
                            select(func.count())
                            .select_from(CreativeShotLineage)
                            .where(CreativeShotLineage.session_id == session_id)
                        )
                        or 0
                    ),
                },
                brief=brief_view,
                turns=turns,
                anchors=anchors,
                bible=_bible_view(bible_row) if bible_row is not None else None,
                beats=beats,
                actions=actions,
                screenplay=(
                    {
                        **self._screenplay_view(current_screenplay),
                        "brief_conformance": conformance_json,
                    }
                    if current_screenplay is not None
                    else None
                ),
                screenplays=[
                    {
                        "id": item.id,
                        "revision": item.revision,
                        "status": item.status,
                        "reasoner": item.reasoner,
                        "reason_codes": list(item.reason_codes),
                        "parent_revision": item.parent_revision,
                        "created_at": item.created_at.isoformat() if item.created_at else None,
                    }
                    for item in screenplay_rows
                ],
            )

    def shot_lineage(self, shot_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.scalar(select(CreativeShotLineage).where(CreativeShotLineage.shot_id == shot_id))
            if row is None:
                raise LookupError("no creative lineage for this shot")
            creative = session.get(CreativeSession, row.session_id)
            brief = session.get(CreativeBriefRevision, row.brief_id)
            screenplay = session.get(CreativeScreenplayRevision, row.screenplay_id)
            bible = session.get(VisualBibleVersion, row.bible_id) if row.bible_id else None
            anchors = [
                _anchor_view(anchor)
                for anchor in (session.get(CreativeVisualAnchor, anchor_id) for anchor_id in row.anchor_ids)
                if anchor is not None
            ]
            return {
                "shot_id": row.shot_id,
                "episode_id": row.episode_id,
                "session_id": row.session_id,
                "project_id": creative.project_id if creative is not None else None,
                "beat_sequence": row.beat_sequence,
                "shot_sequence": row.shot_sequence,
                "brief": {"id": row.brief_id, "revision": brief.revision, "content_hash": brief.content_hash}
                if brief is not None
                else {"id": row.brief_id},
                "screenplay": {
                    "id": row.screenplay_id,
                    "revision": screenplay.revision,
                    "content_hash": screenplay.content_hash,
                    "reasoner": screenplay.reasoner,
                    "skill_version": screenplay.skill_version,
                }
                if screenplay is not None
                else {"id": row.screenplay_id},
                "bible": {"id": row.bible_id, "version": bible.version, "content_hash": bible.content_hash}
                if bible is not None
                else None,
                "anchors": anchors,
                "identity_version_ids": list(row.identity_version_ids),
                "style_lock_id": row.style_lock_id,
                "intent": row.intent_json,
            }

    def list_sessions(self, project_id: str, *, include_abandoned: bool = False) -> list[dict[str, Any]]:
        with self.database.session() as session:
            statement = select(CreativeSession).where(CreativeSession.project_id == project_id)
            if not include_abandoned:
                statement = statement.where(CreativeSession.status != CreativeSessionStatus.ABANDONED.value)
            return [
                {
                    "id": row.id,
                    "title": row.title,
                    "status": row.status,
                    "format": row.format,
                    "compiled_episode_id": row.compiled_episode_id,
                }
                for row in session.scalars(statement.order_by(CreativeSession.updated_at.desc()))
            ]

    def abandon_session(self, session_id: str) -> dict[str, Any]:
        """Retire a conversation: it leaves the list and stops accepting turns.

        Rows are kept (turns, briefs, anchors and their paid generations are
        history the ledgers reference); ABANDONED is the one backward
        transition the lifecycle allows. A compiled session is part of an
        episode and stays.
        """

        with self.database.session() as session:
            row = session.scalar(
                select(CreativeSession).where(CreativeSession.id == session_id).with_for_update()
            )
            if row is None:
                raise LookupError("creative session not found")
            if row.status == CreativeSessionStatus.COMPILED.value:
                raise CreativeSessionConflict(
                    "a compiled session is part of an episode and cannot be deleted",
                    reason_code="SESSION_COMPILED",
                )
            row.status = CreativeSessionStatus.ABANDONED.value
            session.flush()
            return {"id": row.id, "status": row.status}

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _turn_views(session: Any, session_id: str) -> list[dict[str, Any]]:
        views: list[dict[str, Any]] = []
        for turn in session.scalars(
            select(CreativeTurn).where(CreativeTurn.session_id == session_id).order_by(CreativeTurn.sequence)
        ):
            extracted = turn.extracted_json
            views.append(
                {
                    "id": turn.id,
                    "sequence": turn.sequence,
                    "speaker": turn.speaker,
                    "content": turn.content,
                    "questions": turn.questions_json,
                    "operations": extracted if isinstance(extracted, list) else [],
                    "reasoner": turn.reasoner,
                    "reason_codes": list(turn.reason_codes or []),
                    "brief_revision": turn.brief_revision,
                    "skill_version": turn.skill_version,
                    "skill_content_hash": turn.skill_content_hash,
                    "model_execution_record_id": turn.model_execution_record_id,
                    "context": turn.context_json,
                    "result": turn.result_json,
                    "client_turn_id": turn.client_turn_id,
                    "created_at": turn.created_at.isoformat() if turn.created_at else None,
                }
            )
        return views

    @staticmethod
    def _session(session: Any, session_id: str) -> CreativeSession:
        row = session.get(CreativeSession, session_id)
        if row is None:
            raise LookupError("creative session not found")
        return row

    @staticmethod
    def _next_turn_sequence(session: Any, session_id: str) -> int:
        return (
            session.scalar(
                select(func.coalesce(func.max(CreativeTurn.sequence), 0)).where(
                    CreativeTurn.session_id == session_id
                )
            )
            + 1
        )

    @staticmethod
    def _brief_at(session: Any, session_id: str, revision: int) -> CreativeBriefRevision | None:
        if revision <= 0:
            return None
        return session.scalar(
            select(CreativeBriefRevision).where(
                CreativeBriefRevision.session_id == session_id,
                CreativeBriefRevision.revision == revision,
            )
        )

    def _head_brief(self, session: Any, row: CreativeSession) -> CreativeBriefRevision | None:
        return self._brief_at(session, row.id, row.current_brief_revision)

    @staticmethod
    def _approved_brief(session: Any, row: CreativeSession) -> CreativeBriefRevision:
        brief = session.scalar(
            select(CreativeBriefRevision)
            .where(
                CreativeBriefRevision.session_id == row.id,
                CreativeBriefRevision.status == "APPROVED",
            )
            .order_by(CreativeBriefRevision.revision.desc())
        )
        if brief is None:
            raise CreativeSessionConflict(
                "no approved brief on this session", reason_code="BRIEF_NOT_APPROVED"
            )
        return brief

    @staticmethod
    def _screenplay_at(session: Any, session_id: str, revision: int) -> CreativeScreenplayRevision | None:
        if revision <= 0:
            return None
        return session.scalar(
            select(CreativeScreenplayRevision).where(
                CreativeScreenplayRevision.session_id == session_id,
                CreativeScreenplayRevision.revision == revision,
            )
        )

    @staticmethod
    def _approved_screenplay(session: Any, row: CreativeSession) -> CreativeScreenplayRevision:
        screenplay = session.scalar(
            select(CreativeScreenplayRevision)
            .where(
                CreativeScreenplayRevision.session_id == row.id,
                CreativeScreenplayRevision.status == "APPROVED",
            )
            .order_by(CreativeScreenplayRevision.revision.desc())
        )
        if screenplay is None:
            raise CreativeSessionConflict(
                "no approved screenplay on this session", reason_code="SCREENPLAY_NOT_APPROVED"
            )
        return screenplay

    def _brief_view(self, brief: CreativeBriefRevision) -> dict[str, Any]:
        completeness = dict(brief.completeness_json or {})
        return {
            "id": brief.id,
            "revision": brief.revision,
            "status": brief.status,
            "source": brief.source,
            "fields": brief.fields_json,
            "completeness": completeness,
            "provenance": brief.provenance_json,
            "question_states": brief.question_state_json,
            "assumptions": completeness.get("assumptions") or [],
            "blocking": completeness.get("blocking") or [],
            "proposable": bool(completeness.get("proposable")),
            "content_hash": brief.content_hash,
            "approved_at": brief.approved_at.isoformat() if brief.approved_at else None,
        }

    @staticmethod
    def _screenplay_view(row: CreativeScreenplayRevision) -> dict[str, Any]:
        content = dict(row.content_json or {})
        audit = content.pop("_context", None)
        return {
            "id": row.id,
            "revision": row.revision,
            "status": row.status,
            "reasoner": row.reasoner,
            "reason_codes": list(row.reason_codes or []),
            "deterministic": row.reasoner == "DETERMINISTIC",
            "parent_revision": row.parent_revision,
            "user_notes": row.user_notes,
            "skill_version": row.skill_version,
            "skill_content_hash": row.skill_content_hash,
            "model_execution_record_id": row.model_execution_record_id,
            "content": content,
            "context": audit,
            "script_text": row.script_text,
            "content_hash": row.content_hash,
            "approved_at": row.approved_at.isoformat() if row.approved_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def _beats_view(self, session: Any, session_id: str, plan_revision: int) -> list[dict[str, Any]]:
        if plan_revision == 0:
            return []
        return [
            {
                "sequence": beat.sequence,
                "status": beat.status,
                "screenplay_id": beat.screenplay_id,
                **dict(beat.beat_json),
            }
            for beat in session.scalars(
                select(CreativeBeat)
                .where(
                    CreativeBeat.session_id == session_id,
                    CreativeBeat.plan_revision == plan_revision,
                )
                .order_by(CreativeBeat.sequence)
            )
        ]

    def _compiled_view(self, session: Any, row: CreativeSession) -> dict[str, Any]:
        compile_action = session.scalar(
            select(CreativeAction).where(
                CreativeAction.session_id == row.id,
                CreativeAction.kind == StructuredActionKind.COMPILE_EPISODE.value,
            )
        )
        return {
            "session_id": row.id,
            "status": row.status,
            "episode_id": row.compiled_episode_id,
            "screenplay_revision": row.current_screenplay_revision,
            "shot_ids": (
                list(compile_action.payload_json.get("shot_ids", [])) if compile_action is not None else []
            ),
        }


# ------------------------------------------------------------------ helpers
def _first_choice_json(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
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
            # A live model sometimes wraps the object in a sentence or two;
            # the outermost braces are the object, the prose is not.
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise
            parsed = json.loads(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("chat response is not a JSON object")


def _parse_turn_result(raw: dict[str, Any]) -> tuple[DirectorTurnResult, list[str]]:
    """Validate the model's turn; accept the pre-2026-09-02 shapes as degraded input."""

    codes: list[str] = []
    payload = dict(raw)
    if "assistant_message" not in payload and isinstance(payload.get("reply"), str):
        payload["assistant_message"] = payload["reply"]
        codes.append("LEGACY_REPLY_SHAPE")
    if "brief_operations" not in payload:
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else None
        if fields is None and "assistant_message" not in payload:
            # A bare field object: every key is a proposed field.
            fields = {key: value for key, value in payload.items() if key not in {"reply"}}
        if fields:
            payload["brief_operations"] = [
                {"op": "SET", "path": key, "value": value, "confidence": "INFERRED", "evidence": ""}
                for key, value in fields.items()
            ]
            codes.append("LEGACY_FIELDS_SHAPE")
    if not str(payload.get("assistant_message") or "").strip():
        payload["assistant_message"] = "…"
        codes.append("MODEL_NO_REPLY")
    result = DirectorTurnResult.model_validate(payload)
    if result.assistant_message == "…":
        # No words from the model: the service's language-aware sentence
        # stands in, and the turn says so.
        result = result.model_copy(update={"assistant_message": ""})
    codes.insert(0, ReasonCode.MODEL_REPLY.value if result.assistant_message else "MODEL_NO_REPLY")
    return result, codes


def _deterministic_message(content: str, *, proposable: bool) -> str:
    if _is_cjk_text(content):
        return (
            "这是我根据我们的对话整理出的创意简报，请审阅：批准，或告诉我要改什么。"
            if proposable
            else "还有几点会让方案更清晰："
        )
    return (
        "Here is the creative brief I put together. Review it and approve, or tell me what to change."
        if proposable
        else "A few things would sharpen this a lot:"
    )


def _content_without_audit(content: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in dict(content or {}).items() if key != "_context"}


def _aspect(fields: dict[str, Any]) -> str:
    return str(fields.get("aspect_ratio") or "9:16")


def _compose_anchor_prompt(kind: str, prompt_json: dict[str, Any]) -> str:
    """Compose the provider prompt for one anchor from its structured parts."""

    style = prompt_json.get("style") or {}
    style_terms = ", ".join(
        term
        for term in [
            str(style.get("medium") or ""),
            str(style.get("palette") or ""),
            ", ".join(style.get("tone") or []),
            str(style.get("direction") or ""),
        ]
        if term
    )
    subject = str(prompt_json.get("subject") or "").strip()
    if kind == "CHARACTER":
        look = str(prompt_json.get("look") or "").strip()
        role = str(prompt_json.get("role") or "").strip()
        base = f"Character reference of {subject}"
        if role:
            base += f" ({role})"
        if look:
            base += f", {look}"
        base += ", full body, neutral background, consistent identity"
    elif kind == "SCENE":
        time_value = str(prompt_json.get("time") or "").strip()
        description = str(prompt_json.get("description") or "").strip()
        base = f"Establishing view of {subject}"
        if time_value:
            base += f" at {time_value.lower()}"
        if description:
            base += f", {description}"
        base += ", no people, location reference plate"
    elif kind == "PRODUCT":
        points = list(prompt_json.get("selling_points") or []) + list(prompt_json.get("claims") or [])
        base = f"Hero shot of {subject}"
        if points:
            base += ", " + ", ".join(str(point) for point in points[:3])
        base += ", clean studio backdrop"
    elif kind == "PROP":
        base = f"Prop reference of {subject}, isolated, neutral background"
    else:
        base = f"Style key plate: {subject}" if subject else "Style key plate"
    return f"{base}. {style_terms}".strip().rstrip(".") + "."


def _action_view(action: CreativeAction) -> dict[str, Any]:
    return {
        "id": action.id,
        "sequence": action.sequence,
        "kind": action.kind,
        "status": action.status,
        "payload": action.payload_json,
        "result": action.result_json,
        "idempotency_key": action.idempotency_key,
        "executed_at": action.executed_at.isoformat() if action.executed_at else None,
    }


def _anchor_view(anchor: CreativeVisualAnchor) -> dict[str, Any]:
    return {
        "id": anchor.id,
        "anchor_key": anchor.anchor_key,
        "version": anchor.version,
        "kind": anchor.kind,
        "title": anchor.title,
        "required": bool(anchor.required),
        "status": anchor.status,
        "generation_job_id": anchor.generation_job_id,
        "media_asset_id": anchor.media_asset_id,
        "character_id": anchor.character_id,
        "failure_code": anchor.failure_code,
        "skip_reason": anchor.skip_reason,
        "prompt_hash": anchor.prompt_hash,
        "prompt": anchor.prompt_json,
        "brief_id": anchor.brief_id,
        "screenplay_id": anchor.screenplay_id,
    }


def _anchor_summary(anchors: list[CreativeVisualAnchor]) -> dict[str, Any]:
    views = [_anchor_view(anchor) for anchor in anchors]
    terminal = {
        CreativeAnchorStatus.READY.value,
        CreativeAnchorStatus.FAILED.value,
        CreativeAnchorStatus.SKIPPED.value,
    }
    required_not_ready = [
        view for view in views if view["required"] and view["status"] != CreativeAnchorStatus.READY.value
    ]
    optional_not_terminal = [
        view
        for view in views
        if not view["required"]
        and view["status"] not in {CreativeAnchorStatus.READY.value, CreativeAnchorStatus.SKIPPED.value}
    ]
    return {
        "anchors": views,
        "all_terminal": bool(views) and all(view["status"] in terminal for view in views),
        "ready": sum(1 for view in views if view["status"] == CreativeAnchorStatus.READY.value),
        "failed": sum(1 for view in views if view["status"] == CreativeAnchorStatus.FAILED.value),
        "skipped": sum(1 for view in views if view["status"] == CreativeAnchorStatus.SKIPPED.value),
        "required_not_ready": required_not_ready,
        "optional_not_terminal": optional_not_terminal,
        "can_propose_bible": bool(views) and not required_not_ready and not optional_not_terminal,
    }


def _bible_view(bible: VisualBibleVersion) -> dict[str, Any]:
    return {
        "id": bible.id,
        "version": bible.version,
        "status": bible.status,
        "content": bible.content_json,
        "content_hash": bible.content_hash,
        "screenplay_id": bible.screenplay_id,
        "lineage": bible.lineage_json,
        "locked_at": bible.locked_at.isoformat() if bible.locked_at else None,
        "locked_by": bible.locked_by,
    }


__all__ = [
    "COMMERCE_FORMATS",
    "RETRYABLE_REASON_CODES",
    "CreativeDirectorService",
    "CreativeSessionConflict",
    "CreativeSessionState",
    "CreativeTurnLimitReached",
    "DirectorReply",
    "is_assumed",
]
