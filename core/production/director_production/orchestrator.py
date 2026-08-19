from __future__ import annotations

from dataclasses import dataclass

from character_core import CharacterIdentityService
from continuity_core import ContinuityDecisionEngine, ContinuityRiskVector
from narrative_core import NarrativeCompiler
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
    ):
        self.narrative = narrative
        self.characters = characters
        self.continuity = continuity
        self.prompts = prompts
        self.candidates = candidates

    def compile_episode(self, episode_id: str) -> OrchestrationResult:
        result = self.narrative.compile_episode(episode_id)
        return OrchestrationResult(
            "SHOT_PLANNING_COMPLETE",
            episode_id,
            {"scene_ids": result.scene_ids, "shot_ids": result.shot_ids, "entities": result.entities},
        )

    def plan_continuity(
        self, shot_id: str, project_id: str, risk: ContinuityRiskVector
    ) -> OrchestrationResult:
        decision = self.continuity.decide(risk, project_id=project_id, shot_id=shot_id)
        return OrchestrationResult(
            "CONTINUITY_DECIDED",
            shot_id,
            {"mode": decision.mode, "risk_score": decision.risk_score, "reasons": decision.reasons},
        )
