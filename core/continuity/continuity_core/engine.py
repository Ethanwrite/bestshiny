from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from platform_database import Database
from production_domain.models import ContinuityMode, DecisionRecord


@dataclass(frozen=True)
class ContinuityRiskVector:
    camera_angle_delta: float = 0.0
    camera_axis_delta: float = 0.0
    shot_scale_delta: float = 0.0
    pose_delta: float = 0.0
    orientation_delta: float = 0.0
    character_visibility: float = 1.0
    face_visibility: float = 1.0
    occlusion: float = 0.0
    blocking_delta: float = 0.0
    scene_delta: float = 0.0
    timeline_delta: float = 0.0
    action_continuity: float = 1.0
    previous_frame_quality: float = 1.0
    identity_risk: float = 0.0
    same_scene: bool = True
    same_timeline: bool = True
    same_action_chain: bool = True
    reverse_shot: bool = False
    scene_change: bool = False
    timeline_jump: bool = False
    flashback: bool = False
    montage: bool = False
    explicit_reset: bool = False
    previous_end_frame_available: bool = True

    def normalized(self) -> ContinuityRiskVector:
        values = asdict(self)
        normalized: dict[str, Any] = {}
        for key, raw_value in values.items():
            if isinstance(raw_value, bool):
                normalized[key] = raw_value
                continue
            value = float(raw_value)
            if not math.isfinite(value):
                raise ValueError(f"continuity feature {key} must be finite")
            normalized[key] = max(0.0, min(1.0, value))
        return ContinuityRiskVector(**normalized)


@dataclass(frozen=True)
class ContinuityDecision:
    mode: str
    risk_score: float
    reasons: list[str]
    use_previous_end_frame: bool
    require_new_keyframe: bool
    required_context: tuple[str, ...] = ()


class ContinuityDecisionEngine:
    version = "continuity-rules-v2"

    weights = {
        "camera_angle_delta": 0.10,
        "camera_axis_delta": 0.16,
        "shot_scale_delta": 0.05,
        "pose_delta": 0.08,
        "orientation_delta": 0.08,
        "occlusion": 0.05,
        "blocking_delta": 0.08,
        "scene_delta": 0.14,
        "timeline_delta": 0.12,
        "identity_risk": 0.08,
    }

    def __init__(self, database: Database):
        self.database = database

    def decide(
        self,
        risk: ContinuityRiskVector,
        *,
        project_id: str | None = None,
        shot_id: str | None = None,
    ) -> ContinuityDecision:
        value = risk.normalized()
        data = asdict(value)
        score = sum(data[key] * weight for key, weight in self.weights.items())
        score += (1 - value.action_continuity) * 0.04
        score += (1 - value.previous_frame_quality) * 0.02
        score = round(min(1.0, score), 4)
        reasons: list[str] = []
        required_context: tuple[str, ...]
        if value.camera_axis_delta >= 0.65:
            reasons.append("CAMERA_AXIS_CHANGE")
        if value.reverse_shot:
            reasons.append("REVERSE_SHOT")
        if value.scene_change or not value.same_scene or value.scene_delta >= 0.5:
            reasons.append("SCENE_CHANGE")
        if value.timeline_jump or not value.same_timeline or value.timeline_delta >= 0.5:
            reasons.append("TIMELINE_JUMP")
        if value.flashback:
            reasons.append("FLASHBACK")
        if value.montage:
            reasons.append("MONTAGE")
        if value.explicit_reset:
            reasons.append("EXPLICIT_RESET")
        if value.previous_frame_quality < 0.45:
            reasons.append("LOW_PREVIOUS_FRAME_QUALITY")
        if not value.previous_end_frame_available:
            reasons.append("PREVIOUS_END_FRAME_UNAVAILABLE")
        if value.face_visibility < 0.3:
            reasons.append("LOW_PREVIOUS_FACE_VISIBILITY")
        if value.identity_risk >= 0.6:
            reasons.append("IDENTITY_DRIFT_RISK")
        if value.action_continuity < 0.4:
            reasons.append("ACTION_DISCONTINUITY")

        force_reanchor = bool(
            value.reverse_shot
            or value.camera_axis_delta >= 0.65
            or value.scene_change
            or not value.same_scene
            or value.scene_delta >= 0.5
            or value.timeline_jump
            or not value.same_timeline
            or value.timeline_delta >= 0.5
            or value.flashback
            or value.montage
            or value.explicit_reset
            or value.previous_frame_quality < 0.35
            or not value.previous_end_frame_available
            or value.identity_risk >= 0.75
        )
        hard_ok = bool(
            score <= 0.24
            and value.same_scene
            and value.same_timeline
            and value.same_action_chain
            and not value.reverse_shot
            and value.scene_delta < 0.1
            and value.timeline_delta < 0.1
            and value.camera_axis_delta < 0.25
            and value.action_continuity >= 0.7
            and value.previous_frame_quality >= 0.65
            and value.previous_end_frame_available
        )
        if force_reanchor:
            mode = ContinuityMode.RE_ANCHOR.value
            reasons = reasons or ["HIGH_CONTINUITY_RISK"]
            required_context = (
                "canonical_character_references",
                "matching_character_angle",
                "canonical_scene_reference",
                "narrative_state",
                "current_camera_prompt",
            )
        elif hard_ok:
            mode = ContinuityMode.HARD_CONTINUITY.value
            reasons = ["SAME_SCENE", "ACTION_CHAIN_CONTINUES", "USABLE_END_FRAME"]
            required_context = (
                "previous_end_frame",
                "character_binding",
                "current_action_prompt",
            )
        else:
            mode = ContinuityMode.HYBRID.value
            if not value.same_action_chain:
                reasons.append("ACTION_CHAIN_BOUNDARY")
            reasons = reasons or ["MODERATE_CAMERA_OR_BLOCKING_CHANGE"]
            required_context = (
                "character_master",
                "previous_end_frame_context",
                "current_camera_state",
                "narrative_state",
                "shot_prompt",
            )
        decision = ContinuityDecision(
            mode,
            score,
            list(dict.fromkeys(reasons)),
            use_previous_end_frame=mode != ContinuityMode.RE_ANCHOR.value,
            require_new_keyframe=mode == ContinuityMode.RE_ANCHOR.value,
            required_context=required_context,
        )
        if project_id or shot_id:
            with self.database.session() as session:
                session.add(
                    DecisionRecord(
                        project_id=project_id,
                        shot_id=shot_id,
                        decision_type="CONTINUITY_DECISION",
                        input_features=data,
                        selected_action=decision.mode,
                        reason_codes=decision.reasons,
                        model_version=self.version,
                        policy_version="continuity-v2",
                    )
                )
        return decision
