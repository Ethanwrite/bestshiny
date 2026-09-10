"""The visual stages of a compiled episode, run in order through the Skill runtime.

After the creative director compiles an episode - shots exist, states are
chained, intents are applied - three Skill-bound stages run over it:
cinematography design per shot, the continuity review per adjacent pair, and
the prompt compilation per shot. Each stage records its own invocation and
falls back on its own; a stage that cannot run never blocks the compile, it
is recorded as fallen back and the generation path compiles deterministically
until the stage is run again.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from platform_database import Database
from production_domain.models import Episode, Scene, Shot
from sqlalchemy import select

logger = logging.getLogger(__name__)


class ShotDesigner(Protocol):
    async def design_shot(self, shot_id: str, *, model_roles: Any | None = None) -> dict[str, Any]: ...


class PairReviewer(Protocol):
    async def review_pair(self, target_shot_id: str, *, model_roles: Any | None = None) -> dict[str, Any]: ...


class SkillCompiler(Protocol):
    async def compile_shot_with_skill(self, shot_id: str, *, model_roles: Any | None = None) -> Any: ...


class VisualStageRunner:
    """Cinematography -> continuity -> prompt compilation, per compiled episode."""

    version = "visual-stage-runner-v1"

    def __init__(
        self,
        database: Database,
        *,
        cinematography: ShotDesigner,
        continuity: PairReviewer,
        compiler: SkillCompiler,
        enabled: bool = True,
    ):
        self.database = database
        self.cinematography = cinematography
        self.continuity = continuity
        self.compiler = compiler
        self.enabled = enabled

    def _shot_ids(self, episode_id: str) -> list[str]:
        with self.database.session() as session:
            if session.get(Episode, episode_id) is None:
                raise LookupError("episode not found")
            return list(
                session.scalars(
                    select(Shot.id)
                    .join(Scene, Shot.scene_id == Scene.id)
                    .where(Scene.episode_id == episode_id)
                    .order_by(Scene.sequence, Shot.sequence)
                )
            )

    async def run_episode(self, episode_id: str, *, model_roles: Any | None = None) -> dict[str, Any]:
        """Run every stage over every shot; report per stage what was Skill-driven."""

        shot_ids = self._shot_ids(episode_id)
        report: dict[str, Any] = {
            "episode_id": episode_id,
            "enabled": self.enabled,
            "shots": len(shot_ids),
            "cinematography": [],
            "continuity": [],
            "prompt_compilation": [],
            # Handoffs the Continuity Skill escalated: their shots are not
            # compiled below (the compiler records the refusal) and cannot be
            # generated until a human acknowledges the decision or a re-run
            # of the review clears it.
            "approval_required": [],
            "errors": [],
        }
        if not self.enabled:
            report["skipped_reason"] = "SKILL_STAGES_DISABLED"
            return report
        for shot_id in shot_ids:
            try:
                designed = await self.cinematography.design_shot(shot_id, model_roles=model_roles)
                report["cinematography"].append(
                    {
                        "shot_id": shot_id,
                        "skill_driven": bool(designed.get("skill_driven")),
                        "execution_mode": designed.get("execution_mode"),
                        "fallback_reason": (designed.get("skill_invocation") or {}).get("fallback_reason"),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one shot's failure never hides the others'
                logger.warning("cinematography design failed for %s: %s", shot_id, exc, exc_info=True)
                report["errors"].append(
                    {"stage": "cinematography", "shot_id": shot_id, "error": str(exc)[:300]}
                )
        for shot_id in shot_ids:
            try:
                reviewed = await self.continuity.review_pair(shot_id, model_roles=model_roles)
                report["continuity"].append(
                    {
                        "to_shot_id": shot_id,
                        "from_shot_id": reviewed.get("from_shot_id"),
                        "verdict": (reviewed.get("review") or {}).get("verdict"),
                        "approval_required": bool(reviewed.get("approval_required")),
                        "skill_driven": bool(reviewed.get("skill_driven")),
                        "execution_mode": reviewed.get("execution_mode"),
                        "decision_record_id": reviewed.get("decision_record_id"),
                    }
                )
                if reviewed.get("approval_required") and reviewed.get("skill_driven"):
                    report["approval_required"].append(
                        {
                            "shot_id": shot_id,
                            "from_shot_id": reviewed.get("from_shot_id"),
                            "decision_id": reviewed.get("decision_record_id"),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("continuity review failed for %s: %s", shot_id, exc, exc_info=True)
                report["errors"].append({"stage": "continuity", "shot_id": shot_id, "error": str(exc)[:300]})
        for shot_id in shot_ids:
            try:
                compiled = await self.compiler.compile_shot_with_skill(shot_id, model_roles=model_roles)
                report["prompt_compilation"].append(
                    {
                        "shot_id": shot_id,
                        "status": compiled.output.status,
                        "skill_driven": bool(compiled.skill_driven),
                        "execution_mode": compiled.execution_mode,
                        "fallback_reason": (compiled.skill_invocation or {}).get("fallback_reason"),
                        "record_id": compiled.record_id,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("skill prompt compilation failed for %s: %s", shot_id, exc, exc_info=True)
                report["errors"].append(
                    {"stage": "prompt_compilation", "shot_id": shot_id, "error": str(exc)[:300]}
                )
        report["summary"] = {
            stage: {
                "total": len(report[stage]),
                "skill_driven": sum(1 for item in report[stage] if item.get("skill_driven")),
            }
            for stage in ("cinematography", "continuity", "prompt_compilation")
        }
        return report
