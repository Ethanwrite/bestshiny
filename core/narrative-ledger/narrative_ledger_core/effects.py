"""Declared narrative effects of shots: what committing each shot does to the ledger.

Declarations come from script compilation (explicit directives), manual
editing, or upper-level planning (creative director / episode continuation).
They are validated and idempotent here; the ledger writes they imply happen in
:meth:`narrative_ledger_core.service.NarrativeLedgerService.apply_shot_effects_in_session`,
inside the candidate-commit transaction.
"""

from __future__ import annotations

from typing import Any

from platform_database import Database
from production_domain.models import (
    Episode,
    Scene,
    Shot,
    ShotNarrativeEffect,
    ShotNarrativeEffectOrigin,
    ShotNarrativeEffectType,
)
from sqlalchemy import select
from sqlalchemy.orm import Session


class ShotNarrativeEffectError(ValueError):
    """An effect declaration that can never be valid."""


class ShotNarrativeEffectService:
    """Declare and list the ledger consequences a shot will have when it commits."""

    version = "shot-narrative-effect-v1"

    def __init__(self, database: Database):
        self.database = database

    def declare(
        self,
        project_id: str,
        *,
        shot_id: str,
        effect_type: str,
        fact_key: str | None = None,
        obligation_key: str | None = None,
        holder_key: str | None = None,
        summary: str = "",
        channel: str = "ON_SCREEN",
        disclose_to: list[str] | None = None,
        subject_character_ids: list[str] | None = None,
        origin: str = ShotNarrativeEffectOrigin.MANUAL.value,
        metadata: dict[str, Any] | None = None,
    ) -> ShotNarrativeEffect:
        with self.database.session() as session:
            row = self.declare_in_session(
                session,
                project_id,
                shot_id=shot_id,
                effect_type=effect_type,
                fact_key=fact_key,
                obligation_key=obligation_key,
                holder_key=holder_key,
                summary=summary,
                channel=channel,
                disclose_to=disclose_to,
                subject_character_ids=subject_character_ids,
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
        shot_id: str,
        effect_type: str,
        fact_key: str | None = None,
        obligation_key: str | None = None,
        holder_key: str | None = None,
        summary: str = "",
        channel: str = "ON_SCREEN",
        disclose_to: list[str] | None = None,
        subject_character_ids: list[str] | None = None,
        origin: str = ShotNarrativeEffectOrigin.MANUAL.value,
        metadata: dict[str, Any] | None = None,
    ) -> ShotNarrativeEffect:
        """Validate and append one effect declaration, idempotent on its key."""

        try:
            kind = ShotNarrativeEffectType(effect_type)
        except ValueError as exc:
            raise ShotNarrativeEffectError(f"unknown effect type: {effect_type}") from exc
        if kind in {ShotNarrativeEffectType.ESTABLISH_FACT, ShotNarrativeEffectType.DISCLOSE_FACT}:
            if not fact_key:
                raise ShotNarrativeEffectError(f"{kind.value} requires fact_key")
        if kind in {
            ShotNarrativeEffectType.OPEN_OBLIGATION,
            ShotNarrativeEffectType.SETTLE_OBLIGATION,
        }:
            if not obligation_key:
                raise ShotNarrativeEffectError(f"{kind.value} requires obligation_key")
        if kind is ShotNarrativeEffectType.ESTABLISH_FACT and not summary:
            raise ShotNarrativeEffectError("ESTABLISH_FACT requires a summary")
        if kind is ShotNarrativeEffectType.OPEN_OBLIGATION and not summary:
            raise ShotNarrativeEffectError("OPEN_OBLIGATION requires a promise summary")
        if kind is ShotNarrativeEffectType.DISCLOSE_FACT and not (
            holder_key or (disclose_to or [])
        ):
            raise ShotNarrativeEffectError("DISCLOSE_FACT requires at least one holder")

        shot = session.get(Shot, shot_id)
        if shot is None:
            raise LookupError("shot not found")
        scene = session.get(Scene, shot.scene_id)
        episode = session.get(Episode, scene.episode_id) if scene else None
        if scene is None or episode is None:
            raise LookupError("shot narrative position could not be resolved")
        if episode.project_id != project_id:
            raise ShotNarrativeEffectError("shot belongs to a different project")

        effect_key = ShotNarrativeEffect.natural_key(
            kind.value,
            fact_key=fact_key,
            obligation_key=obligation_key,
            holder_key=holder_key,
        )
        existing = session.scalar(
            select(ShotNarrativeEffect).where(
                ShotNarrativeEffect.shot_id == shot_id,
                ShotNarrativeEffect.effect_key == effect_key,
            )
        )
        if existing is not None:
            return existing
        row = ShotNarrativeEffect(
            project_id=project_id,
            shot_id=shot_id,
            effect_type=kind.value,
            episode_number=episode.episode_number,
            scene_sequence=scene.sequence,
            shot_sequence=shot.sequence,
            fact_key=fact_key,
            obligation_key=obligation_key,
            holder_key=holder_key,
            summary=summary,
            channel=channel,
            disclose_to=list(disclose_to or []),
            subject_character_ids=list(subject_character_ids or []),
            origin=ShotNarrativeEffectOrigin(origin).value,
            effect_key=effect_key,
            metadata_json=dict(metadata or {}),
        )
        session.add(row)
        session.flush([row])
        return row

    def list_for(self, shot_id: str) -> list[ShotNarrativeEffect]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(ShotNarrativeEffect)
                    .where(ShotNarrativeEffect.shot_id == shot_id)
                    .order_by(ShotNarrativeEffect.created_at, ShotNarrativeEffect.id)
                )
            )

    def remove(self, project_id: str, *, effect_id: str) -> None:
        """Withdraw a declaration made in error — only while it is unapplied."""

        with self.database.session() as session:
            row = session.get(ShotNarrativeEffect, effect_id)
            if row is None or row.project_id != project_id:
                raise LookupError("shot narrative effect not found")
            if row.applied_at is not None:
                raise ShotNarrativeEffectError(
                    "effect was already applied to the ledger by a committed candidate; "
                    "it is canon and cannot be withdrawn"
                )
            session.delete(row)
            session.flush()
