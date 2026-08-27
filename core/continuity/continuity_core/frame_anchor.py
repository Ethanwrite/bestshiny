"""The Shot Transition / Frame Anchor Planner.

Between every two adjacent shots there is exactly one frame-strategy decision:
inherit the previous shot's last frame, use it only as soft context, or
reconstruct the first frame from canonical references — and, when
reconstructing, *which* characters and scene anchor it. That decision used to
live implicitly in whoever set ``Shot.continuity_mode`` (the API accepted a
caller-supplied risk vector); this planner derives the risk vector from the
structured rows that already exist — ``TimelineTransition``, the authoritative
timeline states, the declared ``STATE_INHERITANCE`` dependencies — and feeds
the existing :class:`ContinuityDecisionEngine`. Nothing downstream changes:
``GenerationPolicyEngine`` keeps translating the chosen mode into a policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from platform_database import Database
from production_domain.models import (
    Asset,
    AssetKind,
    AssetVersion,
    Character,
    CharacterIdentityVersion,
    ContinuityMode,
    DecisionRecord,
    Episode,
    Scene,
    Shot,
    ShotDependency,
    ShotDependencyType,
    ShotStatus,
    TimelineState,
    TimelineTransition,
    TimelineTransitionType,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from .engine import ContinuityDecisionEngine, ContinuityRiskVector


class FrameAnchorStrategy:
    """The three frame strategies a shot pair can resolve to."""

    INHERIT_LAST_FRAME = "INHERIT_LAST_FRAME"
    HYBRID_CONTEXT = "HYBRID_CONTEXT"
    RECONSTRUCT_FIRST_FRAME = "RECONSTRUCT_FIRST_FRAME"


_MODE_TO_STRATEGY = {
    ContinuityMode.HARD_CONTINUITY.value: FrameAnchorStrategy.INHERIT_LAST_FRAME,
    ContinuityMode.HYBRID.value: FrameAnchorStrategy.HYBRID_CONTEXT,
    ContinuityMode.RE_ANCHOR.value: FrameAnchorStrategy.RECONSTRUCT_FIRST_FRAME,
}

@dataclass(frozen=True)
class AnchorSubject:
    """One character a reconstructed first frame must anchor."""

    character_id: str
    name: str
    identity_version_id: str | None
    master_asset_id: str | None


@dataclass(frozen=True)
class FrameAnchorPlan:
    target_shot_id: str
    source_shot_id: str | None
    strategy: str
    continuity_mode: str
    transition_type: str
    risk_score: float
    reasons: tuple[str, ...]
    #: Characters a reconstructed/hybrid first frame must carry, resolved from
    #: the target shot's own input state — the people visible when the shot
    #: opens, not whoever the previous frame happened to hold.
    anchor_subjects: tuple[AnchorSubject, ...] = ()
    scene_asset_id: str | None = None
    requires_keyframe_generation: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "planner_version": FrameAnchorPlanner.version,
            "target_shot_id": self.target_shot_id,
            "source_shot_id": self.source_shot_id,
            "strategy": self.strategy,
            "continuity_mode": self.continuity_mode,
            "transition_type": self.transition_type,
            "risk_score": self.risk_score,
            "reasons": list(self.reasons),
            "anchor_subjects": [
                {
                    "character_id": subject.character_id,
                    "name": subject.name,
                    "identity_version_id": subject.identity_version_id,
                    "master_asset_id": subject.master_asset_id,
                }
                for subject in self.anchor_subjects
            ],
            "scene_asset_id": self.scene_asset_id,
            "requires_keyframe_generation": self.requires_keyframe_generation,
        }


@dataclass
class _PairFacts:
    """Everything the risk derivation needs, read in one session."""

    project_id: str
    target: Shot
    source: Shot | None
    transition_type: str | None
    source_output_state: dict[str, Any] = field(default_factory=dict)
    target_input_state: dict[str, Any] = field(default_factory=dict)
    target_output_state: dict[str, Any] = field(default_factory=dict)
    explicit_state_inheritance: bool = False
    same_scene: bool = True
    source_failed: bool = False

    def subject_state(self) -> dict[str, Any]:
        """Who the shot must carry: visible at open *or* acting within it.

        The narrative compiler only adds an actor to the shot's output state,
        so a first frame anchored on the input state alone would omit the very
        character the shot exists to show.
        """

        input_characters = self.target_input_state.get("characters")
        output_characters = self.target_output_state.get("characters")
        merged: dict[str, Any] = {}
        if isinstance(input_characters, dict):
            merged.update(input_characters)
        if isinstance(output_characters, dict):
            merged.update(output_characters)
        return {"characters": merged}


def _state_characters(state: dict[str, Any]) -> set[str]:
    characters = state.get("characters")
    return set(characters.keys()) if isinstance(characters, dict) else set()


def _state_camera(state: dict[str, Any]) -> dict[str, Any]:
    camera = state.get("camera")
    return camera if isinstance(camera, dict) else {}


class FrameAnchorPlanner:
    """Decide the frame strategy for every adjacent shot pair, from rows."""

    version = "frame-anchor-planner-v1"

    def __init__(self, database: Database, decisions: ContinuityDecisionEngine):
        self.database = database
        self.decisions = decisions

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _project_id(session: Session, shot: Shot) -> str:
        scene = session.get(Scene, shot.scene_id)
        episode = session.get(Episode, scene.episode_id) if scene else None
        if episode is None:
            raise LookupError("shot project could not be resolved")
        return episode.project_id

    def _pair_facts(self, session: Session, target: Shot) -> _PairFacts:
        project_id = self._project_id(session, target)
        source = session.get(Shot, target.previous_shot_id) if target.previous_shot_id else None
        transition = session.scalar(
            select(TimelineTransition).where(TimelineTransition.target_shot_id == target.id)
        )
        facts = _PairFacts(
            project_id=project_id,
            target=target,
            source=source,
            transition_type=transition.transition_type if transition else None,
        )
        target_input = (
            session.get(TimelineState, target.input_state_id) if target.input_state_id else None
        )
        target_output = (
            session.get(TimelineState, target.output_state_id) if target.output_state_id else None
        )
        facts.target_input_state = dict(target_input.state_json) if target_input else {}
        facts.target_output_state = dict(target_output.state_json) if target_output else {}
        if source is None:
            return facts
        facts.same_scene = source.scene_id == target.scene_id
        facts.source_failed = source.status == ShotStatus.FAILED.value
        source_output = (
            session.get(TimelineState, source.output_state_id) if source.output_state_id else None
        )
        facts.source_output_state = dict(source_output.state_json) if source_output else {}
        facts.explicit_state_inheritance = (
            session.scalar(
                select(ShotDependency.id)
                .where(
                    ShotDependency.target_shot_id == target.id,
                    ShotDependency.source_shot_id == source.id,
                    ShotDependency.dependency_type == ShotDependencyType.STATE_INHERITANCE.value,
                )
                .limit(1)
            )
            is not None
        )
        return facts

    @staticmethod
    def _risk(facts: _PairFacts) -> tuple[ContinuityRiskVector, list[str]]:
        """Derive the risk vector from structured rows, never from a caller."""

        extra_reasons: list[str] = []
        transition = facts.transition_type
        same_scene = facts.same_scene and transition not in {
            TimelineTransitionType.SCENE_CUT.value,
            TimelineTransitionType.LOCATION_CHANGE.value,
        }
        scene_change = not same_scene
        timeline_break = transition in {
            TimelineTransitionType.TIME_JUMP.value,
            TimelineTransitionType.FLASH_FORWARD.value,
            TimelineTransitionType.FLASHBACK.value,
            TimelineTransitionType.DREAM.value,
        }
        if transition is None and facts.source is not None:
            extra_reasons.append("TRANSITION_ROW_MISSING")

        source_camera = _state_camera(facts.source_output_state)
        target_camera = _state_camera(facts.target_input_state)
        axis_delta = (
            1.0
            if source_camera.get("axis") is not None
            and target_camera.get("axis") is not None
            and source_camera.get("axis") != target_camera.get("axis")
            else 0.0
        )
        angle_delta = (
            0.6
            if source_camera.get("angle") is not None
            and target_camera.get("angle") is not None
            and source_camera.get("angle") != target_camera.get("angle")
            else 0.0
        )
        scale_delta = (
            0.6
            if source_camera.get("shot_size") is not None
            and target_camera.get("shot_size") is not None
            and source_camera.get("shot_size") != target_camera.get("shot_size")
            else 0.0
        )

        source_characters = _state_characters(facts.source_output_state)
        target_characters = _state_characters(facts.target_input_state)
        entering = target_characters - source_characters
        identity_risk = 0.6 if entering and same_scene else 0.0
        if entering and same_scene:
            extra_reasons.append("CHARACTER_ENTERS_FRAME")

        same_action_chain = bool(
            same_scene
            and not timeline_break
            and (transition in {None, TimelineTransitionType.CONTINUOUS.value})
        )
        action_continuity = 1.0 if same_action_chain else 0.3
        if facts.explicit_state_inheritance:
            extra_reasons.append("EXPLICIT_STATE_INHERITANCE")
            action_continuity = max(action_continuity, 1.0 if same_scene else action_continuity)

        risk = ContinuityRiskVector(
            camera_angle_delta=angle_delta,
            camera_axis_delta=axis_delta,
            shot_scale_delta=scale_delta,
            scene_delta=1.0 if scene_change else 0.0,
            timeline_delta=1.0 if timeline_break else 0.0,
            identity_risk=identity_risk,
            same_scene=same_scene,
            same_timeline=not timeline_break,
            same_action_chain=same_action_chain,
            scene_change=scene_change,
            timeline_jump=transition
            in {
                TimelineTransitionType.TIME_JUMP.value,
                TimelineTransitionType.FLASH_FORWARD.value,
            },
            flashback=transition == TimelineTransitionType.FLASHBACK.value
            or transition == TimelineTransitionType.DREAM.value,
            montage=transition == TimelineTransitionType.MONTAGE.value,
            explicit_reset=transition == TimelineTransitionType.EXPLICIT_RESET.value,
            action_continuity=action_continuity,
            # Planning intent: the previous shot will produce an end frame
            # unless it has already failed. Actual availability is enforced at
            # generation time by the policy engine, which fails closed.
            previous_end_frame_available=not facts.source_failed,
        )
        return risk, extra_reasons

    def _anchor_subjects(
        self, session: Session, project_id: str, target_input_state: dict[str, Any]
    ) -> tuple[AnchorSubject, ...]:
        character_ids = sorted(_state_characters(target_input_state))
        if not character_ids:
            return ()
        rows = {
            row.id: row
            for row in session.scalars(
                select(Character).where(
                    Character.id.in_(character_ids), Character.project_id == project_id
                )
            )
        }
        subjects: list[AnchorSubject] = []
        for character_id in character_ids:
            row = rows.get(character_id)
            if row is None:
                # State keys that are not project characters (e.g. free-text
                # names from hand-written states) still name a subject the
                # frame must carry; they simply have no canonical identity.
                subjects.append(
                    AnchorSubject(
                        character_id=character_id,
                        name=character_id,
                        identity_version_id=None,
                        master_asset_id=None,
                    )
                )
                continue
            version = (
                session.get(CharacterIdentityVersion, row.current_identity_version_id)
                if row.current_identity_version_id
                else None
            )
            subjects.append(
                AnchorSubject(
                    character_id=row.id,
                    name=row.name,
                    identity_version_id=version.id if version else None,
                    master_asset_id=version.master_asset_id if version else None,
                )
            )
        return tuple(subjects)

    @staticmethod
    def _canonical_scene_assets(session: Session, project_id: str) -> list[tuple[str, Any]]:
        return list(
            session.execute(
                select(Asset.id, Asset.canonical_metadata)
                .join(AssetVersion, Asset.canonical_version_id == AssetVersion.id)
                .where(
                    Asset.project_id == project_id,
                    Asset.asset_type == AssetKind.SCENE.value,
                    AssetVersion.primary_media_asset_id.is_not(None),
                )
            ).tuples()
        )

    def _scene_asset_id(self, session: Session, project_id: str, scene_id: str) -> str | None:
        scene = session.get(Scene, scene_id)
        location_id = scene.location_id if scene else None
        if not location_id:
            return None
        for asset_id, metadata in self._canonical_scene_assets(session, project_id):
            if isinstance(metadata, dict) and metadata.get("location_id") == location_id:
                return asset_id
        return None

    def _downgrade_reasons(
        self,
        session: Session,
        project_id: str,
        strategy: str,
        anchor_subjects: tuple[AnchorSubject, ...],
    ) -> list[str]:
        """Canonical material the chosen mode would demand but the project lacks.

        RE_ANCHOR compiles to REANCHOR_FULL, which requires character *and*
        scene references; HYBRID requires character references on both of its
        branches. A project that owns none of that canon cannot satisfy the
        mode ever, so planning it would brick generation with a policy error
        rather than fail anything closed. The plan downgrades to a fresh-start
        `NONE` — nothing is inherited, which is the decision that mattered —
        and says exactly which canon is missing.
        """

        if strategy == FrameAnchorStrategy.INHERIT_LAST_FRAME:
            return []
        missing: list[str] = []
        if not any(subject.master_asset_id for subject in anchor_subjects):
            missing.append("NO_CANONICAL_CHARACTER_REFERENCE")
        if strategy == FrameAnchorStrategy.RECONSTRUCT_FIRST_FRAME and not self._canonical_scene_assets(
            session, project_id
        ):
            missing.append("NO_CANONICAL_SCENE_REFERENCE")
        return missing

    # ------------------------------------------------------------------ plans
    def plan_pair(self, target_shot_id: str) -> FrameAnchorPlan:
        """Decide the frame strategy for one shot against its predecessor.

        The predecessor is the shot's own ``previous_shot_id``; a shot with
        none (the first of an episode) always reconstructs its first frame.
        Committed shots are never re-planned.
        """

        with self.database.session() as session:
            target = session.get(Shot, target_shot_id)
            if target is None:
                raise LookupError("shot not found")
            if target.status == ShotStatus.COMMITTED.value or target.committed_candidate_id:
                raise ValueError("a committed shot's frame strategy is history, not a plan")
            facts = self._pair_facts(session, target)

        if facts.source is None:
            plan = self._first_shot_plan(facts)
        else:
            risk, extra_reasons = self._risk(facts)
            decision = self.decisions.decide(
                risk, project_id=facts.project_id, shot_id=target_shot_id
            )
            strategy = _MODE_TO_STRATEGY[decision.mode]
            with self.database.session() as session:
                anchor_subjects = (
                    self._anchor_subjects(session, facts.project_id, facts.subject_state())
                    if strategy != FrameAnchorStrategy.INHERIT_LAST_FRAME
                    else ()
                )
                scene_asset_id = (
                    self._scene_asset_id(session, facts.project_id, facts.target.scene_id)
                    if strategy == FrameAnchorStrategy.RECONSTRUCT_FIRST_FRAME
                    else None
                )
                downgraded = self._downgrade_reasons(
                    session, facts.project_id, strategy, anchor_subjects
                )
            mode = ContinuityMode.NONE.value if downgraded else decision.mode
            plan = FrameAnchorPlan(
                target_shot_id=target_shot_id,
                source_shot_id=facts.source.id,
                strategy=strategy,
                continuity_mode=mode,
                transition_type=facts.transition_type or "UNRECORDED",
                risk_score=decision.risk_score,
                reasons=tuple(dict.fromkeys([*decision.reasons, *extra_reasons, *downgraded])),
                anchor_subjects=anchor_subjects,
                scene_asset_id=scene_asset_id,
                requires_keyframe_generation=(
                    strategy == FrameAnchorStrategy.RECONSTRUCT_FIRST_FRAME and not downgraded
                ),
            )
        self._apply(plan)
        return plan

    def _first_shot_plan(self, facts: _PairFacts) -> FrameAnchorPlan:
        with self.database.session() as session:
            anchor_subjects = self._anchor_subjects(session, facts.project_id, facts.subject_state())
            scene_asset_id = self._scene_asset_id(session, facts.project_id, facts.target.scene_id)
            downgraded = self._downgrade_reasons(
                session,
                facts.project_id,
                FrameAnchorStrategy.RECONSTRUCT_FIRST_FRAME,
                anchor_subjects,
            )
        return FrameAnchorPlan(
            target_shot_id=facts.target.id,
            source_shot_id=None,
            strategy=FrameAnchorStrategy.RECONSTRUCT_FIRST_FRAME,
            continuity_mode=(
                ContinuityMode.NONE.value if downgraded else ContinuityMode.RE_ANCHOR.value
            ),
            transition_type=facts.transition_type or "EPISODE_START",
            risk_score=1.0,
            reasons=tuple(dict.fromkeys(["FIRST_SHOT", "NO_PREVIOUS_SHOT", *downgraded])),
            anchor_subjects=anchor_subjects,
            scene_asset_id=scene_asset_id,
            requires_keyframe_generation=not downgraded,
        )

    def _apply(self, plan: FrameAnchorPlan) -> None:
        """Write the decision onto the rows the pipeline already reads."""

        with self.database.session() as session:
            shot = session.get(Shot, plan.target_shot_id)
            if shot is None:
                raise LookupError("shot disappeared during frame anchor planning")
            shot.continuity_mode = plan.continuity_mode
            if plan.strategy == FrameAnchorStrategy.INHERIT_LAST_FRAME:
                source = session.get(Shot, plan.source_shot_id) if plan.source_shot_id else None
                if source and source.end_frame_asset_id:
                    shot.start_frame_asset_id = source.end_frame_asset_id
            else:
                # HYBRID uses the previous end frame as soft context only, and
                # RECONSTRUCT starts from canonical references; neither may
                # keep an inherited strong first frame.
                shot.start_frame_asset_id = None
            transition = session.scalar(
                select(TimelineTransition).where(
                    TimelineTransition.target_shot_id == plan.target_shot_id
                )
            )
            if transition is not None:
                transition.metadata_json = {
                    **(transition.metadata_json or {}),
                    "frame_anchor_plan": plan.as_json(),
                }
            session.add(
                DecisionRecord(
                    project_id=self._project_id(session, shot),
                    shot_id=shot.id,
                    decision_type="FRAME_ANCHOR_PLAN",
                    input_features={
                        "source_shot_id": plan.source_shot_id,
                        "transition_type": plan.transition_type,
                        "risk_score": plan.risk_score,
                        "anchor_subjects": [
                            subject.character_id for subject in plan.anchor_subjects
                        ],
                        "scene_asset_id": plan.scene_asset_id,
                    },
                    selected_action=plan.strategy,
                    reason_codes=list(plan.reasons),
                    model_version=self.version,
                    policy_version="frame-anchor-v1",
                )
            )
            session.flush()

    def plan_episode(self, episode_id: str) -> list[FrameAnchorPlan]:
        """Plan every adjacent pair of an episode, in narrative order.

        Committed shots are skipped — their frame strategy is history — and
        every other shot gets exactly one decision against its predecessor.
        """

        with self.database.session() as session:
            episode = session.get(Episode, episode_id)
            if episode is None:
                raise LookupError("episode not found")
            shot_ids = [
                shot_id
                for shot_id, status, committed in session.execute(
                    select(Shot.id, Shot.status, Shot.committed_candidate_id)
                    .join(Scene, Shot.scene_id == Scene.id)
                    .where(Scene.episode_id == episode_id)
                    .order_by(Scene.sequence, Shot.sequence)
                ).all()
                if status != ShotStatus.COMMITTED.value and not committed
            ]
        return [self.plan_pair(shot_id) for shot_id in shot_ids]
