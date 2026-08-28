from __future__ import annotations

from dataclasses import dataclass

from character_core import CharacterIdentityService
from continuity_core import ContinuityDecisionEngine, ContinuityRiskVector, FrameAnchorPlanner
from generation_policy_core import AvailableGenerationAssets
from narrative_core import NarrativeCompiler
from production_domain.models import ContinuityMode, Episode, Scene, Shot
from skill_core import PromptCompilerService

from .pipeline import CandidatePipeline


@dataclass(frozen=True)
class OrchestrationResult:
    stage: str
    resource_id: str
    detail: dict


class AgentOrchestrator:
    """Central role sequence. Agents emit plans; only CandidatePipeline reaches GenerationGateway."""

    role_order = (
        "narrative_compiler",
        "director",
        "shot_planner",
        "assistant_director",
        "cinematographer",
        "camera",
        "lighting",
        "prompt_compiler",
        "frame_anchor_planner",
        "continuity_decision",
        "generation_policy",
        "provider_router",
        "qa_reviewer",
    )

    def __init__(
        self,
        narrative: NarrativeCompiler,
        characters: CharacterIdentityService,
        continuity: ContinuityDecisionEngine,
        prompts: PromptCompilerService,
        candidates: CandidatePipeline,
        frame_anchors: FrameAnchorPlanner | None = None,
    ):
        self.narrative = narrative
        self.characters = characters
        self.continuity = continuity
        self.prompts = prompts
        self.candidates = candidates
        self.frame_anchors = frame_anchors

    def compile_episode(self, episode_id: str) -> OrchestrationResult:
        result = self.narrative.compile_episode(episode_id)
        # Script compilation ends with a frame strategy for every adjacent
        # shot pair — the planner reads the transitions and dependencies the
        # compiler just wrote, so the two stay one operation for the caller.
        anchor_plans = (
            self.frame_anchors.plan_episode(episode_id) if self.frame_anchors is not None else []
        )
        return OrchestrationResult(
            "SHOT_PLANNING_COMPLETE",
            episode_id,
            {
                "scene_ids": result.scene_ids,
                "shot_ids": result.shot_ids,
                "entities": result.entities,
                "frame_anchor_plans": [plan.as_json() for plan in anchor_plans],
            },
        )

    def plan_frame_anchors(self, episode_id: str) -> OrchestrationResult:
        """Re-run the per-pair frame strategy without recompiling the script."""

        if self.frame_anchors is None:
            raise LookupError("frame anchor planner is not configured")
        plans = self.frame_anchors.plan_episode(episode_id)
        return OrchestrationResult(
            "FRAME_ANCHORS_PLANNED",
            episode_id,
            {"frame_anchor_plans": [plan.as_json() for plan in plans]},
        )

    def plan_continuity(
        self, shot_id: str, project_id: str, risk: ContinuityRiskVector
    ) -> OrchestrationResult:
        with self.candidates.database.session() as session:
            shot = session.get(Shot, shot_id)
            scene = session.get(Scene, shot.scene_id) if shot else None
            episode = session.get(Episode, scene.episode_id) if scene else None
            if shot is None or episode is None or episode.project_id != project_id:
                raise LookupError("shot does not belong to the continuity project")
        decision = self.continuity.decide(risk, project_id=project_id, shot_id=shot_id)
        if self.frame_anchors is not None:
            # A manual decision is a plan, not a bypass: registering it means
            # the generation preflight reuses it instead of overriding an
            # operator's judgement with a data-blind automatic one. The
            # planner's `_apply` performs the same shot writes the inline
            # branch below performs for planner-less deployments.
            self.frame_anchors.record_manual_decision(shot_id, decision)
        else:
            with self.candidates.database.session() as session:
                shot = session.get(Shot, shot_id)
                if shot is None:
                    raise LookupError("shot disappeared during continuity planning")
                shot.continuity_mode = decision.mode
                if decision.require_new_keyframe:
                    shot.start_frame_asset_id = None
                elif decision.mode == ContinuityMode.HARD_CONTINUITY.value and shot.previous_shot_id:
                    previous = session.get(Shot, shot.previous_shot_id)
                    if previous and previous.end_frame_asset_id:
                        shot.start_frame_asset_id = previous.end_frame_asset_id
                elif decision.mode == ContinuityMode.HYBRID.value:
                    # HYBRID may use the previous end frame as soft reference
                    # context, but it must never inherit it as a strong first
                    # frame.
                    shot.start_frame_asset_id = None
        return OrchestrationResult(
            "CONTINUITY_DECIDED",
            shot_id,
            {
                "mode": decision.mode,
                "risk_score": decision.risk_score,
                "reasons": decision.reasons,
                "required_context": list(decision.required_context),
                "use_previous_end_frame": decision.use_previous_end_frame,
                "require_new_keyframe": decision.require_new_keyframe,
            },
        )

    def plan_generation(
        self,
        shot_id: str,
        project_id: str,
        assets: AvailableGenerationAssets,
    ) -> OrchestrationResult:
        with self.candidates.database.session() as session:
            shot = session.get(Shot, shot_id)
            scene = session.get(Scene, shot.scene_id) if shot else None
            episode = session.get(Episode, scene.episode_id) if scene else None
            if shot is None or episode is None or episode.project_id != project_id:
                raise LookupError("shot does not belong to the generation project")
            continuity_mode = shot.continuity_mode
        decision = self.candidates.policy.decide(
            continuity_mode,
            assets,
            project_id=project_id,
            shot_id=shot_id,
        )
        with self.candidates.database.session() as session:
            shot = session.get(Shot, shot_id)
            if shot is None:
                raise LookupError("shot disappeared during generation planning")
            shot.generation_policy = decision.policy
            if decision.use_previous_end_frame_as_start:
                shot.start_frame_asset_id = assets.previous_end_frame_asset_id
            elif decision.require_new_keyframe:
                shot.start_frame_asset_id = None
        return OrchestrationResult(
            "GENERATION_POLICY_DECIDED",
            shot_id,
            {
                "policy": decision.policy,
                "required_inputs": list(decision.required_inputs),
                "reasons": list(decision.reason_codes),
                "require_new_keyframe": decision.require_new_keyframe,
            },
        )
