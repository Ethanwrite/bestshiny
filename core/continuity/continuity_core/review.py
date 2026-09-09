"""The Continuity Skill's review of the handoff between two connected shots.

Between shot n and shot n+1 the previous approved end state and the next
start state form a contract. This stage asks the Continuity Skill, through
the runtime, whether that contract holds - ``PASS``, ``REPAIRABLE`` or
``ESCALATE`` - and records the verdict as a decision on the target shot with
the Skill version that produced it. When the model path is unavailable, a
deterministic field-by-field comparison of the two timeline states stands in,
and the record says so; it never redesigns a frame, rewrites an action or
promotes a rendered frame to canon.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from platform_contracts import ContinuityReview, continuity_authority_violations
from platform_database import Database
from production_domain.models import (
    DecisionRecord,
    Episode,
    Scene,
    Shot,
    TimelineState,
    TimelineTransition,
    TimelineTransitionType,
)
from skill_core.runtime import (
    SKILL_STAGE_DISABLED,
    AuthorityViolation,
    SkillInvocation,
    SkillOperation,
    SkillRuntime,
)
from sqlalchemy import select

logger = logging.getLogger(__name__)

CONTINUITY_PROTOCOL = """
## Reviewing one handoff (application protocol)

Compare the previous shot's approved end state with the next shot's start state under the declared
transition, and say whether the contract holds. Canonical assets are compared by id and version, never by
description.

Answer with ONE JSON object and nothing else, exactly this shape:
{
  "verdict": "PASS" | "REPAIRABLE" | "ESCALATE",
  "matched_state": [str],
  "mismatches": [{"field": str, "previous": str, "next": str, "severity": "MINOR" | "MAJOR" | "IDENTITY"}],
  "evidence": [str],
  "minimal_repair": str | null,   // the smallest repair that restores the contract without touching story
  "approval_required": bool,
  "unresolved": [str]
}

Rules:
- The transition decides what may carry: CONTINUOUS carries everything; SCENE_CUT and LOCATION_CHANGE reset
  spatial state but carry identity and committed physical state; TIME_JUMP, FLASH_FORWARD and MONTAGE need
  explicit reconciliation; FLASHBACK and DREAM are a separate branch; EXPLICIT_RESET breaks the contract on
  purpose.
- Never invent a transition, prop transfer, action or asset version to explain a mismatch away. An unshown
  pickup, turn, transfer or location change is a missing shot: ESCALATE it.
- Never redesign framing, movement, lighting or composition, never change an action or a line, never name a
  model or provider, never promote a frame to canon: a review carrying any such field is refused as a whole.
- Absent evidence is never a pass.
""".strip()

_BRANCH_TRANSITIONS = {TimelineTransitionType.FLASHBACK.value, TimelineTransitionType.DREAM.value}
_SPATIAL_RESET_TRANSITIONS = {
    TimelineTransitionType.SCENE_CUT.value,
    TimelineTransitionType.LOCATION_CHANGE.value,
}
_RECONCILE_TRANSITIONS = {
    TimelineTransitionType.TIME_JUMP.value,
    TimelineTransitionType.FLASH_FORWARD.value,
    TimelineTransitionType.MONTAGE.value,
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def deterministic_review(
    previous_end: dict[str, Any],
    next_start: dict[str, Any],
    *,
    transition: str,
    reconciliation_required: bool = False,
) -> ContinuityReview:
    """A field-by-field comparison of two timeline states under the transition's rules."""

    if transition in _BRANCH_TRANSITIONS:
        return ContinuityReview(
            verdict="PASS",
            evidence=[f"{transition}: a separate branch; the main timeline is not compared"],
        )
    if transition == TimelineTransitionType.EXPLICIT_RESET.value:
        return ContinuityReview(
            verdict="PASS",
            evidence=["EXPLICIT_RESET: the contract is deliberately broken by a declared transition"],
        )
    continuous = transition == TimelineTransitionType.CONTINUOUS.value
    spatial_carries = continuous
    matched: list[str] = []
    mismatches: list[dict[str, str]] = []

    def compare(field: str, before: Any, after: Any, severity: str) -> None:
        if _text(before) == _text(after):
            matched.append(field)
        else:
            mismatches.append(
                {
                    "field": field,
                    "previous": _text(before)[:400],
                    "next": _text(after)[:400],
                    "severity": severity,
                }
            )

    previous_scene = previous_end.get("scene") if isinstance(previous_end.get("scene"), dict) else {}
    next_scene = next_start.get("scene") if isinstance(next_start.get("scene"), dict) else {}
    if continuous:
        compare("scene.location", previous_scene.get("location"), next_scene.get("location"), "MAJOR")
        compare("scene.time", previous_scene.get("time"), next_scene.get("time"), "MAJOR")
    previous_characters = (
        previous_end.get("characters") if isinstance(previous_end.get("characters"), dict) else {}
    )
    next_characters = next_start.get("characters") if isinstance(next_start.get("characters"), dict) else {}
    for key in sorted(set(previous_characters) | set(next_characters)):
        before = previous_characters.get(key) if isinstance(previous_characters.get(key), dict) else None
        after = next_characters.get(key) if isinstance(next_characters.get(key), dict) else None
        if before is None or after is None:
            if continuous and before is not None and after is None:
                mismatches.append(
                    {
                        "field": f"characters.{key}.presence",
                        "previous": "present",
                        "next": "absent (exit not shown)",
                        "severity": "MAJOR",
                    }
                )
            continue
        compare(f"characters.{key}.costume", before.get("costume"), after.get("costume"), "MAJOR")
        if before.get("identity_version_id") or after.get("identity_version_id"):
            compare(
                f"characters.{key}.identity_version_id",
                before.get("identity_version_id"),
                after.get("identity_version_id"),
                "IDENTITY",
            )
        if spatial_carries:
            compare(f"characters.{key}.position", before.get("position"), after.get("position"), "MINOR")
            compare(
                f"characters.{key}.orientation", before.get("orientation"), after.get("orientation"), "MINOR"
            )
            compare(
                f"characters.{key}.gaze_target", before.get("gaze_target"), after.get("gaze_target"), "MINOR"
            )
    previous_held = previous_end.get("held_props") if isinstance(previous_end.get("held_props"), dict) else {}
    next_held = next_start.get("held_props") if isinstance(next_start.get("held_props"), dict) else {}
    if not transition or continuous or transition in _SPATIAL_RESET_TRANSITIONS:
        for key in sorted(set(previous_held) | set(next_held)):
            compare(f"held_props.{key}", previous_held.get(key), next_held.get(key), "MAJOR")
    if spatial_carries:
        previous_light = (
            previous_end.get("lighting") if isinstance(previous_end.get("lighting"), dict) else {}
        )
        next_light = next_start.get("lighting") if isinstance(next_start.get("lighting"), dict) else {}
        compare(
            "lighting.direction",
            previous_light.get("direction") or previous_light.get("continuity"),
            next_light.get("direction") or next_light.get("continuity"),
            "MINOR",
        )
        previous_camera = previous_end.get("camera") if isinstance(previous_end.get("camera"), dict) else {}
        next_camera = next_start.get("camera") if isinstance(next_start.get("camera"), dict) else {}
        compare("camera.axis", previous_camera.get("axis"), next_camera.get("axis"), "MINOR")
    severities = {item["severity"] for item in mismatches}
    verdict: Literal["PASS", "REPAIRABLE", "ESCALATE"]
    if "IDENTITY" in severities or "MAJOR" in severities:
        verdict = "ESCALATE"
    elif mismatches:
        verdict = "REPAIRABLE"
    else:
        verdict = "PASS"
    if transition in _RECONCILE_TRANSITIONS and reconciliation_required and mismatches:
        verdict = "ESCALATE"
    repair = None
    if verdict == "REPAIRABLE":
        repair = "; ".join(
            f"restore {item['field']} to {item['previous']}"
            for item in mismatches
            if item["severity"] == "MINOR"
        )[:600]
    return ContinuityReview(
        verdict=verdict,
        matched_state=matched[:40],
        mismatches=mismatches[:40],  # type: ignore[arg-type]
        evidence=[
            f"deterministic comparison of timeline states under {transition or 'UNRECORDED'} transition",
            *(["reconciliation required by the declared transition"] if reconciliation_required else []),
        ],
        minimal_repair=repair or None,
        approval_required=verdict == "ESCALATE",
        unresolved=(
            [
                "DETERMINISTIC REVIEW: the continuity model was unavailable; "
                "textual director states were not read"
            ]
        ),
    )


def validate_review(raw: dict[str, Any]) -> ContinuityReview:
    violations = continuity_authority_violations(raw)
    if violations:
        raise AuthorityViolation(violations)
    return ContinuityReview.model_validate(raw)


class ContinuityReviewer:
    """Run the Continuity Skill for one adjacent pair and record the verdict."""

    version = "continuity-reviewer-v1"
    decision_type = "CONTINUITY_REVIEW"

    def __init__(self, database: Database, runtime: SkillRuntime, *, enabled: bool = True):
        self.database = database
        self.runtime = runtime
        self.enabled = enabled

    # ---------------------------------------------------------------- context
    def pair_context(self, target_shot_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            target = session.get(Shot, target_shot_id)
            if target is None:
                raise LookupError("shot not found")
            scene = session.get(Scene, target.scene_id)
            episode = session.get(Episode, scene.episode_id) if scene else None
            if scene is None or episode is None:
                raise LookupError("shot has no scene or episode")
            source = session.get(Shot, target.previous_shot_id) if target.previous_shot_id else None
            transition = session.scalar(
                select(TimelineTransition).where(TimelineTransition.target_shot_id == target.id)
            )
            target_input = (
                session.get(TimelineState, target.input_state_id) if target.input_state_id else None
            )
            source_output = (
                session.get(TimelineState, source.output_state_id)
                if source is not None and source.output_state_id
                else None
            )
            same_scene = source is not None and source.scene_id == target.scene_id
            transition_type = (
                transition.transition_type
                if transition is not None
                else (
                    TimelineTransitionType.CONTINUOUS.value
                    if same_scene
                    else TimelineTransitionType.SCENE_CUT.value
                )
            )
            return {
                "project_id": episode.project_id,
                "from_shot": (
                    {
                        "id": source.id,
                        "sequence": source.sequence,
                        "action_line": source.user_prompt or source.prompt,
                        "director_end_state": (source.director_intent_json or {}).get("end_state", ""),
                        "director_gaze_target": (source.director_intent_json or {}).get("gaze_target", ""),
                        "continuity_obligations": list(
                            (source.director_intent_json or {}).get("continuity_obligations") or []
                        ),
                        "end_state": dict(source_output.state_json) if source_output else {},
                        "cinematography": (source.cinematography_json or {}).get("plan"),
                        "end_frame_asset_id": source.end_frame_asset_id,
                    }
                    if source is not None
                    else None
                ),
                "to_shot": {
                    "id": target.id,
                    "sequence": target.sequence,
                    "action_line": target.user_prompt or target.prompt,
                    "director_start_state": (target.director_intent_json or {}).get("start_state", ""),
                    "director_gaze_target": (target.director_intent_json or {}).get("gaze_target", ""),
                    "start_state": dict(target_input.state_json) if target_input else {},
                    "cinematography": (target.cinematography_json or {}).get("plan"),
                },
                "transition": {
                    "type": transition_type,
                    "declared": transition is not None,
                    "reconciliation_required": bool(transition.reconciliation_required)
                    if transition is not None
                    else False,
                    "same_scene": same_scene,
                },
                "canonical_bindings": {
                    "character_ids": sorted(
                        {
                            *(
                                (dict(source_output.state_json).get("characters") or {}).keys()
                                if source_output
                                else ()
                            ),
                            *(
                                (dict(target_input.state_json).get("characters") or {}).keys()
                                if target_input
                                else ()
                            ),
                        }
                    ),
                    "reference_asset_ids": [
                        str(item)
                        for item in ((target.director_intent_json or {}).get("reference_asset_ids") or [])
                    ],
                },
                "evidence": {
                    "previous_end_frame_registered": bool(source.end_frame_asset_id) if source else False,
                },
            }

    # ----------------------------------------------------------------- review
    async def review_pair(self, target_shot_id: str, *, model_roles: Any | None = None) -> dict[str, Any]:
        context = self.pair_context(target_shot_id)
        project_id = str(context["project_id"])
        transition = context["transition"]
        if context["from_shot"] is None:
            invocation = self.runtime.deterministic(
                SkillOperation.CONTINUITY_REVIEW, reason="NO_PREVIOUS_SHOT"
            )
            review = ContinuityReview(verdict="PASS", evidence=["first shot: there is no handoff to review"])
            return self._persist(context, review, invocation)
        if not self.enabled:
            invocation = self.runtime.deterministic(
                SkillOperation.CONTINUITY_REVIEW, reason=SKILL_STAGE_DISABLED
            )
        else:
            invocation = await self.runtime.invoke(
                SkillOperation.CONTINUITY_REVIEW,
                project_id=project_id,
                protocol=CONTINUITY_PROTOCOL,
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"task": "REVIEW_HANDOFF", **context}, ensure_ascii=False, default=str
                        ),
                    }
                ],
                validator=validate_review,
                model_roles=model_roles,
            )
        review = invocation.output
        if review is None:
            review = deterministic_review(
                context["from_shot"]["end_state"],
                context["to_shot"]["start_state"],
                transition=str(transition["type"]),
                reconciliation_required=bool(transition["reconciliation_required"]),
            )
        return self._persist(context, review, invocation)

    async def review_episode(
        self, episode_id: str, *, model_roles: Any | None = None
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            episode = session.get(Episode, episode_id)
            if episode is None:
                raise LookupError("episode not found")
            shot_ids = list(
                session.scalars(
                    select(Shot.id)
                    .join(Scene, Shot.scene_id == Scene.id)
                    .where(Scene.episode_id == episode_id)
                    .order_by(Scene.sequence, Shot.sequence)
                )
            )
        return [await self.review_pair(shot_id, model_roles=model_roles) for shot_id in shot_ids]

    # ---------------------------------------------------------------- persist
    def _persist(
        self, context: dict[str, Any], review: ContinuityReview, invocation: SkillInvocation
    ) -> dict[str, Any]:
        from_shot = context["from_shot"]
        with self.database.session() as session:
            decision = DecisionRecord(
                project_id=str(context["project_id"]),
                shot_id=str(context["to_shot"]["id"]),
                decision_type=self.decision_type,
                input_features={
                    "from_shot_id": from_shot["id"] if from_shot else None,
                    "transition": dict(context["transition"]),
                    "review": review.model_dump(mode="json"),
                    "skill_invocation": invocation.as_json(),
                },
                selected_action=review.verdict,
                reason_codes=[invocation.execution_mode, *invocation.reason_codes],
                model_version=(invocation.version if invocation.loaded else self.version) or self.version,
                policy_version=self.version,
            )
            session.add(decision)
            session.flush()
            return {
                "from_shot_id": from_shot["id"] if from_shot else None,
                "to_shot_id": str(context["to_shot"]["id"]),
                "transition": dict(context["transition"]),
                "review": review.model_dump(mode="json"),
                "skill_invocation": invocation.as_json(),
                "skill_driven": invocation.skill_driven,
                "execution_mode": invocation.execution_mode,
                "decision_record_id": decision.id,
            }
