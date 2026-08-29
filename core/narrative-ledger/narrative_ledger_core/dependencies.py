"""Explicit shot dependencies: what a shot requires, not what it resembles.

The narrative ledger records what the series owes the viewer; this module
records what one shot owes another. A payoff shares no vocabulary with its
setup, so which earlier beat matters can never be a similarity decision —
dependencies are declared rows, resolved before generation, and a dependency
that cannot be resolved refuses generation (``REVIEW_REQUIRED``) rather than
silently degrading to similarity retrieval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from platform_database import Database
from production_domain.models import (
    Episode,
    NarrativeFact,
    NarrativeObligation,
    Scene,
    Shot,
    ShotDependency,
    ShotDependencyOrigin,
    ShotDependencyType,
    ShotNarrativeEffect,
    ShotStatus,
    TimelineState,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

#: Provenance label carried by every forced context segment built from a
#: declared dependency. Similarity segments carry ``SIMILARITY`` instead, so a
#: reader of any assembled context can tell why each piece is present.
EXPLICIT_DEPENDENCY = "EXPLICIT_DEPENDENCY"

#: Whether each dependency type requires its source shot to be COMMITTED
#: before the depending shot may generate. This is the explicit decision the
#: payload's ``committed`` flag used to leave implicit:
#:
#: - ``STATE_INHERITANCE`` inherits the *authoritative timeline plan*, which
#:   exists from compilation and is guarded by the timeline fence and the
#:   downstream-stale propagation — requiring commit would serialize batch
#:   scene production for no added truth, so commit is NOT required.
#: - ``FORESHADOWING``, ``FACT_REVELATION`` and ``OBLIGATION_FULFILLMENT``
#:   quote *produced canon* (what the earlier shot actually showed), so their
#:   source shot must be COMMITTED. A declared, audited override —
#:   ``metadata.allow_uncommitted_source: true`` on the dependency row — may
#:   relax it for a shot the author accepts regenerating if the source changes.
#:
#: An uncommitted source under a requiring type is an unresolved dependency:
#: the shot goes to USER_REVIEW_REQUIRED, never to generation with
#: ``committed: false`` quietly riding in the payload.
COMMITTED_SOURCE_POLICY: dict[str, bool] = {
    ShotDependencyType.STATE_INHERITANCE.value: False,
    ShotDependencyType.FORESHADOWING.value: True,
    ShotDependencyType.FACT_REVELATION.value: True,
    ShotDependencyType.OBLIGATION_FULFILLMENT.value: True,
}


class ShotDependencyError(ValueError):
    """A dependency declaration that can never be valid."""


class ShotDependencyUnresolved(ValueError):
    """An explicit dependency exists and its referent cannot be resolved.

    ``ValueError`` on purpose: the generate route maps it to ``409`` alongside
    the other plan conflicts. The caller must move the shot to
    ``USER_REVIEW_REQUIRED`` — never fall back to similarity retrieval.
    """

    def __init__(self, shot_id: str, reason_codes: list[str]):
        self.shot_id = shot_id
        self.reason_codes = tuple(dict.fromkeys(reason_codes))
        super().__init__(
            f"shot {shot_id} has unresolved explicit dependencies "
            f"({', '.join(self.reason_codes)}); review required"
        )


@dataclass(frozen=True)
class DependencyContext:
    """One resolved dependency, ready to be forced into generation context."""

    dependency_id: str
    dependency_type: str
    summary: str
    source_shot_id: str | None = None
    fact_key: str | None = None
    obligation_key: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    source_reason: str = EXPLICIT_DEPENDENCY


def _narrative_order(session: Session, shot: Shot) -> tuple[int, int, int]:
    scene = session.get(Scene, shot.scene_id)
    episode = session.get(Episode, scene.episode_id) if scene else None
    if scene is None or episode is None:
        raise LookupError("shot narrative position could not be resolved")
    return (episode.episode_number, scene.sequence, shot.sequence)


def _shot_project_id(session: Session, shot: Shot) -> str:
    scene = session.get(Scene, shot.scene_id)
    episode = session.get(Episode, scene.episode_id) if scene else None
    if episode is None:
        raise LookupError("shot project could not be resolved")
    return episode.project_id


def _bounded(value: str, limit: int = 500) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _strictly_before(
    position: tuple[int, int, int], target: tuple[int, int, int]
) -> bool:
    return position < target


def _pending_effect_before(
    session: Session,
    project_id: str,
    *,
    effect_type: str,
    target_order: tuple[int, int, int],
    fact_key: str | None = None,
    obligation_key: str | None = None,
) -> ShotNarrativeEffect | None:
    """A declared-but-not-yet-canon ledger effect at an earlier position.

    Facts and obligations reach the ledger only when their declaring shot
    commits; before that, the declaration row is the evidence the referent is
    *planned*. Declaration accepts it; generation, by default, does not.
    """

    query = select(ShotNarrativeEffect).where(
        ShotNarrativeEffect.project_id == project_id,
        ShotNarrativeEffect.effect_type == effect_type,
    )
    if fact_key is not None:
        query = query.where(ShotNarrativeEffect.fact_key == fact_key)
    if obligation_key is not None:
        query = query.where(ShotNarrativeEffect.obligation_key == obligation_key)
    for effect in session.scalars(
        query.order_by(
            ShotNarrativeEffect.episode_number,
            ShotNarrativeEffect.scene_sequence,
            ShotNarrativeEffect.shot_sequence,
        )
    ):
        order = (effect.episode_number, effect.scene_sequence, effect.shot_sequence)
        if _strictly_before(order, target_order):
            return effect
    return None


class ShotDependencyService:
    """Declare, list and resolve explicit shot dependencies."""

    version = "shot-dependency-v1"

    def __init__(self, database: Database):
        self.database = database

    # ------------------------------------------------------------------ write
    def declare(
        self,
        project_id: str,
        *,
        target_shot_id: str,
        dependency_type: str,
        source_shot_id: str | None = None,
        fact_key: str | None = None,
        obligation_key: str | None = None,
        summary: str = "",
        origin: str = ShotDependencyOrigin.MANUAL.value,
        metadata: dict[str, Any] | None = None,
    ) -> ShotDependency:
        with self.database.session() as session:
            row = self.declare_in_session(
                session,
                project_id,
                target_shot_id=target_shot_id,
                dependency_type=dependency_type,
                source_shot_id=source_shot_id,
                fact_key=fact_key,
                obligation_key=obligation_key,
                summary=summary,
                origin=origin,
                metadata=metadata,
            )
            session.flush()
            return row

    def declare_in_session(
        self,
        session: Session,
        project_id: str,
        *,
        target_shot_id: str,
        dependency_type: str,
        source_shot_id: str | None = None,
        fact_key: str | None = None,
        obligation_key: str | None = None,
        summary: str = "",
        origin: str = ShotDependencyOrigin.MANUAL.value,
        metadata: dict[str, Any] | None = None,
    ) -> ShotDependency:
        """Validate and append one dependency inside the caller's transaction.

        Idempotent on the natural key: redeclaring the same (type, referent)
        for a target returns the existing row unchanged, append-only style.
        """

        try:
            kind = ShotDependencyType(dependency_type)
        except ValueError as exc:
            raise ShotDependencyError(f"unknown dependency type: {dependency_type}") from exc
        if kind is ShotDependencyType.FACT_REVELATION and not fact_key:
            raise ShotDependencyError("FACT_REVELATION requires fact_key")
        if kind is ShotDependencyType.OBLIGATION_FULFILLMENT and not obligation_key:
            raise ShotDependencyError("OBLIGATION_FULFILLMENT requires obligation_key")
        if kind is ShotDependencyType.STATE_INHERITANCE and not source_shot_id:
            raise ShotDependencyError("STATE_INHERITANCE requires source_shot_id")
        if not (source_shot_id or fact_key or obligation_key):
            raise ShotDependencyError(
                "a dependency requires a referent: source_shot_id, fact_key or obligation_key"
            )

        target = session.get(Shot, target_shot_id)
        if target is None:
            raise LookupError("target shot not found")
        if _shot_project_id(session, target) != project_id:
            raise ShotDependencyError("target shot belongs to a different project")
        if source_shot_id:
            source = session.get(Shot, source_shot_id)
            if source is None:
                raise LookupError("source shot not found")
            if _shot_project_id(session, source) != project_id:
                raise ShotDependencyError("source shot belongs to a different project")
            if _narrative_order(session, source) >= _narrative_order(session, target):
                raise ShotDependencyError(
                    "a shot may only depend on earlier narrative material; "
                    "the source shot is not earlier than the target"
                )
        target_order = _narrative_order(session, target)
        if fact_key:
            fact = session.scalar(
                select(NarrativeFact).where(
                    NarrativeFact.project_id == project_id,
                    NarrativeFact.fact_key == fact_key,
                )
            )
            if fact is None and (
                _pending_effect_before(
                    session,
                    project_id,
                    effect_type="ESTABLISH_FACT",
                    target_order=target_order,
                    fact_key=fact_key,
                )
                is None
            ):
                raise LookupError(f"narrative fact not established: {fact_key}")
        if obligation_key:
            obligation = session.scalar(
                select(NarrativeObligation).where(
                    NarrativeObligation.project_id == project_id,
                    NarrativeObligation.obligation_key == obligation_key,
                )
            )
            if obligation is None and (
                _pending_effect_before(
                    session,
                    project_id,
                    effect_type="OPEN_OBLIGATION",
                    target_order=target_order,
                    obligation_key=obligation_key,
                )
                is None
            ):
                raise LookupError(f"narrative obligation not found: {obligation_key}")

        dependency_key = ShotDependency.natural_key(
            kind.value,
            source_shot_id=source_shot_id,
            fact_key=fact_key,
            obligation_key=obligation_key,
        )
        existing = session.scalar(
            select(ShotDependency).where(
                ShotDependency.target_shot_id == target_shot_id,
                ShotDependency.dependency_key == dependency_key,
            )
        )
        if existing is not None:
            return existing
        row = ShotDependency(
            project_id=project_id,
            target_shot_id=target_shot_id,
            source_shot_id=source_shot_id,
            dependency_type=kind.value,
            fact_key=fact_key,
            obligation_key=obligation_key,
            summary=summary,
            origin=ShotDependencyOrigin(origin).value,
            dependency_key=dependency_key,
            metadata_json=dict(metadata or {}),
        )
        session.add(row)
        session.flush([row])
        return row

    def remove(self, project_id: str, *, dependency_id: str) -> None:
        """Manual editing includes withdrawing a dependency declared in error."""

        with self.database.session() as session:
            row = session.get(ShotDependency, dependency_id)
            if row is None or row.project_id != project_id:
                raise LookupError("shot dependency not found")
            session.delete(row)
            session.flush()

    # ------------------------------------------------------------------- read
    def list_for(self, target_shot_id: str) -> list[ShotDependency]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(ShotDependency)
                    .where(ShotDependency.target_shot_id == target_shot_id)
                    .order_by(ShotDependency.created_at, ShotDependency.id)
                )
            )

    def resolve_for_generation(self, shot_id: str) -> list[DependencyContext]:
        """Stage one of retrieval: force every declared dependency, or refuse.

        Every declared dependency must resolve to real material. Any failure
        raises :class:`ShotDependencyUnresolved` carrying one reason code per
        failure — the caller marks the shot ``USER_REVIEW_REQUIRED`` and must
        not continue to similarity retrieval as if nothing were owed.
        """

        with self.database.session() as session:
            return self.resolve_for_generation_in_session(session, shot_id)

    def resolve_for_generation_in_session(
        self, session: Session, shot_id: str
    ) -> list[DependencyContext]:
        """The resolution itself, inside the caller's transaction.

        The candidate-commit path re-runs this in the commit transaction so a
        dependency that stopped resolving between generation and commit —
        an obligation settled elsewhere, a fact withdrawn — refuses the
        commit atomically.
        """

        contexts: list[DependencyContext] = []
        unresolved: list[str] = []
        shot = session.get(Shot, shot_id)
        if shot is None:
            raise LookupError("shot not found")
        project_id = _shot_project_id(session, shot)
        target_order = _narrative_order(session, shot)
        rows = list(
            session.scalars(
                select(ShotDependency)
                .where(ShotDependency.target_shot_id == shot_id)
                .order_by(ShotDependency.created_at, ShotDependency.id)
            )
        )
        for row in rows:
            payload: dict[str, Any] = {}
            reasons: list[str] = []
            metadata = dict(row.metadata_json or {})
            if row.source_shot_id:
                source = session.get(Shot, row.source_shot_id)
                if source is None:
                    reasons.append(f"DEPENDENCY_SOURCE_SHOT_MISSING:{row.id}")
                else:
                    output_state = (
                        session.get(TimelineState, source.output_state_id)
                        if source.output_state_id
                        else None
                    )
                    if output_state is None:
                        reasons.append(f"DEPENDENCY_SOURCE_STATE_MISSING:{row.id}")
                    else:
                        source_committed = source.status == ShotStatus.COMMITTED.value
                        commit_required = COMMITTED_SOURCE_POLICY.get(
                            row.dependency_type, True
                        ) and not bool(metadata.get("allow_uncommitted_source"))
                        if commit_required and not source_committed:
                            # The explicit determination: this dependency
                            # type quotes produced canon, the source is not
                            # canon yet, and no declared override exists.
                            # Review — not a `committed: false` shrug.
                            reasons.append(
                                f"DEPENDENCY_SOURCE_NOT_COMMITTED:{row.source_shot_id}"
                            )
                        else:
                            state_json = dict(output_state.state_json or {})
                            payload["source_shot"] = {
                                "shot_id": source.id,
                                "prompt": _bounded(source.user_prompt or source.prompt),
                                "committed": source_committed,
                                "committed_requirement": (
                                    "REQUIRED"
                                    if COMMITTED_SOURCE_POLICY.get(row.dependency_type, True)
                                    else "NOT_REQUIRED_STATE_PLAN"
                                ),
                                **(
                                    {"uncommitted_source_allowed_by": "DEPENDENCY_METADATA"}
                                    if not source_committed
                                    and bool(metadata.get("allow_uncommitted_source"))
                                    else {}
                                ),
                                "output_video_asset_id": source.output_video_asset_id,
                                "end_frame_asset_id": source.end_frame_asset_id,
                                "narrative_facts": [
                                    item.get("fact") if isinstance(item, dict) else item
                                    for item in list(state_json.get("narrative_facts") or [])[-5:]
                                ],
                                "characters": sorted(
                                    (state_json.get("characters") or {}).keys()
                                    if isinstance(state_json.get("characters"), dict)
                                    else []
                                ),
                            }
            if row.fact_key:
                fact = session.scalar(
                    select(NarrativeFact).where(
                        NarrativeFact.project_id == project_id,
                        NarrativeFact.fact_key == row.fact_key,
                    )
                )
                if fact is None:
                    pending = _pending_effect_before(
                        session,
                        project_id,
                        effect_type="ESTABLISH_FACT",
                        target_order=target_order,
                        fact_key=row.fact_key,
                    )
                    if pending is None:
                        reasons.append(f"DEPENDENCY_FACT_UNKNOWN:{row.fact_key}")
                    elif not bool(metadata.get("allow_uncommitted_source")):
                        # Declared on an earlier shot, but that shot has
                        # not committed, so the fact is not canon yet.
                        reasons.append(f"DEPENDENCY_FACT_NOT_CANON:{row.fact_key}")
                    else:
                        payload["fact"] = {
                            "fact_key": pending.fact_key,
                            "summary": _bounded(pending.summary),
                            "canon": False,
                            "pending_shot_id": pending.shot_id,
                            "established_position": {
                                "episode": pending.episode_number,
                                "scene_sequence": pending.scene_sequence,
                                "shot_sequence": pending.shot_sequence,
                            },
                        }
                else:
                    fact_order = (
                        fact.established_episode,
                        fact.established_scene_sequence,
                        fact.established_shot_sequence,
                    )
                    if not _strictly_before(fact_order, target_order):
                        # The complete position, not just the episode: a
                        # fact established by a later shot of this same
                        # episode is future material here.
                        reasons.append(f"DEPENDENCY_FACT_FROM_FUTURE:{row.fact_key}")
                    else:
                        payload["fact"] = {
                            "fact_key": fact.fact_key,
                            "summary": _bounded(fact.summary),
                            "canon": True,
                            "established_episode": fact.established_episode,
                            "established_position": {
                                "episode": fact.established_episode,
                                "scene_sequence": fact.established_scene_sequence,
                                "shot_sequence": fact.established_shot_sequence,
                            },
                            "established_shot_id": fact.established_shot_id,
                        }
            if row.obligation_key:
                obligation = session.scalar(
                    select(NarrativeObligation).where(
                        NarrativeObligation.project_id == project_id,
                        NarrativeObligation.obligation_key == row.obligation_key,
                    )
                )
                if obligation is None:
                    pending = _pending_effect_before(
                        session,
                        project_id,
                        effect_type="OPEN_OBLIGATION",
                        target_order=target_order,
                        obligation_key=row.obligation_key,
                    )
                    if pending is None:
                        reasons.append(f"DEPENDENCY_OBLIGATION_UNKNOWN:{row.obligation_key}")
                    elif not bool(metadata.get("allow_uncommitted_source")):
                        reasons.append(
                            f"DEPENDENCY_OBLIGATION_NOT_CANON:{row.obligation_key}"
                        )
                    else:
                        payload["obligation"] = {
                            "obligation_key": pending.obligation_key,
                            "promise": _bounded(pending.summary),
                            "status": "PENDING_COMMIT",
                            "canon": False,
                            "pending_shot_id": pending.shot_id,
                        }
                else:
                    opened_order = (
                        obligation.opened_episode,
                        obligation.opened_scene_sequence,
                        obligation.opened_shot_sequence,
                    )
                    if not _strictly_before(opened_order, target_order):
                        # Fulfilment must come after the promise: an
                        # obligation opened at or after this shot's
                        # position cannot be settled by it.
                        reasons.append(
                            f"DEPENDENCY_OBLIGATION_FROM_FUTURE:{row.obligation_key}"
                        )
                    elif obligation.status == "ABANDONED":
                        reasons.append(
                            f"DEPENDENCY_OBLIGATION_ABANDONED:{row.obligation_key}"
                        )
                    elif obligation.status == "SETTLED" and obligation.settled_shot_id not in {
                        None,
                        shot_id,
                    }:
                        settled_order = (
                            obligation.settled_episode,
                            obligation.settled_scene_sequence,
                            obligation.settled_shot_sequence,
                        )
                        if _strictly_before(settled_order, target_order):
                            reasons.append(
                                f"DEPENDENCY_OBLIGATION_ALREADY_SETTLED:{row.obligation_key}"
                            )
                        else:
                            # Historical regeneration sees the obligation as
                            # it was at this shot, not its later present-day
                            # settlement state.
                            payload["obligation"] = {
                                "obligation_key": obligation.obligation_key,
                                "promise": _bounded(obligation.promise),
                                "status": "OPEN",
                                "canon": True,
                                "opened_episode": obligation.opened_episode,
                                "opened_position": {
                                    "episode": obligation.opened_episode,
                                    "scene_sequence": obligation.opened_scene_sequence,
                                    "shot_sequence": obligation.opened_shot_sequence,
                                },
                            }
                    else:
                        payload["obligation"] = {
                            "obligation_key": obligation.obligation_key,
                            "promise": _bounded(obligation.promise),
                            "status": obligation.status,
                            "canon": True,
                            "opened_episode": obligation.opened_episode,
                            "opened_position": {
                                "episode": obligation.opened_episode,
                                "scene_sequence": obligation.opened_scene_sequence,
                                "shot_sequence": obligation.opened_shot_sequence,
                            },
                        }
            if reasons:
                unresolved.extend(reasons)
                continue
            contexts.append(
                DependencyContext(
                    dependency_id=row.id,
                    dependency_type=row.dependency_type,
                    summary=_bounded(row.summary),
                    source_shot_id=row.source_shot_id,
                    fact_key=row.fact_key,
                    obligation_key=row.obligation_key,
                    payload=payload,
                )
            )
        if unresolved:
            raise ShotDependencyUnresolved(shot_id, unresolved)
        return contexts
