from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from platform_database import Database
from production_domain.models import (
    DecisionRecord,
    Episode,
    Scene,
    Shot,
    ShotStatus,
    TimelineState,
    TimelineTransition,
    TimelineTransitionType,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

_MISSING = object()

PROPAGATION_TARGET_STATUSES = frozenset(
    {
        ShotStatus.DRAFT.value,
        ShotStatus.PLANNED.value,
        ShotStatus.READY.value,
    }
)

_BRANCH_TRANSITIONS = frozenset(
    {
        TimelineTransitionType.FLASHBACK,
        TimelineTransitionType.FLASH_FORWARD,
        TimelineTransitionType.DREAM,
    }
)
_RECONCILIATION_TRANSITIONS = frozenset(
    {
        TimelineTransitionType.TIME_JUMP,
        TimelineTransitionType.FLASH_FORWARD,
        TimelineTransitionType.MONTAGE,
    }
)
_RESET_TRANSITIONS = frozenset(
    {
        TimelineTransitionType.SCENE_CUT,
        TimelineTransitionType.TIME_JUMP,
        TimelineTransitionType.FLASHBACK,
        TimelineTransitionType.FLASH_FORWARD,
        TimelineTransitionType.MONTAGE,
        TimelineTransitionType.DREAM,
        TimelineTransitionType.LOCATION_CHANGE,
        TimelineTransitionType.EXPLICIT_RESET,
    }
)
_LEGACY_TRANSITIONS = {
    "SCENE_CHANGE": TimelineTransitionType.SCENE_CUT,
    "TIMELINE_JUMP": TimelineTransitionType.TIME_JUMP,
}
_SPATIAL_CHARACTER_FIELDS = frozenset(
    {
        "position",
        "orientation",
        "location",
        "coordinates",
        "blocking",
        "pose",
    }
)


class TimelinePropagationError(RuntimeError):
    pass


@dataclass(frozen=True)
class TimelinePropagationResult:
    current_shot_id: str
    next_shot_id: str | None
    propagated: bool
    reason_code: str
    source_state_id: str
    target_state_id: str | None
    target_output_state_id: str | None
    output_rebased: bool
    transition_type: str | None
    reconciliation_required: bool
    branch_key: str | None


@dataclass(frozen=True)
class TimelineStaleResult:
    edited_shot_id: str
    marked_shot_ids: tuple[str, ...]
    planning_shot_ids: tuple[str, ...]
    immutable_shot_ids: tuple[str, ...]
    reason_code: str = "RECOMPUTE_REQUIRED"


@dataclass(frozen=True)
class TimelineRecomputeResult:
    edited_shot_id: str
    recomputed_shot_ids: tuple[str, ...]
    blocked_shot_id: str | None
    reason_code: str


class AuthoritativeTimelineStateEngine:
    """Propagate committed SQL state without consulting semantic memory.

    ``TimelineTransition`` is the relational source of truth for boundaries.
    Legacy transition hints are migrated into a row on first use. Vector/LLM
    memory may help retrieve context, but it is never accepted as a replacement
    for the committed ``SHOT_OUTPUT`` row.
    """

    version = "sql-timeline-propagation-v3"
    policy_version = "timeline-v3"

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _project_id(session: Session, shot: Shot) -> str:
        scene = session.get(Scene, shot.scene_id)
        episode = session.get(Episode, scene.episode_id) if scene else None
        if episode is None:
            raise TimelinePropagationError("shot project could not be resolved")
        return episode.project_id

    @staticmethod
    def _legacy_transition_kind(next_input: TimelineState) -> TimelineTransitionType | None:
        state = next_input.state_json or {}
        raw: Any = state.get("transition_kind") or state.get("timeline_transition")
        if raw is None and isinstance(state.get("transition"), dict):
            raw = state["transition"].get("kind")
        if not raw:
            return None
        normalized = str(raw).strip().upper()
        normalized = _LEGACY_TRANSITIONS.get(normalized, normalized)
        try:
            return TimelineTransitionType(normalized)
        except ValueError as exc:
            raise TimelinePropagationError(f"unknown timeline transition: {raw}") from exc

    @staticmethod
    def _transition_metadata(transition_type: TimelineTransitionType) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "propagation_semantics": "FULL"
            if transition_type is TimelineTransitionType.CONTINUOUS
            else "RESET_BOUNDARY",
        }
        if transition_type in {
            TimelineTransitionType.SCENE_CUT,
            TimelineTransitionType.LOCATION_CHANGE,
        }:
            metadata.update(
                {
                    "spatial_state": "RESET",
                    "character_state": "MAY_PROPAGATE_WITH_EXPLICIT_OPT_IN",
                    "propagate_character_state": False,
                }
            )
        if transition_type in _BRANCH_TRANSITIONS:
            metadata["timeline_branch"] = "NEW_BRANCH"
        return metadata

    @staticmethod
    def _branch_key(
        transition_type: TimelineTransitionType,
        target_shot_id: str,
        supplied: str | None,
    ) -> str | None:
        if transition_type not in _BRANCH_TRANSITIONS:
            return None
        return supplied or f"{transition_type.value.lower()}:{target_shot_id}"

    @staticmethod
    def _transition_type(value: TimelineTransitionType | str) -> TimelineTransitionType:
        try:
            return TimelineTransitionType(str(value).strip().upper())
        except ValueError as exc:
            raise TimelinePropagationError(f"unknown timeline transition: {value}") from exc

    @staticmethod
    def _register_branch(
        session: Session,
        *,
        project_id: str,
        branch_key: str | None,
        transition_type: TimelineTransitionType,
        source_shot: Shot,
        target_shot: Shot,
    ) -> None:
        """Give a freshly named branch its lifecycle row, in the same transaction.

        The parent scope is the branch the *source* shot lives on (the branch
        forks from wherever its fork shot stands), and the fork shot is
        recorded — a dream or flashback with no parent is invalid by
        construction. Function-level import: character_core depends on this
        package, so the module edge cannot point back at import time.
        """

        if not branch_key:
            return
        from character_core.branches import ensure_branch_in_session

        parent_transition = session.scalar(
            select(TimelineTransition).where(
                TimelineTransition.target_shot_id == source_shot.id
            )
        )
        parent_scope = (
            str(parent_transition.branch_key)
            if parent_transition is not None and parent_transition.branch_key
            else "main"
        )
        kind = {
            TimelineTransitionType.DREAM: "DREAM",
            TimelineTransitionType.FLASHBACK: "FLASHBACK",
            TimelineTransitionType.FLASH_FORWARD: "FLASH_FORWARD",
        }.get(transition_type, "ALTERNATE")
        ensure_branch_in_session(
            session,
            project_id=project_id,
            scope_key=branch_key,
            branch_kind=kind,
            parent_scope_key=parent_scope,
            fork_shot_id=source_shot.id,
            metadata={
                "first_branch_shot_id": target_shot.id,
                "transition_type": transition_type.value,
            },
        )

    def _resolve_transition(
        self,
        session: Session,
        source_shot: Shot,
        target_shot: Shot,
        target_input: TimelineState,
        *,
        project_id: str,
    ) -> tuple[TimelineTransition, TimelineTransitionType]:
        transition = session.scalar(
            select(TimelineTransition)
            .where(TimelineTransition.target_shot_id == target_shot.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if transition is not None:
            if transition.project_id != project_id or transition.source_shot_id != source_shot.id:
                raise TimelinePropagationError("timeline transition has invalid shot ownership")
            transition_type = self._transition_type(transition.transition_type)
            if transition.reconciliation_required:
                target_shot.downstream_state_stale = True
                target_shot.stale_reason = "RECOMPUTE_REQUIRED"
                target_shot.stale_from_shot_id = source_shot.id
            return transition, transition_type

        legacy_type = self._legacy_transition_kind(target_input)
        transition_type = legacy_type or (
            TimelineTransitionType.CONTINUOUS
            if target_shot.scene_id == source_shot.scene_id
            else TimelineTransitionType.SCENE_CUT
        )
        metadata = self._transition_metadata(transition_type)
        if legacy_type is not None:
            metadata["inferred_from"] = "legacy_state_hint"
        elif target_shot.scene_id != source_shot.scene_id:
            metadata["inferred_from"] = "scene_boundary"
        else:
            metadata["inferred_from"] = "linked_shot_default"
        transition = TimelineTransition(
            project_id=project_id,
            source_shot_id=source_shot.id,
            target_shot_id=target_shot.id,
            transition_type=transition_type.value,
            branch_key=self._branch_key(transition_type, target_shot.id, None),
            reconciliation_required=transition_type in _RECONCILIATION_TRANSITIONS,
            metadata_json=metadata,
        )
        session.add(transition)
        self._register_branch(
            session,
            project_id=project_id,
            branch_key=transition.branch_key,
            transition_type=transition_type,
            source_shot=source_shot,
            target_shot=target_shot,
        )
        if transition.reconciliation_required:
            target_shot.downstream_state_stale = True
            target_shot.stale_reason = "RECOMPUTE_REQUIRED"
            target_shot.stale_from_shot_id = source_shot.id
        session.flush()
        return transition, transition_type

    def set_transition(
        self,
        target_shot_id: str,
        transition_type: TimelineTransitionType | str,
        *,
        branch_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TimelineTransition:
        """Create or replace the explicit transition leading into a shot."""

        normalized = self._transition_type(transition_type)
        with self.database.session() as session:
            target = session.scalar(
                select(Shot)
                .where(Shot.id == target_shot_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if target is None:
                raise LookupError("target shot not found")
            if not target.previous_shot_id:
                raise TimelinePropagationError("target shot has no source shot")
            source = session.get(Shot, target.previous_shot_id)
            if source is None:
                raise TimelinePropagationError("transition source shot not found")
            project_id = self._project_id(session, target)
            if self._project_id(session, source) != project_id:
                raise TimelinePropagationError("transition shots belong to different projects")
            row = session.scalar(
                select(TimelineTransition)
                .where(TimelineTransition.target_shot_id == target.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None:
                row = TimelineTransition(project_id=project_id, target_shot_id=target.id)
                session.add(row)
            row.project_id = project_id
            row.source_shot_id = source.id
            row.transition_type = normalized.value
            row.branch_key = self._branch_key(normalized, target.id, branch_key)
            row.reconciliation_required = normalized in _RECONCILIATION_TRANSITIONS
            row.metadata_json = {
                **self._transition_metadata(normalized),
                **(metadata or {}),
            }
            self._register_branch(
                session,
                project_id=project_id,
                branch_key=row.branch_key,
                transition_type=normalized,
                source_shot=source,
                target_shot=target,
            )
            if row.reconciliation_required:
                target.downstream_state_stale = True
                target.stale_reason = "RECOMPUTE_REQUIRED"
                target.stale_from_shot_id = source.id
            session.flush()
            # Detach a stable snapshot from the short-lived session.
            session.expunge(row)
            return row

    def list_transitions(self, project_id: str) -> list[dict[str, Any]]:
        """Minimal development observability for formal transition records."""

        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(TimelineTransition)
                    .where(TimelineTransition.project_id == project_id)
                    .order_by(TimelineTransition.created_at, TimelineTransition.id)
                )
            )
            return [
                {
                    "id": row.id,
                    "project_id": row.project_id,
                    "source_shot_id": row.source_shot_id,
                    "target_shot_id": row.target_shot_id,
                    "transition_type": row.transition_type,
                    "branch_key": row.branch_key,
                    "reconciliation_required": row.reconciliation_required,
                    "metadata": deepcopy(row.metadata_json),
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
                for row in rows
            ]

    @classmethod
    def _replay_delta(cls, baseline: Any, planned: Any, current: Any) -> Any:
        """Replay the deterministic baseline-to-plan delta onto current state."""

        if baseline == planned:
            return deepcopy(current) if current is not _MISSING else _MISSING
        if isinstance(baseline, dict) and isinstance(planned, dict):
            dict_result = deepcopy(current) if isinstance(current, dict) else {}
            ordered_keys = [*baseline, *(key for key in planned if key not in baseline)]
            for key in ordered_keys:
                if key not in planned:
                    dict_result.pop(key, None)
                    continue
                if key not in baseline:
                    dict_result[key] = deepcopy(planned[key])
                    continue
                rebased = cls._replay_delta(
                    baseline[key],
                    planned[key],
                    dict_result.get(key, _MISSING),
                )
                if rebased is _MISSING:
                    dict_result.pop(key, None)
                else:
                    dict_result[key] = rebased
            return dict_result
        if isinstance(baseline, list) and isinstance(planned, list):
            if len(planned) >= len(baseline) and planned[: len(baseline)] == baseline:
                list_result = deepcopy(current) if isinstance(current, list) else []
                for item in planned[len(baseline) :]:
                    if item not in list_result:
                        list_result.append(deepcopy(item))
                return list_result
            return deepcopy(planned)
        return deepcopy(planned)

    @classmethod
    def _rebase_planned_output(
        cls,
        baseline_input: dict[str, Any],
        planned_output: dict[str, Any],
        authoritative_input: dict[str, Any],
    ) -> dict[str, Any]:
        rebased = cls._replay_delta(baseline_input, planned_output, authoritative_input)
        if not isinstance(rebased, dict):  # pragma: no cover - guarded by typed inputs above.
            raise TimelinePropagationError("rebased shot output state must be an object")
        return rebased

    @staticmethod
    def _character_state_across_spatial_reset(
        baseline: dict[str, Any],
        authoritative: dict[str, Any],
    ) -> dict[str, Any]:
        """Copy explicitly stable character facts without carrying spatial pose."""

        reconciled = deepcopy(baseline)
        for key in ("costume", "held_props"):
            if key in authoritative:
                reconciled[key] = deepcopy(authoritative[key])
        source_characters = authoritative.get("characters")
        target_characters = reconciled.get("characters")
        if isinstance(source_characters, dict) and isinstance(target_characters, dict):
            for character_id, source_state in source_characters.items():
                if not isinstance(source_state, dict):
                    continue
                target_state = target_characters.setdefault(character_id, {})
                if not isinstance(target_state, dict):
                    continue
                for field, value in source_state.items():
                    if field not in _SPATIAL_CHARACTER_FIELDS:
                        target_state[field] = deepcopy(value)
        return reconciled

    def _apply_transition(
        self,
        *,
        transition: TimelineTransition,
        transition_type: TimelineTransitionType,
        output_state: TimelineState,
        next_input: TimelineState,
        next_output: TimelineState,
    ) -> tuple[bool, bool, str]:
        if transition.reconciliation_required:
            return False, False, f"{transition_type.value}_RECONCILIATION_REQUIRED"
        if transition_type in _RESET_TRANSITIONS:
            if (
                transition_type in {TimelineTransitionType.SCENE_CUT, TimelineTransitionType.LOCATION_CHANGE}
                and transition.metadata_json.get("propagate_character_state") is True
            ):
                baseline_input = deepcopy(next_input.state_json)
                authoritative_input = self._character_state_across_spatial_reset(
                    baseline_input,
                    deepcopy(output_state.state_json),
                )
                planned_output = deepcopy(next_output.state_json)
                next_input.state_json = authoritative_input
                next_input.previous_state_id = output_state.id
                next_output.state_json = self._rebase_planned_output(
                    baseline_input,
                    planned_output,
                    authoritative_input,
                )
                return True, True, f"{transition_type.value}_CHARACTER_PROPAGATED_SPATIAL_RESET"
            if transition_type in _BRANCH_TRANSITIONS:
                return False, False, f"{transition_type.value}_BRANCH_START"
            inferred = transition.metadata_json.get("inferred_from")
            if transition_type is TimelineTransitionType.SCENE_CUT and inferred == "scene_boundary":
                return False, False, "SCENE_CHANGE"
            return False, False, f"{transition_type.value}_RESET"

        baseline_input = deepcopy(next_input.state_json)
        authoritative_input = deepcopy(output_state.state_json)
        planned_output = deepcopy(next_output.state_json)
        if not all(
            isinstance(value, dict) for value in (baseline_input, authoritative_input, planned_output)
        ):
            raise TimelinePropagationError("timeline state payloads must be objects")
        next_input.state_json = authoritative_input
        next_input.previous_state_id = output_state.id
        next_output.state_json = self._rebase_planned_output(
            baseline_input,
            planned_output,
            authoritative_input,
        )
        return True, True, "CONTINUOUS_TIMELINE"

    @staticmethod
    def _transition_requires_state_write(
        transition: TimelineTransition,
        transition_type: TimelineTransitionType,
    ) -> bool:
        return transition_type is TimelineTransitionType.CONTINUOUS or (
            transition_type in {TimelineTransitionType.SCENE_CUT, TimelineTransitionType.LOCATION_CHANGE}
            and transition.metadata_json.get("propagate_character_state") is True
            and not transition.reconciliation_required
        )

    @staticmethod
    def _preserved_boundary_reason(
        transition: TimelineTransition,
        transition_type: TimelineTransitionType,
    ) -> str:
        if transition.reconciliation_required:
            return f"{transition_type.value}_RECONCILIATION_REQUIRED"
        if transition_type in _BRANCH_TRANSITIONS:
            return f"{transition_type.value}_BRANCH_START"
        if (
            transition_type is TimelineTransitionType.SCENE_CUT
            and transition.metadata_json.get("inferred_from") == "scene_boundary"
        ):
            return "SCENE_CHANGE"
        return f"{transition_type.value}_RESET"

    def propagate(
        self,
        session: Session,
        shot: Shot,
        output_state: TimelineState,
        *,
        record_decision: bool = True,
    ) -> TimelinePropagationResult:
        locked_shot = session.scalar(
            select(Shot).where(Shot.id == shot.id).with_for_update().execution_options(populate_existing=True)
        )
        locked_output = session.scalar(
            select(TimelineState)
            .where(TimelineState.id == output_state.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked_shot is None or locked_output is None:
            raise TimelinePropagationError("source shot or output state disappeared")
        shot = locked_shot
        output_state = locked_output
        project_id = self._project_id(session, shot)
        if (
            output_state.id != shot.output_state_id
            or output_state.shot_id not in {None, shot.id}
            or output_state.project_id != project_id
            or output_state.state_kind != "SHOT_OUTPUT"
        ):
            raise TimelinePropagationError("source is not the shot's authoritative output state")
        if shot.status != ShotStatus.COMMITTED.value:
            raise TimelinePropagationError("only a committed shot may propagate timeline state")

        next_shot = (
            session.scalar(
                select(Shot)
                .where(Shot.id == shot.next_shot_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if shot.next_shot_id
            else None
        )
        target_state_id: str | None = None
        target_output_state_id: str | None = None
        transition_type: TimelineTransitionType | None = None
        transition: TimelineTransition | None = None
        propagated = False
        output_rebased = False
        reason = "NO_NEXT_SHOT"
        if next_shot is not None:
            if self._project_id(session, next_shot) != project_id:
                raise TimelinePropagationError("next shot belongs to a different project")
            next_input = (
                session.scalar(
                    select(TimelineState)
                    .where(TimelineState.id == next_shot.input_state_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if next_shot.input_state_id
                else None
            )
            if next_input is None:
                raise TimelinePropagationError("next shot has no authoritative input state")
            target_state_id = next_input.id
            if (
                next_input.project_id != project_id
                or next_input.state_kind != "SHOT_INPUT"
                or next_input.shot_id not in {None, next_shot.id}
            ):
                raise TimelinePropagationError("next shot input state has invalid ownership or kind")
            transition, transition_type = self._resolve_transition(
                session,
                shot,
                next_shot,
                next_input,
                project_id=project_id,
            )
            requires_state_write = self._transition_requires_state_write(
                transition,
                transition_type,
            )
            if next_shot.status not in PROPAGATION_TARGET_STATUSES and requires_state_write:
                raise TimelinePropagationError(
                    f"cannot overwrite the input state of a {next_shot.status} next shot"
                )
            if not requires_state_write:
                reason = self._preserved_boundary_reason(transition, transition_type)
                target_output_state_id = next_shot.output_state_id
            else:
                next_output = (
                    session.scalar(
                        select(TimelineState)
                        .where(TimelineState.id == next_shot.output_state_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if next_shot.output_state_id
                    else None
                )
                if next_output is None:
                    raise TimelinePropagationError("next shot has no planned output state")
                target_output_state_id = next_output.id
                if (
                    next_output.project_id != project_id
                    or next_output.state_kind != "SHOT_OUTPUT"
                    or next_output.shot_id not in {None, next_shot.id}
                ):
                    raise TimelinePropagationError("next shot output state has invalid ownership or kind")
                propagated, output_rebased, reason = self._apply_transition(
                    transition=transition,
                    transition_type=transition_type,
                    output_state=output_state,
                    next_input=next_input,
                    next_output=next_output,
                )

        result = TimelinePropagationResult(
            current_shot_id=shot.id,
            next_shot_id=next_shot.id if next_shot else None,
            propagated=propagated,
            reason_code=reason,
            source_state_id=output_state.id,
            target_state_id=target_state_id,
            target_output_state_id=target_output_state_id,
            output_rebased=output_rebased,
            transition_type=transition_type.value if transition_type else None,
            reconciliation_required=bool(transition and transition.reconciliation_required),
            branch_key=transition.branch_key if transition else None,
        )
        if record_decision:
            session.add(
                DecisionRecord(
                    project_id=project_id,
                    shot_id=shot.id,
                    decision_type="TIMELINE_PROPAGATION",
                    input_features={
                        "source_state_id": output_state.id,
                        "source_state_kind": output_state.state_kind,
                        "next_shot_id": result.next_shot_id,
                        "target_state_id": target_state_id,
                        "target_output_state_id": target_output_state_id,
                        "output_rebased": output_rebased,
                        "transition_type": result.transition_type,
                        "reconciliation_required": result.reconciliation_required,
                        "branch_key": result.branch_key,
                        "sql_authoritative": True,
                    },
                    selected_action="PROPAGATE" if propagated else "PRESERVE_TRANSITION_BOUNDARY",
                    reason_codes=[reason],
                    model_version=self.version,
                    policy_version=self.policy_version,
                )
            )
        return result

    def propagate_shot(self, shot_id: str) -> TimelinePropagationResult:
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if shot is None or shot.output_state_id is None:
                raise LookupError("shot or output timeline state not found")
            output_state = session.get(TimelineState, shot.output_state_id)
            if output_state is None:
                raise LookupError("shot output timeline state not found")
            return self.propagate(session, shot, output_state)

    def mark_downstream_stale_after_edit(self, edited_shot_id: str) -> TimelineStaleResult:
        """Mark every later shot stale without modifying timeline or media payloads."""

        with self.database.session() as session:
            edited = session.scalar(
                select(Shot)
                .where(Shot.id == edited_shot_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if edited is None:
                raise LookupError("edited shot not found")
            if edited.status != ShotStatus.COMMITTED.value:
                raise TimelinePropagationError(
                    "only an edited committed shot can invalidate downstream state"
                )
            project_id = self._project_id(session, edited)
            marked: list[str] = []
            planning: list[str] = []
            immutable: list[str] = []
            visited = {edited.id}
            next_id = edited.next_shot_id
            while next_id:
                if next_id in visited:
                    raise TimelinePropagationError("shot lineage contains a cycle")
                visited.add(next_id)
                downstream = session.scalar(
                    select(Shot)
                    .where(Shot.id == next_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if downstream is None:
                    raise TimelinePropagationError("shot lineage points to a missing downstream shot")
                if self._project_id(session, downstream) != project_id:
                    raise TimelinePropagationError("downstream shot belongs to a different project")
                downstream.downstream_state_stale = True
                downstream.stale_reason = "RECOMPUTE_REQUIRED"
                downstream.stale_from_shot_id = edited.id
                marked.append(downstream.id)
                if downstream.status in PROPAGATION_TARGET_STATUSES:
                    planning.append(downstream.id)
                else:
                    immutable.append(downstream.id)
                next_id = downstream.next_shot_id
            session.add(
                DecisionRecord(
                    project_id=project_id,
                    shot_id=edited.id,
                    decision_type="TIMELINE_DOWNSTREAM_INVALIDATION",
                    input_features={
                        "edited_shot_id": edited.id,
                        "marked_shot_ids": marked,
                        "planning_shot_ids": planning,
                        "immutable_shot_ids": immutable,
                        "committed_media_modified": False,
                    },
                    selected_action="RECOMPUTE_REQUIRED",
                    reason_codes=["DOWNSTREAM_STATE_STALE_AFTER_COMMITTED_EDIT"],
                    model_version=self.version,
                    policy_version=self.policy_version,
                )
            )
            return TimelineStaleResult(
                edited_shot_id=edited.id,
                marked_shot_ids=tuple(marked),
                planning_shot_ids=tuple(planning),
                immutable_shot_ids=tuple(immutable),
            )

    def recompute_downstream_planning(self, edited_shot_id: str) -> TimelineRecomputeResult:
        """Rebase stale planning states and stop at reconciliation or immutable work."""

        with self.database.session() as session:
            source = session.scalar(
                select(Shot)
                .where(Shot.id == edited_shot_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if source is None:
                raise LookupError("edited shot not found")
            if source.status != ShotStatus.COMMITTED.value or not source.output_state_id:
                raise TimelinePropagationError("recompute must start from a committed shot output")
            project_id = self._project_id(session, source)
            source_output = session.get(TimelineState, source.output_state_id)
            if source_output is None:
                raise TimelinePropagationError("source shot output state not found")
            if (
                source_output.project_id != project_id
                or source_output.state_kind != "SHOT_OUTPUT"
                or source_output.shot_id not in {None, source.id}
            ):
                raise TimelinePropagationError("source shot output state has invalid ownership or kind")
            recomputed: list[str] = []
            blocked_shot_id: str | None = None
            reason = "RECOMPUTED"
            visited = {source.id}
            next_id = source.next_shot_id
            while next_id:
                if next_id in visited:
                    raise TimelinePropagationError("shot lineage contains a cycle")
                visited.add(next_id)
                target = session.scalar(
                    select(Shot)
                    .where(Shot.id == next_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if target is None:
                    raise TimelinePropagationError("shot lineage points to a missing downstream shot")
                if self._project_id(session, target) != project_id:
                    raise TimelinePropagationError("downstream shot belongs to a different project")
                if target.status not in PROPAGATION_TARGET_STATUSES:
                    blocked_shot_id = target.id
                    reason = "IMMUTABLE_OR_ACTIVE_SHOT_REQUIRES_REVIEW"
                    break
                if not target.input_state_id or not target.output_state_id:
                    raise TimelinePropagationError("downstream planning shot has incomplete timeline state")
                target_input = session.get(TimelineState, target.input_state_id)
                target_output = session.get(TimelineState, target.output_state_id)
                if target_input is None or target_output is None:
                    raise TimelinePropagationError("downstream timeline state not found")
                if (
                    target_input.project_id != project_id
                    or target_input.state_kind != "SHOT_INPUT"
                    or target_input.shot_id not in {None, target.id}
                    or target_output.project_id != project_id
                    or target_output.state_kind != "SHOT_OUTPUT"
                    or target_output.shot_id not in {None, target.id}
                ):
                    raise TimelinePropagationError("downstream timeline state has invalid ownership or kind")
                transition, transition_type = self._resolve_transition(
                    session,
                    source,
                    target,
                    target_input,
                    project_id=project_id,
                )
                if transition.reconciliation_required:
                    blocked_shot_id = target.id
                    reason = f"{transition_type.value}_RECONCILIATION_REQUIRED"
                    break
                if transition_type is TimelineTransitionType.CONTINUOUS:
                    baseline_input = deepcopy(target_input.state_json)
                    authoritative_input = deepcopy(source_output.state_json)
                    target_output.state_json = self._rebase_planned_output(
                        baseline_input,
                        deepcopy(target_output.state_json),
                        authoritative_input,
                    )
                    target_input.state_json = authoritative_input
                    target_input.previous_state_id = source_output.id
                elif (
                    transition_type
                    in {TimelineTransitionType.SCENE_CUT, TimelineTransitionType.LOCATION_CHANGE}
                    and transition.metadata_json.get("propagate_character_state") is True
                ):
                    baseline_input = deepcopy(target_input.state_json)
                    reconciled_input = self._character_state_across_spatial_reset(
                        baseline_input,
                        deepcopy(source_output.state_json),
                    )
                    target_output.state_json = self._rebase_planned_output(
                        baseline_input,
                        deepcopy(target_output.state_json),
                        reconciled_input,
                    )
                    target_input.state_json = reconciled_input
                    target_input.previous_state_id = source_output.id
                # Other transition kinds are explicit reset/branch boundaries;
                # their pre-existing planning state remains independent.
                target.downstream_state_stale = False
                target.stale_reason = None
                target.stale_from_shot_id = None
                recomputed.append(target.id)
                source = target
                source_output = target_output
                next_id = target.next_shot_id
            session.add(
                DecisionRecord(
                    project_id=project_id,
                    shot_id=edited_shot_id,
                    decision_type="TIMELINE_RECOMPUTE",
                    input_features={
                        "edited_shot_id": edited_shot_id,
                        "recomputed_shot_ids": recomputed,
                        "blocked_shot_id": blocked_shot_id,
                        "committed_media_modified": False,
                    },
                    selected_action="RECOMPUTED" if blocked_shot_id is None else "REVIEW_REQUIRED",
                    reason_codes=[reason],
                    model_version=self.version,
                    policy_version=self.policy_version,
                )
            )
            return TimelineRecomputeResult(
                edited_shot_id=edited_shot_id,
                recomputed_shot_ids=tuple(recomputed),
                blocked_shot_id=blocked_shot_id,
                reason_code=reason,
            )

    def reconcile_transition(
        self,
        target_shot_id: str,
        reconciled_input_state: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        """Explicitly reconcile a discontinuity without changing committed media."""

        if not reason.strip():
            raise ValueError("reconciliation reason is required")
        with self.database.session() as session:
            target = session.scalar(
                select(Shot)
                .where(Shot.id == target_shot_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if target is None:
                raise LookupError("target shot not found")
            if target.status not in PROPAGATION_TARGET_STATUSES:
                raise TimelinePropagationError("committed or active shot planning state is immutable")
            transition = session.scalar(
                select(TimelineTransition)
                .where(TimelineTransition.target_shot_id == target.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if transition is None or not transition.reconciliation_required:
                raise TimelinePropagationError("timeline transition does not require reconciliation")
            if not target.input_state_id or not target.output_state_id:
                raise TimelinePropagationError("target shot has incomplete timeline state")
            target_input = session.get(TimelineState, target.input_state_id)
            target_output = session.get(TimelineState, target.output_state_id)
            if target_input is None or target_output is None:
                raise TimelinePropagationError("target timeline state not found")
            project_id = self._project_id(session, target)
            if (
                transition.project_id != project_id
                or target_input.project_id != project_id
                or target_input.state_kind != "SHOT_INPUT"
                or target_input.shot_id not in {None, target.id}
                or target_output.project_id != project_id
                or target_output.state_kind != "SHOT_OUTPUT"
                or target_output.shot_id not in {None, target.id}
            ):
                raise TimelinePropagationError("timeline reconciliation state has invalid ownership or kind")
            baseline_input = deepcopy(target_input.state_json)
            target_output.state_json = self._rebase_planned_output(
                baseline_input,
                deepcopy(target_output.state_json),
                deepcopy(reconciled_input_state),
            )
            target_input.state_json = deepcopy(reconciled_input_state)
            transition.reconciliation_required = False
            transition.metadata_json = {
                **transition.metadata_json,
                "reconciled": True,
                "reconciliation_reason": reason.strip(),
            }
            target.downstream_state_stale = False
            target.stale_reason = None
            target.stale_from_shot_id = None
            session.add(
                DecisionRecord(
                    project_id=project_id,
                    shot_id=target.id,
                    decision_type="TIMELINE_TRANSITION_RECONCILIATION",
                    input_features={
                        "transition_id": transition.id,
                        "transition_type": transition.transition_type,
                        "reason": reason.strip(),
                        "committed_media_modified": False,
                    },
                    selected_action="RECONCILED_PLANNING_STATE",
                    reason_codes=["EXPLICIT_RECONCILIATION"],
                    model_version=self.version,
                    policy_version=self.policy_version,
                )
            )
