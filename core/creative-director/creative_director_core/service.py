"""The stateful creative director.

The service owns dialogue state, brief revisions, the visual bible and the
beat plan. It emits **structured actions** for anything that spends money or
touches the production chain - key visual generation, episode creation,
ledger writes - and never reaches a provider itself; the API layer executes
those actions through the existing admission / credit / router / gateway
path. Model reasoning goes through ``ModelRoleRuntime`` when it is available
and degrades to the deterministic rules engine with the degradation recorded,
never silently.
"""

from __future__ import annotations

import json
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
    CreativeSession,
    CreativeSessionStatus,
    CreativeTurn,
    CreativeVisualAnchor,
    GenerationJob,
    JobStatus,
    Project,
    VisualBibleVersion,
    Workspace,
)
from sqlalchemy import func, select

from .beats import BeatPlanner, render_script
from .brief import BriefEngine, brief_hash, get_path
from .schemas import StructuredActionKind


class CreativeSessionConflict(ValueError):
    """The request contradicts the session's recorded state."""


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


@dataclass(frozen=True)
class CreativeSessionState:
    session: dict[str, Any]
    brief: dict[str, Any] | None
    turns: list[dict[str, Any]]
    anchors: list[dict[str, Any]]
    bible: dict[str, Any] | None
    beats: list[dict[str, Any]]
    actions: list[dict[str, Any]]


def _now() -> datetime:
    return datetime.now(UTC)


class CreativeDirectorService:
    version = "creative-director-v1"

    def __init__(
        self,
        database: Database,
        *,
        orchestrator: EpisodeCompiler | None = None,
        ledger: SeriesLedger | None = None,
        model_roles: ModelReasoner | None = None,
        free_plan_turn_limit: int = 10,
    ):
        self.database = database
        self.orchestrator = orchestrator
        self.ledger = ledger
        self.model_roles = model_roles
        self.free_plan_turn_limit = max(0, int(free_plan_turn_limit))
        self.briefs = BriefEngine()
        self.beats = BeatPlanner()

    # ------------------------------------------------------------ reasoning
    async def _model_patch(
        self, project_id: str, *, fields: dict[str, Any], text: str
    ) -> tuple[dict[str, Any], str, list[str]]:
        """Ask the DIRECTOR role to extract brief fields; degrade loudly.

        The model's patch passes through the same guarded merge as the rules
        engine's, so it can fill gaps but never overwrite the user's answers,
        and a model outage records DETERMINISTIC instead of failing the turn.
        """

        deterministic = self.briefs.extract(text, fields)
        if self.model_roles is None:
            return deterministic, "DETERMINISTIC", ["MODEL_RUNTIME_NOT_CONFIGURED"]
        from entitlement_core.canary import LiveCanaryConflict, LiveSpendDenied
        from model_registry_core import ModelRole
        from provider_sdk import ProviderError, ProviderTrustViolation

        try:
            execution = await self.model_roles.execute_chat(
                project_id,
                ModelRole.DIRECTOR,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You extract structured creative-brief fields from a user's idea. "
                            "Answer JSON only, with any of: format, logline, duration_seconds, "
                            "platform, aspect_ratio, tone (list), visual_style {medium, palette}, "
                            "characters (list of {name, role, look}), setting {location, time}, "
                            "product {name, selling_points}, hook, call_to_action, music {mood}, "
                            "audience. Omit anything the text does not state."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"known_fields": fields, "text": text}, ensure_ascii=False
                        ),
                    },
                ],
                parameters={"response_format": {"type": "json_object"}},
            )
            raw = _first_choice_json(execution.response)
        except (
            LiveCanaryConflict,
            # A refused live-spend reservation — a missing canary permit or a
            # tripped production breaker — is a budget refusal, not a platform
            # fault: the turn degrades to the deterministic engine loudly
            # instead of failing the user's whole request with a 500 — which
            # is exactly what happened on production on 2026-08-30.
            LiveSpendDenied,
            LookupError,
            ProviderError,
            ProviderTrustViolation,
            TypeError,
            ValueError,
        ) as exc:
            return (
                deterministic,
                "DETERMINISTIC",
                ["MODEL_UNAVAILABLE", type(exc).__name__],
            )
        patch = _sanitize_model_patch(raw)
        merged = self.briefs.merge(deterministic, patch)
        return merged, "MODEL:DIRECTOR", ["MODEL_EXTRACTION_MERGED"]

    # ------------------------------------------------------------- dialogue
    async def start_session(
        self,
        project_id: str,
        *,
        idea: str,
        workspace_id: str | None = None,
        format_hint: str | None = None,
        title: str = "",
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
        return await self._user_turn(session_id, idea, format_hint=format_hint)

    async def post_message(self, session_id: str, content: str) -> DirectorReply:
        with self.database.session() as session:
            row = self._session(session, session_id)
            if row.status in {
                CreativeSessionStatus.COMPILED.value,
                CreativeSessionStatus.ABANDONED.value,
            }:
                raise CreativeSessionConflict(f"session is {row.status}; the dialogue is closed")
        return await self._user_turn(session_id, content, format_hint=None)

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
                "session; upgrade to Pro to keep the conversation going"
            )

    async def _user_turn(
        self, session_id: str, content: str, *, format_hint: str | None
    ) -> DirectorReply:
        with self.database.session() as session:
            row = self._session(session, session_id)
            self._assert_turn_budget(session, row)
            project_id = row.project_id
            fields = self._current_fields(session, row)
            asked = self._asked_codes(session, session_id)
            next_sequence = self._next_turn_sequence(session, session_id)
            session.add(
                CreativeTurn(
                    session_id=session_id,
                    sequence=next_sequence,
                    speaker="USER",
                    content=content,
                    reasoner="USER",
                )
            )
            session.flush()

        if format_hint:
            fields["format"] = CreativeFormat(format_hint).value
        patch, reasoner, reason_codes = await self._model_patch(
            project_id, fields=fields, text=content
        )
        merged = self.briefs.merge(fields, patch)
        format_value = str(merged.get("format") or CreativeFormat.UNSPECIFIED.value)
        analysis = self.briefs.analyze(merged, format_value=format_value, asked_codes=asked)

        with self.database.session() as session:
            row = self._session(session, session_id)
            row.format = format_value
            revision = row.current_brief_revision + 1
            for path, value in analysis.applied_defaults.items():
                if get_path(merged, path) is None:
                    _set_default(merged, path, value)
            previous = session.scalar(
                select(CreativeBriefRevision).where(
                    CreativeBriefRevision.session_id == session_id,
                    CreativeBriefRevision.status == "PROPOSED",
                )
            )
            if previous is not None:
                previous.status = "SUPERSEDED"
            session.add(
                CreativeBriefRevision(
                    session_id=session_id,
                    revision=revision,
                    fields_json=merged,
                    completeness_json=analysis.completeness(),
                    content_hash=brief_hash(merged),
                )
            )
            row.current_brief_revision = revision
            questions = [
                {"code": gap.code, "question": gap.question, "weight": gap.weight}
                for gap in analysis.questions
            ]
            if analysis.proposable:
                row.status = CreativeSessionStatus.BRIEF_PROPOSED.value
                message = (
                    "Here is the creative brief I put together. Review it and approve, "
                    "or tell me what to change."
                )
            else:
                row.status = CreativeSessionStatus.CLARIFYING.value
                message = "A few things would sharpen this a lot:"
            session.add(
                CreativeTurn(
                    session_id=session_id,
                    sequence=self._next_turn_sequence(session, session_id),
                    speaker="DIRECTOR",
                    content=message,
                    questions_json=questions,
                    extracted_json=patch,
                    reasoner=reasoner,
                    reason_codes=list(reason_codes),
                    brief_revision=revision,
                )
            )
            session.flush()
            return DirectorReply(
                session_id=session_id,
                status=row.status,
                message=message,
                questions=questions,
                brief_revision=revision,
                proposable=analysis.proposable,
                reasoner=reasoner,
                reason_codes=list(reason_codes),
            )

    # ------------------------------------------------------------- approval
    def approve_brief(self, session_id: str, *, revision: int, actor: str) -> list[dict[str, Any]]:
        """Freeze the brief and emit the key-visual actions it implies."""

        with self.database.session() as session:
            row = self._session(session, session_id)
            if row.status not in {
                CreativeSessionStatus.BRIEF_PROPOSED.value,
                CreativeSessionStatus.CLARIFYING.value,
            }:
                raise CreativeSessionConflict(f"brief is not approvable from {row.status}")
            brief = session.scalar(
                select(CreativeBriefRevision).where(
                    CreativeBriefRevision.session_id == session_id,
                    CreativeBriefRevision.revision == revision,
                )
            )
            if brief is None:
                raise LookupError("brief revision not found")
            if brief.revision != row.current_brief_revision:
                raise CreativeSessionConflict(
                    f"revision {revision} is superseded by {row.current_brief_revision}"
                )
            brief.status = "APPROVED"
            brief.approved_at = _now()
            row.status = CreativeSessionStatus.BRIEF_APPROVED.value
            fields = dict(brief.fields_json)

            anchors = self._derive_anchors(session, row, fields)
            actions: list[dict[str, Any]] = []
            for anchor in anchors:
                payload = {
                    "anchor_key": anchor.anchor_key,
                    "prompt": _compose_anchor_prompt(anchor.kind, anchor.prompt_json),
                    "aspect_ratio": "1:1" if anchor.kind == "CHARACTER" else _aspect(fields),
                    "image_count": 1,
                }
                actions.append(
                    self._emit_action(
                        session,
                        row,
                        StructuredActionKind.GENERATE_KEY_VISUAL,
                        payload,
                        idempotency_key=(
                            f"creative:{session_id}:visual:{anchor.anchor_key}:r{revision}"
                        ),
                    )
                )
            row.status = CreativeSessionStatus.VISUALS_IN_PROGRESS.value
            session.flush()
            _ = actor
            return actions

    def _derive_anchors(
        self, session: Any, row: CreativeSession, fields: dict[str, Any]
    ) -> list[CreativeVisualAnchor]:
        """Anchors implied by the brief: characters, the setting, the style key.

        Character anchors also materialize project Character rows (reused by
        name) so the later compile binds the same canonical entities.
        """

        style = {
            "medium": get_path(fields, "visual_style.medium") or "cinematic live-action",
            "palette": get_path(fields, "visual_style.palette") or "",
            "tone": fields.get("tone") or [],
        }
        wanted: list[tuple[str, str, str, dict[str, Any]]] = []
        for character in (fields.get("characters") or [])[:3]:
            name = str(character.get("name", "")).strip()
            if not name:
                continue
            wanted.append(
                (
                    f"character:{name}",
                    "CHARACTER",
                    name,
                    {"subject": name, "look": character.get("look", ""), "style": style},
                )
            )
        location = get_path(fields, "setting.location")
        if location:
            wanted.append(
                (
                    f"scene:{location}",
                    "SCENE",
                    str(location),
                    {"subject": str(location), "time": get_path(fields, "setting.time"), "style": style},
                )
            )
        product = get_path(fields, "product.name")
        if product:
            wanted.append(
                (
                    f"product:{product}",
                    "PRODUCT",
                    str(product),
                    {
                        "subject": str(product),
                        "selling_points": get_path(fields, "product.selling_points") or [],
                        "style": style,
                    },
                )
            )
        wanted.append(
            (
                "style:master",
                "STYLE",
                "Style key plate",
                {"subject": str(fields.get("logline") or "")[:200], "style": style},
            )
        )

        existing = {
            anchor.anchor_key: anchor
            for anchor in session.scalars(
                select(CreativeVisualAnchor).where(CreativeVisualAnchor.session_id == row.id)
            )
        }
        anchors: list[CreativeVisualAnchor] = []
        for anchor_key, kind, title, prompt in wanted:
            anchor = existing.get(anchor_key)
            if anchor is None:
                anchor = CreativeVisualAnchor(
                    session_id=row.id,
                    anchor_key=anchor_key,
                    kind=kind,
                    title=title,
                    prompt_json=prompt,
                )
                if kind == "CHARACTER":
                    anchor.character_id = self._ensure_character(session, row.project_id, title)
                session.add(anchor)
                session.flush()
            anchors.append(anchor)
        return anchors

    @staticmethod
    def _ensure_character(session: Any, project_id: str, name: str) -> str:
        found = session.scalar(
            select(Character).where(
                Character.project_id == project_id, func.lower(Character.name) == name.lower()
            )
        )
        if found is not None:
            return found.id
        character = Character(project_id=project_id, name=name, status="DRAFT")
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
            return [_action_view(action) for action in session.scalars(query)]

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
                anchor = session.scalar(
                    select(CreativeVisualAnchor).where(
                        CreativeVisualAnchor.session_id == action.session_id,
                        CreativeVisualAnchor.anchor_key == action.payload_json.get("anchor_key"),
                    )
                )
                if anchor is not None:
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
            anchors = list(
                session.scalars(
                    select(CreativeVisualAnchor).where(
                        CreativeVisualAnchor.session_id == session_id
                    )
                )
            )
            for anchor in anchors:
                if anchor.status != CreativeAnchorStatus.GENERATING.value:
                    continue
                job = (
                    session.get(GenerationJob, anchor.generation_job_id)
                    if anchor.generation_job_id
                    else None
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
            summary = {
                "anchors": [_anchor_view(anchor) for anchor in anchors],
                "all_terminal": all(
                    anchor.status
                    in {CreativeAnchorStatus.READY.value, CreativeAnchorStatus.FAILED.value}
                    for anchor in anchors
                )
                and bool(anchors),
                "ready": sum(
                    1 for anchor in anchors if anchor.status == CreativeAnchorStatus.READY.value
                ),
            }
            _ = row
            return summary

    # ---------------------------------------------------------- visual bible
    def propose_bible(self, session_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = self._session(session, session_id)
            if row.status not in {
                CreativeSessionStatus.VISUALS_IN_PROGRESS.value,
                CreativeSessionStatus.BIBLE_PROPOSED.value,
            }:
                raise CreativeSessionConflict(f"bible cannot be proposed from {row.status}")
            brief = self._approved_brief(session, row)
            anchors = list(
                session.scalars(
                    select(CreativeVisualAnchor).where(
                        CreativeVisualAnchor.session_id == session_id
                    )
                )
            )
            fields = dict(brief.fields_json)
            content = {
                "logline": fields.get("logline"),
                "format": row.format,
                "style": fields.get("visual_style") or {},
                "tone": fields.get("tone") or [],
                "aspect_ratio": _aspect(fields),
                "anchors": [
                    {
                        "anchor_key": anchor.anchor_key,
                        "kind": anchor.kind,
                        "title": anchor.title,
                        "media_asset_id": anchor.media_asset_id,
                        "character_id": anchor.character_id,
                        "status": anchor.status,
                    }
                    for anchor in anchors
                ],
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
                content_json=content,
                content_hash=brief_hash(content),
            )
            session.add(bible)
            row.current_bible_version = version
            row.status = CreativeSessionStatus.BIBLE_PROPOSED.value
            session.flush()
            return _bible_view(bible)

    def approve_bible(self, session_id: str, *, version: int, actor: str) -> dict[str, Any]:
        """Lock one bible version. A locked version never changes again."""

        with self.database.session() as session:
            row = self._session(session, session_id)
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
                    f"bible version {version} is superseded; lock the current draft"
                )
            bible.status = "LOCKED"
            bible.locked_at = _now()
            bible.locked_by = actor
            row.status = CreativeSessionStatus.BIBLE_LOCKED.value
            session.flush()
            return _bible_view(bible)

    # ---------------------------------------------------------------- beats
    def propose_beats(self, session_id: str) -> list[dict[str, Any]]:
        with self.database.session() as session:
            row = self._session(session, session_id)
            if row.status not in {
                CreativeSessionStatus.BIBLE_LOCKED.value,
                CreativeSessionStatus.BEATS_PROPOSED.value,
            }:
                raise CreativeSessionConflict(
                    "beats require a locked visual bible; current status is " + row.status
                )
            brief = self._approved_brief(session, row)
            planned = self.beats.plan(dict(brief.fields_json), format_value=row.format)
            for stale_row in session.scalars(
                select(CreativeBeat).where(
                    CreativeBeat.session_id == session_id, CreativeBeat.status == "PROPOSED"
                )
            ):
                stale_row.status = "SUPERSEDED"
            plan_revision = row.current_beat_revision + 1
            for planned_beat in planned:
                session.add(
                    CreativeBeat(
                        session_id=session_id,
                        plan_revision=plan_revision,
                        sequence=planned_beat.sequence,
                        beat_json=planned_beat.as_json(),
                    )
                )
            row.current_beat_revision = plan_revision
            row.status = CreativeSessionStatus.BEATS_PROPOSED.value
            session.flush()
            return self._beats_view(session, session_id, plan_revision)

    def approve_beats(
        self,
        session_id: str,
        *,
        plan_revision: int,
        actor: str,
        episode_title: str | None = None,
        edited_beats: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Compile the approved plan into the existing production chain.

        Renders the beats to script text, creates the episode, and runs the
        very same narrative compiler and frame anchor planner every scripted
        episode uses - then applies the structured shot intents to the
        compiled Shot rows and records the ledger writes as actions.
        """

        if self.orchestrator is None:
            raise CreativeSessionConflict("no episode compiler is configured")
        with self.database.session() as session:
            row = self._session(session, session_id)
            if row.status == CreativeSessionStatus.COMPILED.value and row.compiled_episode_id:
                return self._compiled_view(session, row)
            if row.status != CreativeSessionStatus.BEATS_PROPOSED.value:
                raise CreativeSessionConflict(f"beats are not approvable from {row.status}")
            if plan_revision != row.current_beat_revision:
                raise CreativeSessionConflict(
                    f"beat plan {plan_revision} is superseded by {row.current_beat_revision}"
                )
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
            if edited_beats is not None:
                by_sequence = {int(beat.get("sequence", 0)): beat for beat in edited_beats}
                for beat_row in beat_rows:
                    edited = by_sequence.get(beat_row.sequence)
                    if edited is not None:
                        beat_row.beat_json = {**beat_row.beat_json, **edited}
            for beat_row in beat_rows:
                beat_row.status = "APPROVED"
            beats_json = [dict(beat_row.beat_json) for beat_row in beat_rows]
            project_id = row.project_id
            session.flush()

        script, ordered_intents = render_script(beats_json)
        with self.database.session() as session:
            from production_domain.models import Episode

            row = session.scalar(
                select(CreativeSession)
                .where(CreativeSession.id == session_id)
                .with_for_update()
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
                    {"episode_id": episode_id, "episode_number": episode_number},
                    idempotency_key=action_key,
                )
            elif existing_action.payload_json.get("episode_id") != episode_id:
                raise CreativeSessionConflict("episode action disagrees with the recorded episode")
            session.flush()

        result = self.orchestrator.compile_episode(episode_id)
        shot_ids = list(result.detail.get("shot_ids", []))
        self._apply_intents(shot_ids, ordered_intents)
        ledger_results = self._ledger_writes(
            session_id, project_id, episode_number, beats_json, plan_revision
        )

        with self.database.session() as session:
            row = self._session(session, session_id)
            self._emit_action(
                session,
                row,
                StructuredActionKind.COMPILE_EPISODE,
                {"episode_id": episode_id, "shot_ids": shot_ids},
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
            _ = actor
            view = self._compiled_view(session, row)
            view["ledger"] = ledger_results
            return view

    def _apply_intents(self, shot_ids: list[str], intents: list[dict[str, Any]]) -> None:
        from .beats import ShotIntentMismatch, apply_shot_intents

        try:
            apply_shot_intents(self.database, shot_ids, intents)
        except ShotIntentMismatch as exc:
            raise CreativeSessionConflict(str(exc)) from exc

    def _ledger_writes(
        self,
        session_id: str,
        project_id: str,
        episode_number: int,
        beats_json: list[dict[str, Any]],
        plan_revision: int,
    ) -> list[dict[str, Any]]:
        """Open the obligations the plan promises (cliffhanger, hook payoff)."""

        if self.ledger is None:
            return []
        results: list[dict[str, Any]] = []
        cliffhanger = next(
            (beat for beat in beats_json if beat.get("intent") == "CLIFFHANGER"), None
        )
        if cliffhanger is not None:
            obligation_key = f"creative:{session_id}:ep{episode_number}:cliffhanger"
            promise = str(cliffhanger.get("summary") or "resolve the episode cliffhanger")
            dialogue = next(
                (
                    shot.get("dialogue")
                    for shot in cliffhanger.get("shots", [])
                    if shot.get("dialogue")
                ),
                None,
            )
            if dialogue:
                promise = f"resolve: {dialogue}"
            # `open_obligation` is idempotent on the key: an identical confirm
            # replay returns the existing row without raising. A ValueError
            # here therefore means a *real* conflict — a revised plan changed
            # the cliffhanger promise under the same key — and it is reported
            # as one, never relabeled a replay.
            try:
                obligation_id = self.ledger.open_obligation(
                    project_id,
                    obligation_key=obligation_key,
                    promise=promise,
                    episode=episode_number,
                    category="CLIFFHANGER",
                )
            except ValueError as exc:
                results.append(
                    {
                        "kind": "OPEN_OBLIGATION",
                        "key": obligation_key,
                        "conflict": True,
                        "error": str(exc),
                    }
                )
                return results
            results.append({"kind": "OPEN_OBLIGATION", "id": obligation_id, "key": obligation_key})
            with self.database.session() as session:
                row = self._session(session, session_id)
                self._emit_action(
                    session,
                    row,
                    StructuredActionKind.OPEN_OBLIGATION,
                    {"obligation_key": obligation_key, "promise": promise},
                    idempotency_key=f"creative:{session_id}:obligation:r{plan_revision}",
                )
        return results

    # ---------------------------------------------------------------- reads
    def session_state(self, session_id: str) -> CreativeSessionState:
        with self.database.session() as session:
            row = self._session(session, session_id)
            brief = session.scalar(
                select(CreativeBriefRevision).where(
                    CreativeBriefRevision.session_id == session_id,
                    CreativeBriefRevision.revision == row.current_brief_revision,
                )
            )
            turns = [
                {
                    "sequence": turn.sequence,
                    "speaker": turn.speaker,
                    "content": turn.content,
                    "questions": turn.questions_json,
                    "reasoner": turn.reasoner,
                    "brief_revision": turn.brief_revision,
                }
                for turn in session.scalars(
                    select(CreativeTurn)
                    .where(CreativeTurn.session_id == session_id)
                    .order_by(CreativeTurn.sequence)
                )
            ]
            anchors = [
                _anchor_view(anchor)
                for anchor in session.scalars(
                    select(CreativeVisualAnchor)
                    .where(CreativeVisualAnchor.session_id == session_id)
                    .order_by(CreativeVisualAnchor.created_at)
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
            return CreativeSessionState(
                session={
                    "id": row.id,
                    "project_id": row.project_id,
                    "title": row.title,
                    "status": row.status,
                    "format": row.format,
                    "brief_revision": row.current_brief_revision,
                    "bible_version": row.current_bible_version,
                    "beat_revision": row.current_beat_revision,
                    "compiled_episode_id": row.compiled_episode_id,
                },
                brief=(
                    {
                        "revision": brief.revision,
                        "status": brief.status,
                        "fields": brief.fields_json,
                        "completeness": brief.completeness_json,
                    }
                    if brief is not None
                    else None
                ),
                turns=turns,
                anchors=anchors,
                bible=_bible_view(bible_row) if bible_row is not None else None,
                beats=beats,
                actions=actions,
            )

    def list_sessions(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.session() as session:
            return [
                {
                    "id": row.id,
                    "title": row.title,
                    "status": row.status,
                    "format": row.format,
                    "compiled_episode_id": row.compiled_episode_id,
                }
                for row in session.scalars(
                    select(CreativeSession)
                    .where(CreativeSession.project_id == project_id)
                    .order_by(CreativeSession.updated_at.desc())
                )
            ]

    # -------------------------------------------------------------- helpers
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
    def _asked_codes(session: Any, session_id: str) -> set[str]:
        codes: set[str] = set()
        for turn in session.scalars(
            select(CreativeTurn).where(
                CreativeTurn.session_id == session_id, CreativeTurn.speaker == "DIRECTOR"
            )
        ):
            for question in turn.questions_json or []:
                code = question.get("code")
                if code:
                    codes.add(str(code))
        return codes

    def _current_fields(self, session: Any, row: CreativeSession) -> dict[str, Any]:
        if row.current_brief_revision == 0:
            return {}
        brief = session.scalar(
            select(CreativeBriefRevision).where(
                CreativeBriefRevision.session_id == row.id,
                CreativeBriefRevision.revision == row.current_brief_revision,
            )
        )
        return dict(brief.fields_json) if brief is not None else {}

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
            raise CreativeSessionConflict("no approved brief on this session")
        return brief

    def _beats_view(self, session: Any, session_id: str, plan_revision: int) -> list[dict[str, Any]]:
        if plan_revision == 0:
            return []
        return [
            {"sequence": beat.sequence, "status": beat.status, **dict(beat.beat_json)}
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
            "shot_ids": (
                list(compile_action.payload_json.get("shot_ids", []))
                if compile_action is not None
                else []
            ),
        }


def _first_choice_json(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("chat response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("chat response is not a JSON object")


_ALLOWED_MODEL_PATHS = {
    "format",
    "logline",
    "duration_seconds",
    "platform",
    "aspect_ratio",
    "tone",
    "visual_style",
    "characters",
    "setting",
    "product",
    "hook",
    "call_to_action",
    "music",
    "audience",
}


def _sanitize_model_patch(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only known brief fields with sane types; a model invents nothing else."""

    patch: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in _ALLOWED_MODEL_PATHS:
            continue
        if key == "format":
            try:
                patch[key] = CreativeFormat(str(value)).value
            except ValueError:
                continue
        elif key == "duration_seconds":
            try:
                seconds = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= seconds <= 3600:
                patch[key] = seconds
        elif key in {"tone", "characters"} and isinstance(value, list):
            patch[key] = value[:8]
        elif key in {"visual_style", "setting", "product", "music"} and isinstance(value, dict):
            patch[key] = value
        elif isinstance(value, str) and value.strip():
            patch[key] = value.strip()[:500]
    return patch


def _set_default(fields: dict[str, Any], path: str, value: Any) -> None:
    from .brief import set_path

    set_path(fields, path, value)


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
        ]
        if term
    )
    subject = str(prompt_json.get("subject") or "").strip()
    if kind == "CHARACTER":
        look = str(prompt_json.get("look") or "").strip()
        base = f"Character reference of {subject}"
        if look:
            base += f", {look}"
        base += ", full body, neutral background, consistent identity"
    elif kind == "SCENE":
        time_value = str(prompt_json.get("time") or "").strip()
        base = f"Establishing view of {subject}"
        if time_value:
            base += f" at {time_value.lower()}"
        base += ", no people, location reference plate"
    elif kind == "PRODUCT":
        points = prompt_json.get("selling_points") or []
        base = f"Hero shot of {subject}"
        if points:
            base += ", " + ", ".join(str(point) for point in points[:3])
        base += ", clean studio backdrop"
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
    }


def _anchor_view(anchor: CreativeVisualAnchor) -> dict[str, Any]:
    return {
        "id": anchor.id,
        "anchor_key": anchor.anchor_key,
        "kind": anchor.kind,
        "title": anchor.title,
        "status": anchor.status,
        "generation_job_id": anchor.generation_job_id,
        "media_asset_id": anchor.media_asset_id,
        "character_id": anchor.character_id,
        "failure_code": anchor.failure_code,
    }


def _bible_view(bible: VisualBibleVersion) -> dict[str, Any]:
    return {
        "id": bible.id,
        "version": bible.version,
        "status": bible.status,
        "content": bible.content_json,
        "content_hash": bible.content_hash,
        "locked_at": bible.locked_at.isoformat() if bible.locked_at else None,
    }
