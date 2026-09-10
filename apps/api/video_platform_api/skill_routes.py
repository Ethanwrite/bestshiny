"""Routes for the Skill runtime: bindings, the integration matrix, and the per-shot stages.

Every route here reports or runs a Skill through the one runtime path, so
what it returns is what the audit rows carry: which Skill answered for an
operation, which version, whether its body was injected, whether a model was
invoked, and why a deterministic fallback stood in when it did.
"""

from __future__ import annotations

from typing import Any

from continuity_core import ContinuityAcknowledgementConflict
from fastapi import Depends, FastAPI, HTTPException
from production_domain.models import Episode, Scene, Shot
from pydantic import BaseModel, Field
from skill_core import ContinuityApprovalRequired

from .auth import AuthPrincipal, AuthService
from .container import Container


class StageRun(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)


class ContinuityAcknowledge(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)
    #: The CONTINUITY_REVIEW decision being approved: the one standing on the
    #: shot, named so the approval is of that verdict and no other.
    decision_id: str = Field(min_length=1, max_length=36)
    note: str = Field(default="", max_length=600)


def register_skill_routes(app: FastAPI, container: Container, auth: AuthService) -> None:
    def _shot_project(shot_id: str) -> tuple[str, str]:
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            if shot is None:
                raise HTTPException(404, "shot not found")
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id) if scene else None
            if episode is None:
                raise HTTPException(404, "shot has no episode")
            return episode.project_id, episode.id

    def _require_shot(shot_id: str, project_id: str, principal: AuthPrincipal) -> None:
        auth.require_project(principal, project_id, write=True)
        owner, _episode = _shot_project(shot_id)
        if owner != project_id:
            raise HTTPException(409, "shot does not belong to project")

    @app.get("/v1/skills/runtime")
    def skill_runtime_matrix(_principal: AuthPrincipal = Depends(auth.current_user)) -> dict[str, Any]:
        """Skill | Registered | Runtime Bound | Body Injected | Structured Validated | Fallback Tracked."""

        runtime = container.skill_runtime
        return {
            "version": runtime.version,
            "problems": runtime.validate(strict_sections=True),
            "bindings": runtime.bindings(),
            "matrix": runtime.integration_matrix(),
        }

    @app.post("/v1/shots/{shot_id}/cinematography")
    async def design_shot_cinematography(
        shot_id: str,
        body: StageRun,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        _require_shot(shot_id, body.project_id, principal)
        try:
            return await container.cinematography_designer.design_shot(shot_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/v1/shots/{shot_id}/cinematography")
    def get_shot_cinematography(
        shot_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        project_id, _episode = _shot_project(shot_id)
        auth.require_project(principal, project_id)
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            assert shot is not None
            return {"shot_id": shot_id, "shot_type": shot.shot_type, **dict(shot.cinematography_json or {})}

    @app.post("/v1/shots/{shot_id}/continuity/review")
    async def review_shot_continuity(
        shot_id: str,
        body: StageRun,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        _require_shot(shot_id, body.project_id, principal)
        try:
            return await container.continuity_reviewer.review_pair(shot_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/v1/shots/{shot_id}/continuity/review")
    def get_shot_continuity_review(
        shot_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        """The latest review, the escalation the shot is blocked on (if any), and its acknowledgements."""

        project_id, _episode = _shot_project(shot_id)
        auth.require_project(principal, project_id)
        return container.continuity_reviewer.gate_view(shot_id)

    @app.post("/v1/shots/{shot_id}/continuity/review/acknowledge")
    def acknowledge_shot_continuity(
        shot_id: str,
        body: ContinuityAcknowledge,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        """A human approves the handoff the Continuity Skill escalated.

        The approval is traceable to a real user: the development bypass may
        not release a Skill's escalation, as it may not confirm a character
        state change.
        """

        _require_shot(shot_id, body.project_id, principal)
        if principal.development_bypass:
            raise HTTPException(403, "连续性升级的确认必须由真实登录用户完成")
        try:
            return container.continuity_reviewer.acknowledge(
                shot_id,
                decision_id=body.decision_id,
                actor=principal.user_id or "unknown",
                actor_user_id=principal.user_id,
                note=body.note,
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ContinuityAcknowledgementConflict as exc:
            raise HTTPException(409, exc.as_detail()) from exc

    @app.post("/v1/shots/{shot_id}/prompt/compile")
    async def compile_shot_prompt(
        shot_id: str,
        body: StageRun,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        _require_shot(shot_id, body.project_id, principal)
        try:
            result = await container.prompts.compile_shot_with_skill(shot_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ContinuityApprovalRequired as exc:
            raise HTTPException(409, exc.as_detail()) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "shot_id": shot_id,
            "record_id": result.record_id,
            "status": result.output.status,
            "execution_mode": result.execution_mode,
            "skill_driven": result.skill_driven,
            "skill_name": result.skill_name,
            "skill_version": result.skill_version,
            "skill_invocation": result.skill_invocation,
            "output": result.output.model_dump(mode="json"),
        }

    @app.post("/v1/episodes/{episode_id}/skill-stages")
    async def run_episode_skill_stages(
        episode_id: str,
        body: StageRun,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        auth.require_project(principal, body.project_id, write=True)
        with container.database.session() as session:
            episode = session.get(Episode, episode_id)
            if episode is None:
                raise HTTPException(404, "episode not found")
            if episode.project_id != body.project_id:
                raise HTTPException(409, "episode does not belong to project")
        return await container.visual_stages.run_episode(episode_id)
