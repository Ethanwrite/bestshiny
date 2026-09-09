"""HOW WE SEE an approved shot: the Cinematography Skill, run per shot through the runtime.

The Shot Planner decided what happens; this stage decides how it is seen -
framing, angle, height, position, one dominant movement, lens intent, depth
of field, focus, motivated light, atmosphere, start and end composition - and
writes the validated plan onto the shot, where the prompt compiler reads it
between the timeline state and a caller's explicit overrides. The stage never
touches the action, the line, the states, the gaze target, the cast or the
canonical assets: a plan that carries any of those is refused as a whole and
the deterministic defaults stand in, on record.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from platform_contracts import (
    CinematographyPlan,
    approved_aspect_ratio,
    cinematography_authority_violations,
)
from platform_database import Database
from production_domain.models import DecisionRecord, Episode, Location, Scene, Shot, TimelineState
from skill_core.runtime import (
    EXECUTION_MODEL,
    SKILL_STAGE_DISABLED,
    AuthorityViolation,
    SkillInvocation,
    SkillOperation,
    SkillRuntime,
)
from sqlalchemy import select

logger = logging.getLogger(__name__)

CINEMATOGRAPHY_PROTOCOL = """
## Designing one shot (application protocol)

The shot below is approved: its dominant action, staging, line, start state, end state, gaze target, cast
and canonical assets are locked and are not yours to change. Design how it is seen.

Answer with ONE JSON object and nothing else, exactly this shape:
{
  "camera": {"framing": str, "angle": str, "height": str, "position": str,
             "dominant_movement": str, "speed": str, "path": str, "focus": str,
             "screen_axis": str, "lens_intent": str, "depth_of_field": str},
  "lighting": {"direction": str, "quality": str, "contrast": str, "color_temperature": str,
               "practicals": [str], "motivation": str, "exposure_intent": str},
  "subject_positions": {"<character name>": "screen left|centre|right, foreground|midground|background"},
  "atmosphere": str,
  "start_composition": str,   // the opening arrangement, not an action
  "end_composition": str,     // the closing arrangement, not a further action
  "continuity_checks": [str], // axis, eyeline and light-direction checks against the previous shot
  "unresolved": [str]
}

Rules:
- Exactly one dominant camera movement with its start, path, speed and end; "locked" is a choice.
- Every gaze stays on the approved gaze target; never redirect a gaze to the lens.
- Light is motivated by something in the scene; light direction is continuity across connected shots.
- Reference canonical characters, products and props by the names and ids given; never describe a face,
  a product or a wardrobe in your own words.
- Do not write an action, a line, a character fact, a product fact, a state, an ending, a model or a
  provider: a plan carrying any such field is refused as a whole.
- Concrete blocking, distance, light and material language only; no "cinematic", brands or resolution
  slogans.
""".strip()

_FRAMING_TO_SHOT_TYPE: tuple[tuple[tuple[str, ...], str], ...] = (
    (("extreme close", "ecu", "macro"), "EXTREME_CLOSE_UP"),
    (("over the shoulder", "over-the-shoulder", "ots"), "OVER_SHOULDER"),
    (("two shot", "two-shot"), "TWO_SHOT"),
    (("insert", "detail"), "INSERT"),
    (("close",), "CLOSE_UP"),
    (("wide", "establishing", "full shot", "long shot"), "WIDE"),
    (("medium",), "MEDIUM"),
)

_SHOT_TYPE_TO_FRAMING: dict[str, str] = {
    "WIDE": "wide",
    "MEDIUM": "medium",
    "CLOSE": "close-up",
    "CLOSE_UP": "close-up",
    "EXTREME_CLOSE_UP": "extreme close-up",
    "INSERT": "insert detail",
    "OVER_SHOULDER": "over-the-shoulder medium",
    "TWO_SHOT": "two shot",
    "DIALOGUE": "medium close-up",
}


def shot_type_from_framing(framing: str) -> str | None:
    """The coarse shot kind a framing phrase implies, or None when it names none."""

    lowered = " ".join(str(framing or "").lower().split())
    for tokens, shot_type in _FRAMING_TO_SHOT_TYPE:
        if any(token in lowered for token in tokens):
            return shot_type
    return None


def deterministic_plan(context: dict[str, Any], *, reason: str) -> CinematographyPlan:
    """The labelled degradation: the defaults the compiler has always used, on record."""

    shot_type = str(context.get("shot", {}).get("shot_type") or "MEDIUM").upper()
    return CinematographyPlan.model_validate(
        {
            "camera": {"framing": _SHOT_TYPE_TO_FRAMING.get(shot_type, "medium")},
            "lighting": {},
            "unresolved": [
                f"DETERMINISTIC DEFAULTS: the cinematography model was unavailable ({reason}); "
                "locked-off camera and preserved light stand in."
            ],
        }
    )


def validate_plan(raw: dict[str, Any]) -> CinematographyPlan:
    violations = cinematography_authority_violations(raw)
    if violations:
        raise AuthorityViolation(violations)
    return CinematographyPlan.model_validate(raw)


class StyleSource(Protocol):
    def generation_control(self, project_id: str) -> Any: ...


class CinematographyDesigner:
    """Run the Cinematography Skill for one shot and record its plan on the shot."""

    version = "cinematography-designer-v1"
    decision_type = "CINEMATOGRAPHY_DESIGN"

    def __init__(
        self,
        database: Database,
        runtime: SkillRuntime,
        *,
        styles: StyleSource | None = None,
        enabled: bool = True,
    ):
        self.database = database
        self.runtime = runtime
        self.styles = styles
        self.enabled = enabled

    # ---------------------------------------------------------------- context
    def shot_context(self, shot_id: str) -> dict[str, Any]:
        """Everything the Skill may read about one shot, canonical assets by reference."""

        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if shot is None:
                raise LookupError("shot not found")
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id) if scene else None
            if scene is None or episode is None:
                raise LookupError("shot has no scene or episode")
            location = session.get(Location, scene.location_id) if scene.location_id else None
            start = session.get(TimelineState, shot.input_state_id) if shot.input_state_id else None
            end = session.get(TimelineState, shot.output_state_id) if shot.output_state_id else None
            previous = session.get(Shot, shot.previous_shot_id) if shot.previous_shot_id else None
            director = dict(shot.director_intent_json or {})
            project = episode.project
            start_state = dict(start.state_json) if start else {}
            end_state = dict(end.state_json) if end else {}
            characters = (
                start_state.get("characters") if isinstance(start_state.get("characters"), dict) else {}
            )
            context = {
                "project_id": episode.project_id,
                "shot": {
                    "id": shot.id,
                    "sequence": shot.sequence,
                    "shot_type": shot.shot_type,
                    "duration": shot.duration,
                    "aspect_ratio": approved_aspect_ratio(director) or project.default_aspect_ratio,
                    "action_line": shot.user_prompt or shot.prompt,
                    "staging": director.get("description", ""),
                    "dialogue": director.get("dialogue") or end_state.get("dialogue") or "",
                    "start_state": director.get("start_state", ""),
                    "end_state": director.get("end_state", ""),
                    "gaze_target": director.get("gaze_target", ""),
                    "present_characters": list(director.get("present_characters") or []),
                    "identity_critical_characters": list(director.get("identity_critical_characters") or []),
                    "micro_actions": list(director.get("micro_actions") or []),
                    "continuity_obligations": list(director.get("continuity_obligations") or []),
                    "invariants": list(director.get("invariants") or []),
                    "prohibitions": list(director.get("prohibitions") or []),
                },
                "scene": {
                    "location": location.name if location else "",
                    "location_description": location.description if location else "",
                    "time": scene.time_context,
                    "description": scene.description,
                },
                "timeline": {
                    "start_state": {
                        key: start_state.get(key)
                        for key in ("scene", "characters", "props", "held_props", "lighting", "camera")
                        if key in start_state
                    },
                    "end_state": {
                        key: end_state.get(key)
                        for key in ("characters", "props", "held_props")
                        if key in end_state
                    },
                },
                "canonical_bindings": {
                    "character_ids": [str(key) for key in characters],
                    "reference_asset_ids": [
                        str(item) for item in (director.get("reference_asset_ids") or []) if item
                    ],
                    "anchors": list(director.get("anchors") or []),
                },
                "previous_shot": (
                    {
                        "id": previous.id,
                        "shot_type": previous.shot_type,
                        "action_line": previous.user_prompt or previous.prompt,
                        "cinematography": (previous.cinematography_json or {}).get("plan"),
                    }
                    if previous is not None
                    else None
                ),
            }
        if self.styles is not None:
            control = self.styles.generation_control(str(context["project_id"]))
            if control is not None:
                context["style_lock"] = dict(control.prompt_view())
        return context

    @staticmethod
    def input_hash(context: dict[str, Any]) -> str:
        encoded = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    # ----------------------------------------------------------------- design
    async def design_shot(self, shot_id: str, *, model_roles: Any | None = None) -> dict[str, Any]:
        """Design one shot; every outcome is written on the shot and as a decision record."""

        context = self.shot_context(shot_id)
        project_id = str(context["project_id"])
        digest = self.input_hash(context)
        if not self.enabled:
            invocation = self.runtime.deterministic(
                SkillOperation.CINEMATOGRAPHY_DESIGN, reason=SKILL_STAGE_DISABLED
            )
        else:
            invocation = await self.runtime.invoke(
                SkillOperation.CINEMATOGRAPHY_DESIGN,
                project_id=project_id,
                protocol=CINEMATOGRAPHY_PROTOCOL,
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"task": "DESIGN_SHOT", **context}, ensure_ascii=False, default=str
                        ),
                    }
                ],
                validator=validate_plan,
                model_roles=model_roles,
            )
        plan = invocation.output
        if plan is None:
            plan = deterministic_plan(context, reason=invocation.fallback_reason or "unavailable")
        return self._persist(shot_id, project_id, plan, invocation, digest)

    async def design_episode(
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
        return [await self.design_shot(shot_id, model_roles=model_roles) for shot_id in shot_ids]

    # ---------------------------------------------------------------- persist
    def _persist(
        self,
        shot_id: str,
        project_id: str,
        plan: CinematographyPlan,
        invocation: SkillInvocation,
        digest: str,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if shot is None:
                raise LookupError("shot disappeared during cinematography design")
            existing = dict(shot.cinematography_json or {})
            existing_invocation = existing.get("skill_invocation") or {}
            reused = False
            if (
                not invocation.skill_driven
                and existing.get("input_hash") == digest
                and existing_invocation.get("skill_driven")
            ):
                # A Skill-designed plan for exactly these inputs already
                # exists; a fallback does not overwrite it. The decision
                # record still says this run fell back.
                reused = True
                record = existing
            else:
                record = {
                    "version": self.version,
                    "plan": plan.model_dump(mode="json"),
                    "skill_invocation": invocation.as_json(),
                    "execution_mode": invocation.execution_mode,
                    "input_hash": digest,
                    "designed_at": datetime.now(UTC).isoformat(),
                }
                shot.cinematography_json = record
                if invocation.skill_driven and shot.shot_type != "DIALOGUE":
                    mapped = shot_type_from_framing(plan.camera.framing)
                    if mapped:
                        shot.shot_type = mapped
            decision = DecisionRecord(
                project_id=project_id,
                shot_id=shot_id,
                decision_type=self.decision_type,
                input_features={
                    "input_hash": digest,
                    "skill_invocation": invocation.as_json(),
                    "reused_existing_plan": reused,
                },
                selected_action=("REUSED_SKILL_PLAN" if reused else invocation.execution_mode),
                reason_codes=list(invocation.reason_codes),
                model_version=(invocation.version if invocation.loaded else self.version) or self.version,
                policy_version=self.version,
            )
            session.add(decision)
            session.flush()
            return {
                "shot_id": shot_id,
                "shot_type": shot.shot_type,
                "reused_existing_plan": reused,
                "skill_driven": bool(record.get("skill_invocation", {}).get("skill_driven"))
                and record.get("execution_mode", EXECUTION_MODEL) == EXECUTION_MODEL,
                **{key: value for key, value in record.items() if key != "version"},
                "decision_record_id": decision.id,
            }
