"""Routes for the creative director and episode continuations.

The creative director emits structured actions; this module is where those
actions meet the platform. ``GENERATE_KEY_VISUAL`` runs through exactly the
same admission -> credit reservation -> visual runtime -> gateway path as
``POST /v1/images/generations`` - one paid boundary, no second engine - and
every execution outcome is written back onto the action row, so the audit of
what the director caused to happen is complete whether it succeeded or not.
Every approval constraint (a clarifying brief, an unanswered critical field,
an unconfirmed assumption, a deterministic screenplay, a key visual that is
not ready, an incomplete lock) is enforced by the service and surfaced here
as a 409 with a reason code - never by a hidden button.
"""

from __future__ import annotations

from typing import Any

from creative_director_core import (
    CreativeDirectorService,
    CreativeSessionConflict,
    CreativeTurnLimitReached,
)
from entitlement_core import (
    InsufficientWorkspaceCredits,
    PlanEntitlementDenied,
    ProductionBudgetExceeded,
    SpendAuthorizationDenied,
    WorkspaceCreditConflict,
)
from episode_continuation_core import EpisodeContinuationConflict, EpisodeContinuationService
from fastapi import Depends, FastAPI, HTTPException
from generation_gateway import GenerationTargetError, IdempotencyConflict
from model_registry_core import ModelRole
from platform_contracts import GenerationRequest
from production_domain.models import (
    CreativeSession,
    Episode,
    EpisodeContinuation,
    Project,
    Scene,
    Shot,
    ShotStatus,
)
from pydantic import BaseModel, Field
from sqlalchemy import select

from .auth import AuthPrincipal, AuthService
from .container import Container


class CreativeSessionCreate(BaseModel):
    project_id: str
    idea: str = Field(min_length=1, max_length=8000)
    format: str | None = None
    title: str = ""
    #: The idempotency key for the whole create, not only for its first turn.
    #: A retry carrying the key of a create that already landed returns that
    #: session's recorded opening reply instead of opening a second
    #: conversation; the same key with different words is a 409
    #: (CLIENT_TURN_ID_CONTENT_MISMATCH), never a replay of other words.
    client_turn_id: str | None = Field(default=None, max_length=120)


class CreativeMessage(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    client_turn_id: str | None = Field(default=None, max_length=120)
    #: The brief revision the client was looking at. When it is no longer the
    #: head, the turn is refused with BRIEF_REVISION_CHANGED instead of being
    #: re-based onto someone else's newer brief.
    expected_brief_revision: int | None = Field(default=None, ge=0)


_APPROVAL_PHRASES = frozenset(
    {"批准", "同意", "确认", "通过", "批准吧", "approve", "approved", "lgtm", "approve it"}
)


class BriefApprove(BaseModel):
    revision: int = Field(ge=1)
    accept_assumptions: bool = False


class BriefEdit(BaseModel):
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=40)


class QuestionResolve(BaseModel):
    code: str = Field(min_length=1, max_length=40)
    action: str = Field(pattern="^(ACCEPT_ASSUMPTION|SKIP)$")
    value: Any = None


class ScreenplayPropose(BaseModel):
    notes: str = Field(default="", max_length=4000)


class ScreenplayEdit(BaseModel):
    content: dict[str, Any]


class ScreenplayApprove(BaseModel):
    revision: int = Field(ge=1)
    accept_deterministic: bool = False
    #: Approve despite a recorded contradiction with the approved brief. The
    #: user is the only one who may overrule their own brief.
    accept_brief_violations: bool = False


class AnchorSkip(BaseModel):
    reason: str = Field(default="", max_length=240)


class AnchorRegenerate(BaseModel):
    direction: str = Field(default="", max_length=600)


class AnchorReplace(BaseModel):
    media_asset_id: str = Field(min_length=1, max_length=36)


class BibleApprove(BaseModel):
    version: int = Field(ge=1)


class BeatsApprove(BaseModel):
    plan_revision: int = Field(ge=1)
    episode_title: str | None = None
    beats: list[dict[str, Any]] | None = None


class ContinuationCreate(BaseModel):
    continuation_mode: str = Field(pattern="^(CONTINUOUS|TIME_JUMP|LOCATION_CHANGE)$")
    time_gap: str = ""
    new_location: str | None = None
    guidance: str = ""
    regenerate: bool = False


class ContinuationConfirm(BaseModel):
    title: str | None = None
    beats: list[dict[str, Any]] | None = None


def _conflict(exc: CreativeSessionConflict) -> HTTPException:
    return HTTPException(409, exc.as_detail())


def register_creative_routes(
    app: FastAPI,
    container: Container,
    auth: AuthService,
    *,
    creative: CreativeDirectorService,
    continuations: EpisodeContinuationService,
) -> None:
    def _is_approval(content: str) -> bool:
        """Only an unmistakable one-word approval; anything with conditions is a turn."""

        return content.strip().strip("。.!！ ").lower() in _APPROVAL_PHRASES

    def _actor(principal: AuthPrincipal) -> str:
        return principal.user_id or "development"

    def _actor_user_id(principal: AuthPrincipal) -> str | None:
        """A real user id, or None under the development bypass (no User row exists)."""

        return None if principal.development_bypass else principal.user_id

    def _require_session(principal: AuthPrincipal, session_id: str, *, write: bool = False) -> str:
        with container.database.session() as session:
            row = session.get(CreativeSession, session_id)
            if row is None:
                raise HTTPException(404, "creative session not found")
            project_id = row.project_id
        auth.require_project(principal, project_id, write=write)
        return project_id

    def _execute_visual_actions(
        principal: AuthPrincipal, session_id: str, project_id: str
    ) -> list[dict[str, Any]]:
        """Run every pending key-visual action through the Passenger image path.

        Actions are independent: one refusal (out of credits, plan denial,
        idempotency conflict) is recorded on its own row and the rest still
        run, so a retry endpoint can re-execute exactly the failures.
        """

        results: list[dict[str, Any]] = []
        pending = creative.pending_actions(
            session_id, kind="GENERATE_KEY_VISUAL", include_failed=True
        )
        with container.database.session() as session:
            project_workspace_id = session.get(Project, project_id).workspace_id
        enforce_plan = not principal.development_bypass and project_workspace_id is not None
        for action in pending:
            payload = action["payload"]
            try:
                named_provider = ""
                named_model = ""
                if not enforce_plan:
                    # Scoped workspaces let admission resolve the image target
                    # through the plan catalogue — naming one here would be
                    # refused, because image targets are router-owned. Legacy
                    # and development-bypass projects predate that contract
                    # and still need the explicit resolution.
                    try:
                        resolved = container.model_infrastructure.resolve_role(
                            ModelRole.IMAGE_GENERATION
                        )
                    except LookupError as exc:
                        raise GenerationTargetError(
                            "IMAGE_ROLE_UNRESOLVED", f"no image model is available: {exc}"
                        ) from exc
                    named_provider = resolved.provider
                    named_model = resolved.provider_model_id
                # A retry after an asynchronous failure must not reuse the key
                # that already burned a job: the gateway would replay the dead
                # one and rebind the anchor to it. Chaining the previous job's
                # id keeps the retry itself idempotent - pressing Retry twice
                # replays the same *new* job rather than paying twice - and
                # leaves CreativeAction.idempotency_key untouched, so the
                # action dedupe and its unique constraint still hold.
                result_json = action.get("result") or {}
                previous_job_id = str(result_json.get("job_id") or "")
                # Only a job that actually reached a terminal state is dead. An
                # action reopened for any other reason still owns its job, and
                # replaying the same key correctly returns it instead of paying
                # for a second one.
                burned_job = bool(result_json.get("failed_asynchronously")) and bool(previous_job_id)
                generation_key = (
                    f"{action['idempotency_key']}:after:{previous_job_id}"[:250]
                    if burned_job
                    else action["idempotency_key"]
                )
                generation = GenerationRequest(
                    project_id=project_id,
                    type="image",
                    provider=named_provider,
                    model=named_model,
                    prompt=payload["prompt"],
                    aspect_ratio=payload.get("aspect_ratio", "1:1"),
                    image_count=int(payload.get("image_count", 1)),
                    idempotency_key=generation_key,
                )
                admitted = container.generation_admission.admit_passenger(
                    generation,
                    enforce_plan=enforce_plan,
                )
                job, replayed = container.visual_runtime.submit(
                    admitted.request,
                    mode="PASSENGER_SEAT",
                    prompt_version="creative-key-visual-v2",
                    estimated_credits=admitted.estimate.credits,
                    pricing_version=container.credit_pricing.version,
                    quoted_cost_usd=admitted.estimate.estimated_total_usd,
                )
            except (
                IdempotencyConflict,
                GenerationTargetError,
                InsufficientWorkspaceCredits,
                PlanEntitlementDenied,
                ProductionBudgetExceeded,
                SpendAuthorizationDenied,
                WorkspaceCreditConflict,
                LookupError,
                ValueError,
            ) as exc:
                creative.record_action_result(
                    action["id"],
                    status="FAILED",
                    result={"error": str(exc), "error_type": type(exc).__name__},
                )
                results.append(
                    {
                        "action_id": action["id"],
                        "anchor_id": payload.get("anchor_id"),
                        "status": "FAILED",
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                )
                continue
            creative.record_action_result(
                action["id"],
                status="EXECUTED",
                result={
                    "job_id": job.id,
                    "replayed": replayed,
                    "estimated_credits": admitted.estimate.credits,
                    # Which attempt this is, and what the previous one was, so
                    # the audit and the UI can say "attempt 2 after <job>".
                    "attempt": int(result_json.get("attempt") or 0) + 1,
                    **({"previous_job_id": previous_job_id} if burned_job else {}),
                },
            )
            results.append(
                {
                    "action_id": action["id"],
                    "anchor_id": payload.get("anchor_id"),
                    "status": "EXECUTED",
                    "job_id": job.id,
                    "estimated_credits": admitted.estimate.credits,
                }
            )
        return results

    def _state_view(session_id: str) -> dict[str, Any]:
        state = creative.session_state(session_id)
        return {
            "session": state.session,
            "brief": state.brief,
            "turns": state.turns,
            "anchors": state.anchors,
            "bible": state.bible,
            "beats": state.beats,
            "actions": state.actions,
            "screenplay": state.screenplay,
            "screenplays": state.screenplays,
        }

    async def _draft_screenplay(session_id: str, principal: AuthPrincipal) -> dict[str, Any]:
        """Brief approval hands straight to the director's writing desk."""

        try:
            return {"screenplay": await creative.propose_screenplay(session_id, actor=_actor(principal))}
        except CreativeSessionConflict as exc:
            return {"screenplay": None, "screenplay_error": exc.as_detail()}

    # ------------------------------------------------------ creative director
    @app.post("/v1/creative/sessions", status_code=201)
    async def create_creative_session(
        body: CreativeSessionCreate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        project = auth.require_project(principal, body.project_id, write=True)
        # `client_turn_id` makes the first *turn* replayable, but until now
        # nothing guarded the session row: a create whose response was lost on
        # the way back opened a second CreativeSession on retry - a duplicate
        # conversation and a second paid director call - because the replay
        # guard only ever looks inside the session it was handed. The recorded
        # opening user turn is the idempotency record, so this needs no column
        # and no migration: if one already carries this key for this project,
        # the session exists, and the retry is answered from it.
        from production_domain.models import CreativeTurn

        replayed_session_id: str | None = None
        if body.client_turn_id:
            with container.database.session() as session:
                replayed_session_id = session.scalar(
                    select(CreativeTurn.session_id)
                    .join(CreativeSession, CreativeSession.id == CreativeTurn.session_id)
                    .where(
                        CreativeSession.project_id == body.project_id,
                        CreativeTurn.speaker == "USER",
                        CreativeTurn.sequence == 1,
                        CreativeTurn.client_turn_id == body.client_turn_id,
                    )
                    .order_by(CreativeTurn.session_id)
                    .limit(1)
                )
        try:
            if replayed_session_id:
                # Answer from the recorded turn through the same idempotency
                # path a retried message takes, so the same key sent with
                # different words is refused there rather than served someone
                # else's reply.
                reply = await creative.post_message(
                    replayed_session_id, body.idea, client_turn_id=body.client_turn_id
                )
            else:
                reply = await creative.start_session(
                    body.project_id,
                    idea=body.idea,
                    workspace_id=project.workspace_id,
                    format_hint=body.format,
                    title=body.title,
                    client_turn_id=body.client_turn_id,
                )
        except CreativeTurnLimitReached as exc:
            raise HTTPException(403, exc.as_detail()) from exc
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return reply.as_json()

    @app.get("/v1/creative/sessions")
    def list_creative_sessions(
        project_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        auth.require_project(principal, project_id)
        return creative.list_sessions(project_id)

    @app.get("/v1/creative/sessions/{session_id}")
    def get_creative_session(
        session_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id)
        return _state_view(session_id)

    @app.delete("/v1/creative/sessions/{session_id}")
    def delete_creative_session(
        session_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        """Retire a conversation. Rows stay (paid history); it leaves the list."""

        _require_session(principal, session_id, write=True)
        try:
            return creative.abandon_session(session_id)
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/creative/sessions/{session_id}/messages")
    async def post_creative_message(
        session_id: str,
        body: CreativeMessage,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        if _is_approval(body.content):
            # "批准" typed into the chat means the same thing as the Approve
            # button, under the same constraints: a blocked approval is
            # answered with what blocks it, not with a paid model turn.
            current = creative.session_state(session_id)
            brief = current.brief or {}
            if current.session.get("status") == "BRIEF_PROPOSED" and brief.get("revision"):
                revision = int(brief["revision"])
                try:
                    approved = creative.approve_brief(
                        session_id, revision=revision, actor=_actor(principal)
                    )
                except CreativeSessionConflict as exc:
                    detail = exc.as_detail()
                    return {
                        "session_id": session_id,
                        "status": current.session.get("status"),
                        "message": str(exc),
                        "questions": [],
                        "brief_revision": revision,
                        "proposable": True,
                        "reasoner": "APPROVAL_BLOCKED",
                        "reason_codes": [detail.get("reason_code") or "APPROVAL_BLOCKED"],
                        "assumptions": detail.get("assumptions") or brief.get("assumptions") or [],
                        "blocking": detail.get("blocking") or brief.get("blocking") or [],
                        "creative_notes": [],
                        "retryable": False,
                        "turn_sequence": 0,
                        "replayed": False,
                    }
                drafted = await _draft_screenplay(session_id, principal)
                return {
                    "session_id": session_id,
                    "status": creative.session_state(session_id).session.get("status"),
                    "message": "Brief approved. The director is writing the screenplay.",
                    "questions": [],
                    "brief_revision": approved["revision"],
                    "proposable": False,
                    "reasoner": "APPROVAL",
                    "reason_codes": ["BRIEF_APPROVED"],
                    "assumptions": [],
                    "blocking": [],
                    "creative_notes": [],
                    "retryable": False,
                    "turn_sequence": 0,
                    "replayed": False,
                    "approved_revision": approved["revision"],
                    **drafted,
                }
        try:
            reply = await creative.post_message(
                session_id,
                body.content,
                client_turn_id=body.client_turn_id,
                expected_brief_revision=body.expected_brief_revision,
            )
        except CreativeTurnLimitReached as exc:
            raise HTTPException(403, exc.as_detail()) from exc
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return reply.as_json()

    @app.post("/v1/creative/sessions/{session_id}/brief/edit")
    def edit_creative_brief(
        session_id: str,
        body: BriefEdit,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        try:
            return creative.edit_brief(session_id, body.operations, actor=_actor(principal))
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/creative/sessions/{session_id}/brief/questions")
    def resolve_creative_question(
        session_id: str,
        body: QuestionResolve,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        try:
            return creative.resolve_question(
                session_id, code=body.code, action=body.action, value=body.value, actor=_actor(principal)
            )
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/creative/sessions/{session_id}/brief/approve")
    async def approve_creative_brief(
        session_id: str,
        body: BriefApprove,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        try:
            approved = creative.approve_brief(
                session_id,
                revision=body.revision,
                actor=_actor(principal),
                accept_assumptions=body.accept_assumptions,
            )
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        drafted = await _draft_screenplay(session_id, principal)
        return {"approved_revision": approved["revision"], "brief": approved, **drafted}

    @app.post("/v1/creative/sessions/{session_id}/screenplay/propose")
    async def propose_creative_screenplay(
        session_id: str,
        body: ScreenplayPropose | None = None,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        try:
            return await creative.propose_screenplay(
                session_id, notes=(body.notes if body else ""), actor=_actor(principal)
            )
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/creative/sessions/{session_id}/screenplay/edit")
    def edit_creative_screenplay(
        session_id: str,
        body: ScreenplayEdit,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        try:
            return creative.edit_screenplay(session_id, body.content, actor=_actor(principal))
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/v1/creative/sessions/{session_id}/screenplay/approve")
    def approve_creative_screenplay(
        session_id: str,
        body: ScreenplayApprove,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        project_id = _require_session(principal, session_id, write=True)
        try:
            approved = creative.approve_screenplay(
                session_id,
                revision=body.revision,
                actor=_actor(principal),
                accept_deterministic=body.accept_deterministic,
                accept_brief_violations=body.accept_brief_violations,
            )
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        executions = _execute_visual_actions(principal, session_id, project_id)
        return {**approved, "executions": executions}

    @app.post("/v1/creative/sessions/{session_id}/visuals/execute")
    def execute_creative_visuals(
        session_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        project_id = _require_session(principal, session_id, write=True)
        return {"executions": _execute_visual_actions(principal, session_id, project_id)}

    @app.post("/v1/creative/sessions/{session_id}/visuals/sync")
    def sync_creative_visuals(
        session_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        return creative.sync_visuals(session_id)

    @app.post("/v1/creative/sessions/{session_id}/visuals/anchors/{anchor_id}/skip")
    def skip_creative_anchor(
        session_id: str,
        anchor_id: str,
        body: AnchorSkip | None = None,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        try:
            return creative.skip_anchor(
                session_id, anchor_id, reason=(body.reason if body else ""), actor=_actor(principal)
            )
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/creative/sessions/{session_id}/visuals/anchors/{anchor_id}/regenerate")
    def regenerate_creative_anchor(
        session_id: str,
        anchor_id: str,
        body: AnchorRegenerate | None = None,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        """A new version of one key visual with the user's direction, generated anew."""

        project_id = _require_session(principal, session_id, write=True)
        try:
            result = creative.regenerate_anchor(
                session_id, anchor_id, direction=(body.direction if body else ""), actor=_actor(principal)
            )
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        executions = _execute_visual_actions(principal, session_id, project_id)
        return {**result, "executions": executions}

    @app.post("/v1/creative/sessions/{session_id}/visuals/anchors/{anchor_id}/replace")
    def replace_creative_anchor(
        session_id: str,
        anchor_id: str,
        body: AnchorReplace,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        """Bind the user's own uploaded image as a new version of one key visual."""

        _require_session(principal, session_id, write=True)
        try:
            return creative.replace_anchor_image(
                session_id, anchor_id, media_asset_id=body.media_asset_id, actor=_actor(principal)
            )
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/creative/sessions/{session_id}/bible/propose")
    def propose_visual_bible(
        session_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        try:
            return creative.propose_bible(session_id)
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc

    @app.post("/v1/creative/sessions/{session_id}/bible/approve")
    def approve_visual_bible(
        session_id: str,
        body: BibleApprove,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        try:
            return creative.approve_bible(
                session_id,
                version=body.version,
                actor=_actor(principal),
                actor_user_id=_actor_user_id(principal),
            )
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/creative/sessions/{session_id}/beats/propose")
    def propose_creative_beats(
        session_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        try:
            return {"beats": creative.propose_beats(session_id)}
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc

    @app.post("/v1/creative/sessions/{session_id}/beats/approve")
    def approve_creative_beats(
        session_id: str,
        body: BeatsApprove,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        try:
            return creative.approve_beats(
                session_id,
                plan_revision=body.plan_revision,
                actor=_actor(principal),
                episode_title=body.episode_title,
                edited_beats=body.beats,
            )
        except CreativeSessionConflict as exc:
            raise _conflict(exc) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/v1/creative/shots/{shot_id}/lineage")
    def creative_shot_lineage(
        shot_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        try:
            lineage = creative.shot_lineage(shot_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        if lineage.get("project_id"):
            auth.require_project(principal, lineage["project_id"])
        return lineage

    # --------------------------------------------------- series and episodes
    @app.get("/v1/projects/{project_id}/episodes")
    def list_project_episodes(
        project_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        """The series strip: every episode with a rollup the UI can render.

        ``display_status`` derives COMPLETED from the shots (all committed)
        without touching the stored episode status, so nothing existing moves.
        """

        auth.require_project(principal, project_id)
        with container.database.session() as session:
            episodes = list(
                session.scalars(
                    select(Episode)
                    .where(Episode.project_id == project_id)
                    .order_by(Episode.episode_number)
                )
            )
            continuation_rows = list(
                session.scalars(
                    select(EpisodeContinuation).where(
                        EpisodeContinuation.project_id == project_id
                    )
                )
            )
            by_previous = {row.previous_episode_id: row for row in continuation_rows}
            views = []
            for episode in episodes:
                shots = list(
                    session.scalars(
                        select(Shot.status)
                        .join(Scene, Shot.scene_id == Scene.id)
                        .where(Scene.episode_id == episode.id)
                    )
                )
                committed = sum(1 for status in shots if status == ShotStatus.COMMITTED.value)
                if shots and committed == len(shots):
                    display_status = "COMPLETED"
                elif episode.status == "COMPILED":
                    display_status = "IN_PRODUCTION" if committed else "COMPILED"
                else:
                    display_status = episode.status
                continuation = by_previous.get(episode.id)
                views.append(
                    {
                        "id": episode.id,
                        "episode_number": episode.episode_number,
                        "title": episode.title,
                        "status": episode.status,
                        "display_status": display_status,
                        "shot_count": len(shots),
                        "committed_shot_count": committed,
                        "continuation": (
                            {
                                "id": continuation.id,
                                "status": continuation.status,
                                "next_episode_number": continuation.next_episode_number,
                                "continuation_mode": continuation.continuation_mode,
                            }
                            if continuation is not None
                            else None
                        ),
                    }
                )
            return views

    @app.post("/v1/episodes/{episode_id}/continuations", status_code=201)
    async def prepare_episode_continuation(
        episode_id: str,
        body: ContinuationCreate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        with container.database.session() as session:
            episode = session.get(Episode, episode_id)
            if episode is None:
                raise HTTPException(404, "episode not found")
            project_id = episode.project_id
        auth.require_project(principal, project_id, write=True)
        try:
            return await continuations.prepare(
                project_id,
                previous_episode_id=episode_id,
                continuation_mode=body.continuation_mode,
                time_gap=body.time_gap,
                new_location=body.new_location,
                guidance=body.guidance,
                regenerate=body.regenerate,
            )
        except EpisodeContinuationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/v1/continuations/{continuation_id}")
    def get_episode_continuation(
        continuation_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        try:
            view = continuations.get(continuation_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        auth.require_project(principal, view["project_id"])
        return view

    @app.post("/v1/continuations/{continuation_id}/confirm")
    def confirm_episode_continuation(
        continuation_id: str,
        body: ContinuationConfirm,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        try:
            view = continuations.get(continuation_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        auth.require_project(principal, view["project_id"], write=True)
        try:
            return continuations.confirm(
                continuation_id,
                actor=principal.user_id or "development",
                title=body.title,
                edited_beats=body.beats,
            )
        except EpisodeContinuationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
