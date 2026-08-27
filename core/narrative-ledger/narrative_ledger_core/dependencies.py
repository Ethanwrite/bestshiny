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
    ShotStatus,
    TimelineState,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

#: Provenance label carried by every forced context segment built from a
#: declared dependency. Similarity segments carry ``SIMILARITY`` instead, so a
#: reader of any assembled context can tell why each piece is present.
EXPLICIT_DEPENDENCY = "EXPLICIT_DEPENDENCY"


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
        if fact_key:
            fact = session.scalar(
                select(NarrativeFact).where(
                    NarrativeFact.project_id == project_id,
                    NarrativeFact.fact_key == fact_key,
                )
            )
            if fact is None:
                raise LookupError(f"narrative fact not established: {fact_key}")
        if obligation_key:
            obligation = session.scalar(
                select(NarrativeObligation).where(
                    NarrativeObligation.project_id == project_id,
                    NarrativeObligation.obligation_key == obligation_key,
                )
            )
            if obligation is None:
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

        contexts: list[DependencyContext] = []
        unresolved: list[str] = []
        with self.database.session() as session:
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
                            state_json = dict(output_state.state_json or {})
                            payload["source_shot"] = {
                                "shot_id": source.id,
                                "prompt": _bounded(source.user_prompt or source.prompt),
                                "committed": source.status == ShotStatus.COMMITTED.value,
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
                        reasons.append(f"DEPENDENCY_FACT_UNKNOWN:{row.fact_key}")
                    elif fact.established_episode > target_order[0]:
                        reasons.append(f"DEPENDENCY_FACT_FROM_FUTURE:{row.fact_key}")
                    else:
                        payload["fact"] = {
                            "fact_key": fact.fact_key,
                            "summary": _bounded(fact.summary),
                            "established_episode": fact.established_episode,
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
                        reasons.append(f"DEPENDENCY_OBLIGATION_UNKNOWN:{row.obligation_key}")
                    elif obligation.status == "ABANDONED":
                        reasons.append(f"DEPENDENCY_OBLIGATION_ABANDONED:{row.obligation_key}")
                    elif obligation.status == "SETTLED" and obligation.settled_shot_id not in {
                        None,
                        shot_id,
                    }:
                        reasons.append(f"DEPENDENCY_OBLIGATION_ALREADY_SETTLED:{row.obligation_key}")
                    else:
                        payload["obligation"] = {
                            "obligation_key": obligation.obligation_key,
                            "promise": _bounded(obligation.promise),
                            "status": obligation.status,
                            "opened_episode": obligation.opened_episode,
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
