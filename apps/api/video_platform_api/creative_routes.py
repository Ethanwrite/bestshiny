"""Routes for the creative director and episode continuations.

The creative director emits structured actions; this module is where those
actions meet the platform. ``GENERATE_KEY_VISUAL`` runs through exactly the
same admission -> credit reservation -> visual runtime -> gateway path as
``POST /v1/images/generations`` - one paid boundary, no second engine - and
every execution outcome is written back onto the action row, so the audit of
what the director caused to happen is complete whether it succeeded or not.
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


class CreativeMessage(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


_APPROVAL_PHRASES = frozenset(
    {"批准", "同意", "确认", "通过", "批准吧", "approve", "approved", "lgtm", "approve it"}
)


class BriefApprove(BaseModel):
    revision: int = Field(ge=1)


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
                generation = GenerationRequest(
                    project_id=project_id,
                    type="image",
                    provider=named_provider,
                    model=named_model,
                    prompt=payload["prompt"],
                    aspect_ratio=payload.get("aspect_ratio", "1:1"),
                    image_count=int(payload.get("image_count", 1)),
                    idempotency_key=action["idempotency_key"],
                )
                admitted = container.generation_admission.admit_passenger(
                    generation,
                    enforce_plan=enforce_plan,
                )
                job, replayed = container.visual_runtime.submit(
                    admitted.request,
                    mode="PASSENGER_SEAT",
                    prompt_version="creative-key-visual-v1",
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
                    {"action_id": action["id"], "status": "FAILED", "error": str(exc)}
                )
                continue
            creative.record_action_result(
                action["id"],
                status="EXECUTED",
                result={
                    "job_id": job.id,
                    "replayed": replayed,
                    "estimated_credits": admitted.estimate.credits,
                },
            )
            results.append(
                {
                    "action_id": action["id"],
                    "status": "EXECUTED",
                    "job_id": job.id,
                    "estimated_credits": admitted.estimate.credits,
                }
            )
        return results

    # ------------------------------------------------------ creative director
    @app.post("/v1/creative/sessions", status_code=201)
    async def create_creative_session(
        body: CreativeSessionCreate,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        project = auth.require_project(principal, body.project_id, write=True)
        try:
            reply = await creative.start_session(
                body.project_id,
                idea=body.idea,
                workspace_id=project.workspace_id,
                format_hint=body.format,
                title=body.title,
            )
        except CreativeTurnLimitReached as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "session_id": reply.session_id,
            "status": reply.status,
            "message": reply.message,
            "questions": reply.questions,
            "brief_revision": reply.brief_revision,
            "proposable": reply.proposable,
            "reasoner": reply.reasoner,
        }

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
        state = creative.session_state(session_id)
        return {
            "session": state.session,
            "brief": state.brief,
            "turns": state.turns,
            "anchors": state.anchors,
            "bible": state.bible,
            "beats": state.beats,
            "actions": state.actions,
        }

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
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/creative/sessions/{session_id}/messages")
    async def post_creative_message(
        session_id: str,
        body: CreativeMessage,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        project_id = _require_session(principal, session_id, write=True)
        if _is_approval(body.content):
            # "批准" typed into the chat means the same thing as the Approve
            # button. Before this it was a new turn that re-proposed the same
            # brief with the same sentence (production, 2026-09-02).
            current = creative.session_state(session_id)
            brief = current.brief or {}
            if current.session.get("status") == "BRIEF_PROPOSED" and brief.get("revision"):
                revision = int(brief["revision"])
                try:
                    actions = creative.approve_brief(
                        session_id, revision=revision, actor=principal.user_id or "development"
                    )
                except CreativeSessionConflict as exc:
                    raise HTTPException(409, str(exc)) from exc
                executions = _execute_visual_actions(principal, session_id, project_id)
                return {
                    "status": "VISUALS_IN_PROGRESS",
                    "message": "Brief approved. The key visuals are being generated.",
                    "questions": [],
                    "brief_revision": revision,
                    "proposable": False,
                    "reasoner": "APPROVAL",
                    "approved_revision": revision,
                    "actions": actions,
                    "executions": executions,
                }
        try:
            reply = await creative.post_message(session_id, body.content)
        except CreativeTurnLimitReached as exc:
            raise HTTPException(403, str(exc)) from exc
        except CreativeSessionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {
            "status": reply.status,
            "message": reply.message,
            "questions": reply.questions,
            "brief_revision": reply.brief_revision,
            "proposable": reply.proposable,
            "reasoner": reply.reasoner,
        }

    @app.post("/v1/creative/sessions/{session_id}/brief/approve")
    def approve_creative_brief(
        session_id: str,
        body: BriefApprove,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        project_id = _require_session(principal, session_id, write=True)
        try:
            actions = creative.approve_brief(
                session_id, revision=body.revision, actor=principal.user_id or "development"
            )
        except CreativeSessionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        executions = _execute_visual_actions(principal, session_id, project_id)
        return {"approved_revision": body.revision, "actions": actions, "executions": executions}

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

    @app.post("/v1/creative/sessions/{session_id}/bible/propose")
    def propose_visual_bible(
        session_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        try:
            return creative.propose_bible(session_id)
        except CreativeSessionConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/v1/creative/sessions/{session_id}/bible/approve")
    def approve_visual_bible(
        session_id: str,
        body: BibleApprove,
        principal: AuthPrincipal = Depends(auth.current_user),
    ):
        _require_session(principal, session_id, write=True)
        try:
            return creative.approve_bible(
                session_id, version=body.version, actor=principal.user_id or "development"
            )
        except CreativeSessionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
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
            raise HTTPException(409, str(exc)) from exc

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
                actor=principal.user_id or "development",
                episode_title=body.episode_title,
                edited_beats=body.beats,
            )
        except CreativeSessionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

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
