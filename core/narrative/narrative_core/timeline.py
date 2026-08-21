from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from platform_database import Database
from production_domain.models import DecisionRecord, Episode, Scene, Shot, ShotStatus, TimelineState
from sqlalchemy import select
from sqlalchemy.orm import Session

RESET_TRANSITIONS = frozenset(
    {
        "SCENE_CHANGE",
        "TIMELINE_JUMP",
        "FLASHBACK",
        "MONTAGE",
        "EXPLICIT_RESET",
    }
)

_MISSING = object()

PROPAGATION_TARGET_STATUSES = frozenset(
    {
        ShotStatus.DRAFT.value,
        ShotStatus.PLANNED.value,
        ShotStatus.READY.value,
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


class AuthoritativeTimelineStateEngine:
    """Propagate committed SQL state without consulting semantic memory.

    Vector/LLM memory may help retrieve context, but it is never accepted as a
    replacement for the committed ``SHOT_OUTPUT`` row.
    """

    version = "sql-timeline-propagation-v2"

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
    def _transition_kind(next_input: TimelineState) -> str | None:
        state = next_input.state_json or {}
        raw: Any = state.get("transition_kind") or state.get("timeline_transition")
        if raw is None and isinstance(state.get("transition"), dict):
            raw = state["transition"].get("kind")
        return str(raw).strip().upper() if raw else None

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
        if not isinstance(rebased, dict):  # pragma: no cover - guarded by the typed inputs above.
            raise TimelinePropagationError("rebased shot output state must be an object")
        return rebased

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
            transition_kind = self._transition_kind(next_input)
            if next_shot.scene_id != shot.scene_id:
                reason = "SCENE_CHANGE"
            elif transition_kind in RESET_TRANSITIONS:
                reason = transition_kind
            elif next_shot.status not in PROPAGATION_TARGET_STATUSES:
                raise TimelinePropagationError(
                    f"cannot overwrite the input state of a {next_shot.status} next shot"
                )
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
                baseline_input = deepcopy(next_input.state_json)
                authoritative_input = deepcopy(output_state.state_json)
                planned_output = deepcopy(next_output.state_json)
                if not all(
                    isinstance(value, dict) for value in (baseline_input, authoritative_input, planned_output)
                ):
                    raise TimelinePropagationError("timeline state payloads must be objects")
                rebased_output = self._rebase_planned_output(
                    baseline_input,
                    planned_output,
                    authoritative_input,
                )
                # Exact copy is intentional: this is the state invariant. IDs and
                # provenance stay in relational columns rather than contaminating
                # the semantic state payload.
                next_input.state_json = authoritative_input
                next_input.previous_state_id = output_state.id
                next_output.state_json = rebased_output
                propagated = True
                output_rebased = True
                reason = "CONTINUOUS_TIMELINE"

        result = TimelinePropagationResult(
            current_shot_id=shot.id,
            next_shot_id=next_shot.id if next_shot else None,
            propagated=propagated,
            reason_code=reason,
            source_state_id=output_state.id,
            target_state_id=target_state_id,
            target_output_state_id=target_output_state_id,
            output_rebased=output_rebased,
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
                        "sql_authoritative": True,
                    },
                    selected_action="PROPAGATE" if propagated else "PRESERVE_RESET_BOUNDARY",
                    reason_codes=[reason],
                    model_version=self.version,
                    policy_version="timeline-v2",
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
