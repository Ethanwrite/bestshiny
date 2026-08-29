"""Series -> Episodes -> "create the next episode".

The next episode is never a resubmission of the previous script. Preparation
computes an ``EpisodeContinuationContext`` snapshot from the systems that
already own each truth, proposes an episode brief and story beats from it,
and confirmation compiles through the **existing** chain - narrative
compiler, timeline engine, shot dependencies, frame anchor planner - then
links the episode boundary according to the declared continuation mode:

    CONTINUOUS        previous_shot link + CONTINUOUS transition +
                      STATE_INHERITANCE dependency + state propagation;
                      the frame anchor planner may then inherit the previous
                      episode's tail frame inside the same location
    TIME_JUMP /       previous_shot link + the discontinuity transition,
    LOCATION_CHANGE   reconciled on the spot through the timeline engine;
                      narrative and character state inherit through the
                      ledger and state heads, the tail frame does not
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Protocol

from creative_director_core.beats import (
    ShotIntentMismatch,
    action_line,
    apply_shot_intents,
    dialogue_line,
    render_script,
)
from narrative_core import NarrativeCompiler
from narrative_core.timeline import AuthoritativeTimelineStateEngine
from platform_database import Database
from production_domain.models import (
    Episode,
    EpisodeContinuation,
    Scene,
    Shot,
    ShotDependency,
    ShotDependencyOrigin,
    ShotDependencyType,
    ShotStatus,
    TimelineState,
    TimelineTransitionType,
)
from sqlalchemy import func, select

from .context import CONTINUITY_CLASSES, ContinuationContextBuilder, context_hash


class EpisodeContinuationConflict(ValueError):
    """The request contradicts recorded continuation state."""


class FrameAnchorPlanning(Protocol):
    def plan_episode(self, episode_id: str) -> list[Any]: ...


class SeriesLedger(Protocol):
    def open_obligation(
        self,
        project_id: str,
        *,
        obligation_key: str,
        promise: str,
        episode: int,
        scene_sequence: int = 0,
        shot_sequence: int = 0,
        shot_id: str | None = None,
        category: str = "GENERIC",
    ) -> str: ...


class ModelReasoner(Protocol):
    async def execute_chat(
        self,
        project_id: str,
        role: Any,
        *,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> Any: ...


def _now() -> datetime:
    return datetime.now(UTC)


class EpisodeContinuationService:
    version = "episode-continuation-v1"

    def __init__(
        self,
        database: Database,
        *,
        context_builder: ContinuationContextBuilder,
        narrative: NarrativeCompiler,
        frame_anchors: FrameAnchorPlanning,
        ledger: SeriesLedger,
        model_roles: ModelReasoner | None = None,
    ):
        self.database = database
        self.context_builder = context_builder
        self.narrative = narrative
        self.frame_anchors = frame_anchors
        self.ledger = ledger
        self.model_roles = model_roles
        self.timeline = AuthoritativeTimelineStateEngine(database)

    # ------------------------------------------------------------- prepare
    async def prepare(
        self,
        project_id: str,
        *,
        previous_episode_id: str,
        continuation_mode: str,
        time_gap: str = "",
        new_location: str | None = None,
        guidance: str = "",
        regenerate: bool = False,
    ) -> dict[str, Any]:
        if continuation_mode not in CONTINUITY_CLASSES:
            raise EpisodeContinuationConflict(
                f"unknown continuation mode {continuation_mode!r}; "
                f"expected one of {sorted(CONTINUITY_CLASSES)}"
            )
        if continuation_mode != "CONTINUOUS" and not (time_gap.strip() or new_location):
            raise EpisodeContinuationConflict(
                "a discontinuous continuation must say what jumped: time_gap or new_location"
            )
        context = self.context_builder.build(
            project_id,
            previous_episode_id=previous_episode_id,
            continuation_mode=continuation_mode,
        )
        next_number = int(context["previous_episode"]["number"]) + 1
        with self.database.session() as session:
            stranger = session.scalar(
                select(Episode).where(
                    Episode.project_id == project_id,
                    Episode.episode_number == next_number,
                )
            )
            existing = session.scalar(
                select(EpisodeContinuation).where(
                    EpisodeContinuation.project_id == project_id,
                    EpisodeContinuation.previous_episode_id == previous_episode_id,
                    EpisodeContinuation.next_episode_number == next_number,
                )
            )
            if stranger is not None and (existing is None or existing.next_episode_id != stranger.id):
                raise EpisodeContinuationConflict(
                    f"episode {next_number} already exists and was not created by this continuation"
                )
            if existing is not None and not regenerate:
                return self._view(session, existing)
            if existing is not None and existing.status == "COMPILED":
                raise EpisodeContinuationConflict(
                    "the next episode is already compiled; edit it through the director surface"
                )

        brief, beats, reasoner = await self._propose(
            project_id, context, continuation_mode, time_gap, new_location, guidance
        )
        with self.database.session() as session:
            existing = session.scalar(
                select(EpisodeContinuation).where(
                    EpisodeContinuation.project_id == project_id,
                    EpisodeContinuation.previous_episode_id == previous_episode_id,
                    EpisodeContinuation.next_episode_number == next_number,
                )
            )
            if existing is None:
                existing = EpisodeContinuation(
                    project_id=project_id,
                    previous_episode_id=previous_episode_id,
                    next_episode_number=next_number,
                    continuation_mode=continuation_mode,
                    time_gap=time_gap,
                    new_location=new_location,
                    context_json=context,
                    context_hash=context_hash(context),
                    context_version=self.context_builder.version,
                    brief_json=brief,
                    beats_json=beats,
                    reasoner=reasoner,
                )
                session.add(existing)
            else:
                existing.revisions_json = [
                    *existing.revisions_json,
                    {
                        "revision": existing.revision,
                        "brief": existing.brief_json,
                        "beats": existing.beats_json,
                        "context_hash": existing.context_hash,
                    },
                ]
                existing.revision += 1
                existing.continuation_mode = continuation_mode
                existing.time_gap = time_gap
                existing.new_location = new_location
                existing.context_json = context
                existing.context_hash = context_hash(context)
                existing.context_version = self.context_builder.version
                existing.brief_json = brief
                existing.beats_json = beats
                existing.reasoner = reasoner
                existing.status = "BRIEF_PROPOSED"
            session.flush()
            return self._view(session, existing)

    async def _propose(
        self,
        project_id: str,
        context: dict[str, Any],
        mode: str,
        time_gap: str,
        new_location: str | None,
        guidance: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        brief, beats = self._deterministic_proposal(context, mode, time_gap, new_location, guidance)
        if self.model_roles is None:
            return brief, beats, "DETERMINISTIC"
        from model_registry_core import ModelRole
        from provider_sdk import ProviderError, ProviderTrustViolation

        try:
            execution = await self.model_roles.execute_chat(
                project_id,
                ModelRole.DIRECTOR,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are the series director planning the next episode. Answer JSON "
                            'only: {"premise": str, "beats": [{"intent": str, "summary": str}]}. '
                            "Advance the open obligations; never contradict established facts."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "continuation_context": {
                                    "ending": context["ending"]["output_state"],
                                    "open_obligations": context["narrative"]["open_obligations"],
                                    "mode": mode,
                                    "time_gap": time_gap,
                                    "new_location": new_location,
                                    "guidance": guidance,
                                }
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                parameters={"response_format": {"type": "json_object"}},
            )
            raw = _first_choice_json(execution.response)
        except (LookupError, ProviderError, ProviderTrustViolation, TypeError, ValueError):
            return brief, beats, "DETERMINISTIC"
        premise = raw.get("premise")
        if isinstance(premise, str) and premise.strip():
            brief["premise"] = premise.strip()[:800]
        model_beats = raw.get("beats")
        if isinstance(model_beats, list):
            # The model refines summaries only; the structural beat rows -
            # locations, characters, parseable shot actions - stay
            # deterministic so the compile cannot be broken by prose.
            for beat, refinement in zip(beats, model_beats, strict=False):
                summary = refinement.get("summary") if isinstance(refinement, dict) else None
                if isinstance(summary, str) and summary.strip():
                    beat["summary"] = summary.strip()[:400]
        return brief, beats, "MODEL:DIRECTOR"

    def _deterministic_proposal(
        self,
        context: dict[str, Any],
        mode: str,
        time_gap: str,
        new_location: str | None,
        guidance: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        ending = context["ending"]
        obligations: list[str] = list(context["narrative"]["open_obligations"])
        next_number = int(context["previous_episode"]["number"]) + 1
        protagonist = next(
            (
                character["name"]
                for character in context["characters"]
                if character.get("present_in_ending")
            ),
            next((character["name"] for character in context["characters"]), "Alex"),
        )
        if mode == "CONTINUOUS":
            location = str(ending["location"]["name"] or "the same place")
            time_of_day = "NIGHT" if "夜" in (ending.get("time_context") or "") else (
                str(ending.get("time_context") or "DAY").upper()
                if str(ending.get("time_context") or "").upper() in {"DAY", "NIGHT"}
                else "DAY"
            )
            opening_intent = "PICKUP"
            opening_shots = [
                {
                    "action": action_line(protagonist, "turn"),
                    "dialogue": None,
                    "shot_type": "MEDIUM",
                    "duration": 5.0,
                    "anchors": [f"character:{protagonist}", f"scene:{location}"],
                }
            ]
        else:
            location = str(new_location or ending["location"]["name"] or "a new place")
            time_of_day = "DAY"
            opening_intent = "RE_ESTABLISH"
            opening_shots = [
                {
                    "action": action_line(protagonist, "enter", place=location),
                    "dialogue": None,
                    "shot_type": "WIDE",
                    "duration": 5.0,
                    "anchors": [f"character:{protagonist}", f"scene:{location}"],
                }
            ]
        beats: list[dict[str, Any]] = [
            {
                "sequence": 1,
                "intent": opening_intent,
                "summary": (
                    f"Pick up exactly where episode {next_number - 1} ended"
                    if mode == "CONTINUOUS"
                    else f"{time_gap or 'Later'}, {location}"
                ),
                "location": location,
                "time": time_of_day,
                "characters": [protagonist],
                "shots": opening_shots,
            }
        ]
        sequence = 2
        for promise in obligations[:2]:
            beats.append(
                {
                    "sequence": sequence,
                    "intent": "ADVANCE_OBLIGATION",
                    "summary": f"Advance: {promise}"[:400],
                    "location": location,
                    "time": time_of_day,
                    "characters": [protagonist],
                    "shots": [
                        {
                            "action": action_line(protagonist, "pick_up"),
                            "dialogue": None,
                            "shot_type": "MEDIUM",
                            "duration": 5.0,
                            "anchors": [f"character:{protagonist}", f"scene:{location}"],
                        }
                    ],
                }
            )
            sequence += 1
        cliff_text = "还有更多真相" if _is_cjk(protagonist) else "There is more to this"
        beats.append(
            {
                "sequence": sequence,
                "intent": "CLIFFHANGER",
                "summary": f"New question that pulls into episode {next_number + 1}",
                "location": location,
                "time": time_of_day,
                "characters": [protagonist],
                "shots": [
                    {
                        "action": dialogue_line(protagonist, cliff_text),
                        "dialogue": cliff_text,
                        "shot_type": "DIALOGUE",
                        "duration": 6.0,
                        "anchors": [f"character:{protagonist}", f"scene:{location}"],
                    }
                ],
            }
        )
        brief = {
            "episode_number": next_number,
            "premise": (
                guidance.strip()
                or f"Continue the story: {'; '.join(obligations[:3]) or 'advance the protagonist'}"
            )[:800],
            "continuation_mode": mode,
            "time_gap": time_gap,
            "location": location,
            "carried_obligations": obligations,
            "characters": [character["name"] for character in context["characters"]],
            "continuity_classes": CONTINUITY_CLASSES[mode],
        }
        return brief, beats

    # ------------------------------------------------------------- confirm
    def confirm(
        self,
        continuation_id: str,
        *,
        actor: str,
        title: str | None = None,
        edited_beats: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(EpisodeContinuation, continuation_id)
            if row is None:
                raise LookupError("episode continuation not found")
            if row.status == "COMPILED" and row.next_episode_id:
                return self._view(session, row)
            if row.status not in {"BRIEF_PROPOSED", "CONFIRMED"}:
                raise EpisodeContinuationConflict(f"continuation is {row.status}")

            ending_shot = session.get(Shot, row.context_json["ending"]["shot_id"])
            if ending_shot is None:
                raise EpisodeContinuationConflict(
                    "the previous episode changed after this continuation was prepared; "
                    "prepare it again (regenerate=true)"
                )
            beats = [dict(beat) for beat in row.beats_json]
            if edited_beats is not None:
                by_sequence = {int(beat.get("sequence", 0)): beat for beat in edited_beats}
                for beat in beats:
                    edited = by_sequence.get(int(beat.get("sequence", 0)))
                    if edited is not None:
                        beat.update(edited)
                if row.continuation_mode == "CONTINUOUS":
                    expected = str(row.context_json["ending"]["location"]["name"] or "")
                    first_location = str(beats[0].get("location") or "")
                    if expected and first_location.casefold() != expected.casefold():
                        raise EpisodeContinuationConflict(
                            f"a CONTINUOUS pickup must stay in {expected!r}; "
                            "declare TIME_JUMP or LOCATION_CHANGE to move"
                        )
                row.beats_json = beats
            row.status = "CONFIRMED"
            row.confirmed_at = _now()
            row.confirmed_by = actor
            script, ordered_intents = render_script(beats)
            row.script_rendered = script
            project_id = row.project_id
            next_number = row.next_episode_number
            episode_id = row.next_episode_id
            if episode_id is None:
                episode = Episode(
                    project_id=project_id,
                    title=title or f"Episode {next_number}",
                    episode_number=next_number,
                    script_source=script,
                )
                session.add(episode)
                session.flush()
                episode_id = episode.id
                row.next_episode_id = episode_id
            else:
                episode = session.get(Episode, episode_id)
                if episode is None:
                    raise EpisodeContinuationConflict("continuation episode disappeared")
                episode.script_source = script
                # Recompiling replaces this episode's shots, and the previous
                # episode's tail still chains into the old first shot. Unlink
                # for the compile; _link_boundary re-establishes it below.
                previous_tail = session.get(Shot, row.context_json["ending"]["shot_id"])
                if previous_tail is not None:
                    previous_tail.next_shot_id = None
            session.flush()

        result = self.narrative.compile_episode(episode_id)
        shot_ids = list(result.shot_ids)
        if not shot_ids:
            raise EpisodeContinuationConflict("the continuation compiled to zero shots")
        self._link_boundary(continuation_id, first_shot_id=shot_ids[0])
        # The planner runs after the boundary exists, so the first shot is
        # planned as a pair against the previous episode's tail rather than as
        # an orphan first shot.
        self.frame_anchors.plan_episode(episode_id)
        try:
            apply_shot_intents(self.database, shot_ids, ordered_intents)
        except ShotIntentMismatch as exc:
            raise EpisodeContinuationConflict(str(exc)) from exc
        self._ledger_writes(continuation_id)

        with self.database.session() as session:
            row = session.get(EpisodeContinuation, continuation_id)
            row.status = "COMPILED"
            session.flush()
            return self._view(session, row)

    def _link_boundary(self, continuation_id: str, *, first_shot_id: str) -> None:
        """Wire the episode boundary the declared mode requires. Idempotent."""

        with self.database.session() as session:
            row = session.get(EpisodeContinuation, continuation_id)
            first_shot = session.get(Shot, first_shot_id)
            ending_shot_id = str(row.context_json["ending"]["shot_id"])
            ending_action = str(row.context_json["ending"]["output_state"].get("primary_action", ""))
            previous_shot = session.get(Shot, ending_shot_id)
            if first_shot is None or previous_shot is None:
                raise EpisodeContinuationConflict("boundary shots are missing")
            if previous_shot.next_shot_id not in {None, first_shot.id}:
                raise EpisodeContinuationConflict(
                    "the previous episode's ending already continues into another shot"
                )
            first_shot.previous_shot_id = previous_shot.id
            previous_shot.next_shot_id = first_shot.id
            mode = row.continuation_mode
            project_id = row.project_id
            time_gap = row.time_gap
            new_location = row.new_location
            session.flush()

        transition_type = {
            "CONTINUOUS": TimelineTransitionType.CONTINUOUS,
            "TIME_JUMP": TimelineTransitionType.TIME_JUMP,
            "LOCATION_CHANGE": TimelineTransitionType.LOCATION_CHANGE,
        }[mode]
        metadata: dict[str, Any] = {
            "source": "episode_continuation",
            "continuation_id": continuation_id,
            "continuity_classes": CONTINUITY_CLASSES[mode],
        }
        if time_gap:
            metadata["time_gap"] = time_gap
        if new_location:
            metadata["new_location"] = new_location
        self.timeline.set_transition(first_shot_id, transition_type, metadata=metadata)

        if mode == "CONTINUOUS":
            with self.database.session() as session:
                dependency_key = ShotDependency.natural_key(
                    ShotDependencyType.STATE_INHERITANCE.value,
                    source_shot_id=ending_shot_id,
                )
                exists = session.scalar(
                    select(ShotDependency.id).where(
                        ShotDependency.target_shot_id == first_shot_id,
                        ShotDependency.dependency_key == dependency_key,
                    )
                )
                if exists is None:
                    session.add(
                        ShotDependency(
                            project_id=project_id,
                            target_shot_id=first_shot_id,
                            source_shot_id=ending_shot_id,
                            dependency_type=ShotDependencyType.STATE_INHERITANCE.value,
                            summary=(
                                "continues the previous episode's ending state: " + ending_action
                            ),
                            origin=ShotDependencyOrigin.SCRIPT_COMPILER.value,
                            dependency_key=dependency_key,
                        )
                    )
                    session.flush()
                source_committed = (
                    session.get(Shot, ending_shot_id).status == ShotStatus.COMMITTED.value
                )
            if source_committed:
                # Committed truth propagates now, through the existing engine.
                self.timeline.propagate_shot(ending_shot_id)
            # An uncommitted ending is not propagated early: the engine only
            # propagates committed state, and the commit path already walks
            # next_shot links - which now cross the boundary - so the state
            # reaches the new episode the moment the ending commits.
        else:
            # The user's continuation decision *is* the reconciliation: the
            # compiler already built the new episode's opening state fresh for
            # the new time and place, so that state is reconciled explicitly
            # through the existing engine instead of leaving the shot stale.
            with self.database.session() as session:
                first_shot = session.get(Shot, first_shot_id)
                opening_state = session.get(TimelineState, first_shot.input_state_id)
                reconciled = dict(opening_state.state_json) if opening_state is not None else {}
            reason = (
                f"EPISODE_CONTINUATION_{mode}:"
                f"{time_gap or new_location or 'declared discontinuity'}"
            )
            self.timeline.reconcile_transition(first_shot_id, reconciled, reason=reason)

    def _ledger_writes(self, continuation_id: str) -> None:
        with self.database.session() as session:
            row = session.get(EpisodeContinuation, continuation_id)
            beats = list(row.beats_json)
            project_id = row.project_id
            episode_number = row.next_episode_number
        cliffhanger = next((beat for beat in beats if beat.get("intent") == "CLIFFHANGER"), None)
        if cliffhanger is None:
            return
        obligation_key = f"continuation:ep{episode_number}:cliffhanger"
        promise = str(cliffhanger.get("summary") or "resolve the episode cliffhanger")[:500]
        # `open_obligation` is idempotent on (project, obligation_key): a
        # confirm replay with the same promise returns the existing row, and a
        # replay carrying a *different* promise raises LedgerWriteConflict.
        # Nothing is caught here — a blanket except would fake a successful
        # replay for a write that actually conflicted.
        self.ledger.open_obligation(
            project_id,
            obligation_key=obligation_key,
            promise=promise,
            episode=episode_number,
            category="CLIFFHANGER",
        )

    # ---------------------------------------------------------------- reads
    def get(self, continuation_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = session.get(EpisodeContinuation, continuation_id)
            if row is None:
                raise LookupError("episode continuation not found")
            return self._view(session, row)

    def list_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.session() as session:
            return [
                self._view(session, row)
                for row in session.scalars(
                    select(EpisodeContinuation)
                    .where(EpisodeContinuation.project_id == project_id)
                    .order_by(EpisodeContinuation.next_episode_number)
                )
            ]

    def _view(self, session: Any, row: EpisodeContinuation) -> dict[str, Any]:
        compiled: dict[str, Any] | None = None
        if row.next_episode_id is not None:
            scene_count = session.scalar(
                select(func.count(Scene.id)).where(Scene.episode_id == row.next_episode_id)
            )
            shot_count = session.scalar(
                select(func.count(Shot.id))
                .join(Scene, Shot.scene_id == Scene.id)
                .where(Scene.episode_id == row.next_episode_id)
            )
            compiled = {
                "episode_id": row.next_episode_id,
                "scene_count": int(scene_count or 0),
                "shot_count": int(shot_count or 0),
            }
        return {
            "id": row.id,
            "project_id": row.project_id,
            "previous_episode_id": row.previous_episode_id,
            "next_episode_number": row.next_episode_number,
            "status": row.status,
            "continuation_mode": row.continuation_mode,
            "time_gap": row.time_gap,
            "new_location": row.new_location,
            "revision": row.revision,
            "reasoner": row.reasoner,
            "context": row.context_json,
            "context_hash": row.context_hash,
            "brief": row.brief_json,
            "beats": row.beats_json,
            "compiled": compiled,
        }


def _is_cjk(value: str) -> bool:
    return any("一" <= character <= "鿿" for character in value)


def _first_choice_json(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("chat response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("chat response is not a JSON object")
