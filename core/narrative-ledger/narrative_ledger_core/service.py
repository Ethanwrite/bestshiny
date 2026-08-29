"""The series-level ledger: what is true, who may know it, and what is still owed.

`TimelineState` carries physical state and `CharacterStateVersion` carries
appearance and condition. Neither carries *knowledge*, so nothing previously
stopped a character in episode 40 from acting on something they learned in a
scene they were not in. Nor could anything answer "what does this series still
owe the viewer?", because an obligation is owed rather than similar and so is
invisible to embedding retrieval.

Both are explicit and append-only here. The cost is O(1) per shot and does not
grow with episode count, which is what keeps a 60-episode arc checkable.

Every entry is ordered by its **complete narrative position** —
(episode_number, scene.sequence, shot.sequence) — not by episode alone. A fact
disclosed in a later shot of an episode is therefore invisible to that
episode's earlier shots, and settlement records *where* it happened rather
than overwriting the only evidence that the obligation was ever open: a read
at an earlier position still sees it open, which is what makes regenerating a
historical shot read the ledger as it stood then.

Writes are idempotent by natural key: replaying the same write returns the
existing row, replaying a *different* write under the same key raises. Nothing
here catches an exception to fake a successful replay.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from platform_database import Database
from production_domain.models import (
    Episode,
    NarrativeDisclosure,
    NarrativeFact,
    NarrativeObligation,
    Scene,
    Shot,
    ShotDependency,
    ShotNarrativeEffect,
    utcnow,
)
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

AUDIENCE = "AUDIENCE"

#: Sorts after every real scene/shot sequence: the "end of episode" sentinel
#: used when a caller supplies only an episode number. Real sequences are
#: 1-based and bounded far below this.
_SEQUENCE_END = 2_000_000_000


class KnowledgeViolation(ValueError):
    """A shot would let a holder act on a fact never disclosed to them."""


class LedgerWriteConflict(ValueError):
    """A replayed write does not match the row already on the ledger.

    Raised instead of silently keeping the first write: a conflicting replay
    means two different narrative claims share one key, which is an authoring
    error that must surface, never an idempotent success.
    """


class SettlementConflict(LedgerWriteConflict):
    """An obligation was already settled somewhere else."""


@dataclass(frozen=True, order=True)
class NarrativePosition:
    """A complete point in series order: episode, scene sequence, shot sequence.

    Ordering is lexicographic, which is exactly narrative order. Sequence 0 is
    the pre-position "start of episode" used by episode-granular legacy rows
    and by writes that genuinely have no shot (a series-level decision); real
    shots are 1-based.
    """

    episode: int
    scene_sequence: int = 0
    shot_sequence: int = 0

    @classmethod
    def episode_end(cls, episode: int) -> NarrativePosition:
        """The position after every shot of ``episode`` — whole-episode reads."""

        return cls(episode, _SEQUENCE_END, _SEQUENCE_END)

    @classmethod
    def of_shot_in_session(cls, session: Session, shot: Shot) -> NarrativePosition:
        scene = session.get(Scene, shot.scene_id)
        episode = session.get(Episode, scene.episode_id) if scene else None
        if scene is None or episode is None:
            raise LookupError("shot narrative position could not be resolved")
        return cls(episode.episode_number, scene.sequence, shot.sequence)

    def as_dict(self) -> dict[str, int]:
        return {
            "episode": self.episode,
            "scene_sequence": self.scene_sequence,
            "shot_sequence": self.shot_sequence,
        }


def _at_or_before(episode_col, scene_col, shot_col, position: NarrativePosition):  # type: ignore[no-untyped-def]
    """SQL for (episode, scene, shot) <= position, lexicographically."""

    return or_(
        episode_col < position.episode,
        and_(episode_col == position.episode, scene_col < position.scene_sequence),
        and_(
            episode_col == position.episode,
            scene_col == position.scene_sequence,
            shot_col <= position.shot_sequence,
        ),
    )


@dataclass(frozen=True)
class SeriesContext:
    """Everything a shot needs from every strictly-earlier position, at constant cost."""

    episode: int
    known_facts: dict[str, list[str]] = field(default_factory=dict)
    open_obligations: list[str] = field(default_factory=list)
    audience_only_facts: list[str] = field(default_factory=list)
    position: NarrativePosition | None = None

    def continuity_facts(self) -> list[dict[str, Any]]:
        """Render as PromptCompilerInput continuity facts.

        Only established, disclosed material is emitted. The compiler contract
        allows nothing into `continuity_assertions` that is not supplied here,
        so an undisclosed fact cannot reach a prompt by accident.
        """

        facts: list[dict[str, Any]] = []
        for holder, summaries in sorted(self.known_facts.items()):
            for summary in summaries:
                facts.append({"name": "known_fact", "holder": holder, "value": summary})
        for promise in self.open_obligations:
            facts.append({"name": "open_obligation", "value": promise})
        return facts


def _fact_hash(project_id: str, fact_key: str, summary: str) -> str:
    encoded = json.dumps(
        {"project_id": project_id, "fact_key": fact_key, "summary": summary},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class NarrativeLedgerService:
    """Append-only series ledger. Nothing here is ever rewritten in place.

    Every write has an ``*_in_session`` form so the candidate-commit
    transaction can apply a shot's declared effects atomically with the commit
    itself; the session-less form wraps one write in one transaction.
    """

    version = "narrative-ledger-v2-positions"

    def __init__(self, database: Database):
        self.database = database

    # ----------------------------------------------------------------- writes
    def establish_fact(
        self,
        project_id: str,
        *,
        fact_key: str,
        summary: str,
        episode: int,
        scene_sequence: int = 0,
        shot_sequence: int = 0,
        shot_id: str | None = None,
        subject_character_ids: list[str] | None = None,
        disclose_to: list[str] | None = None,
    ) -> str:
        try:
            with self.database.session() as session:
                return self.establish_fact_in_session(
                    session,
                    project_id,
                    fact_key=fact_key,
                    summary=summary,
                    episode=episode,
                    scene_sequence=scene_sequence,
                    shot_sequence=shot_sequence,
                    shot_id=shot_id,
                    subject_character_ids=subject_character_ids,
                    disclose_to=disclose_to,
                )
        except IntegrityError:
            # A concurrent writer won the unique (project, fact_key) race.
            # Re-read and hold the replay to the same verification as the
            # sequential path — never report success for a different fact.
            with self.database.session() as session:
                return self.establish_fact_in_session(
                    session,
                    project_id,
                    fact_key=fact_key,
                    summary=summary,
                    episode=episode,
                    scene_sequence=scene_sequence,
                    shot_sequence=shot_sequence,
                    shot_id=shot_id,
                    subject_character_ids=subject_character_ids,
                    disclose_to=disclose_to,
                )

    def establish_fact_in_session(
        self,
        session: Session,
        project_id: str,
        *,
        fact_key: str,
        summary: str,
        episode: int,
        scene_sequence: int = 0,
        shot_sequence: int = 0,
        shot_id: str | None = None,
        subject_character_ids: list[str] | None = None,
        disclose_to: list[str] | None = None,
    ) -> str:
        """Record that a fact became true, and who witnessed it becoming true.

        Idempotent: replaying the identical establishment returns the existing
        fact id; a different summary or position under the same key raises
        :class:`LedgerWriteConflict`.
        """

        fact_hash = _fact_hash(project_id, fact_key, summary)
        existing = session.scalar(
            select(NarrativeFact).where(
                NarrativeFact.project_id == project_id,
                NarrativeFact.fact_key == fact_key,
            )
        )
        if existing is not None:
            same = (
                existing.fact_hash == fact_hash
                and existing.established_episode == episode
                and existing.established_scene_sequence == scene_sequence
                and existing.established_shot_sequence == shot_sequence
            )
            if not same:
                raise LedgerWriteConflict(
                    f"narrative fact already established with different content or "
                    f"position: {fact_key}"
                )
            for holder in dict.fromkeys(disclose_to or [AUDIENCE]):
                self.disclose_in_session(
                    session,
                    project_id,
                    fact_key=fact_key,
                    holder_key=holder,
                    episode=episode,
                    scene_sequence=scene_sequence,
                    shot_sequence=shot_sequence,
                    shot_id=shot_id,
                )
            return existing.id
        fact = NarrativeFact(
            project_id=project_id,
            fact_key=fact_key,
            summary=summary,
            fact_hash=fact_hash,
            established_episode=episode,
            established_scene_sequence=scene_sequence,
            established_shot_sequence=shot_sequence,
            established_shot_id=shot_id,
            subject_character_ids=list(subject_character_ids or []),
        )
        session.add(fact)
        session.flush()
        fact_id = fact.id
        for holder in dict.fromkeys(disclose_to or [AUDIENCE]):
            session.add(
                NarrativeDisclosure(
                    project_id=project_id,
                    fact_id=fact_id,
                    holder_key=holder,
                    disclosed_episode=episode,
                    disclosed_scene_sequence=scene_sequence,
                    disclosed_shot_sequence=shot_sequence,
                    disclosed_shot_id=shot_id,
                )
            )
        session.flush()
        return fact_id

    def disclose(
        self,
        project_id: str,
        *,
        fact_key: str,
        holder_key: str,
        episode: int,
        scene_sequence: int = 0,
        shot_sequence: int = 0,
        shot_id: str | None = None,
        channel: str = "ON_SCREEN",
    ) -> None:
        try:
            with self.database.session() as session:
                self.disclose_in_session(
                    session,
                    project_id,
                    fact_key=fact_key,
                    holder_key=holder_key,
                    episode=episode,
                    scene_sequence=scene_sequence,
                    shot_sequence=shot_sequence,
                    shot_id=shot_id,
                    channel=channel,
                )
        except IntegrityError:
            with self.database.session() as session:
                self.disclose_in_session(
                    session,
                    project_id,
                    fact_key=fact_key,
                    holder_key=holder_key,
                    episode=episode,
                    scene_sequence=scene_sequence,
                    shot_sequence=shot_sequence,
                    shot_id=shot_id,
                    channel=channel,
                )

    def disclose_in_session(
        self,
        session: Session,
        project_id: str,
        *,
        fact_key: str,
        holder_key: str,
        episode: int,
        scene_sequence: int = 0,
        shot_sequence: int = 0,
        shot_id: str | None = None,
        channel: str = "ON_SCREEN",
    ) -> None:
        """Record that a holder learned an already-established fact.

        The stored disclosure position is the *earliest* one: re-disclosing at
        a later position is a no-op (they already knew), and a commit that
        discloses at an earlier position than currently recorded moves the
        record earlier, keeping an audit trail of the move. ``may_know`` needs
        exactly the earliest position to be correct.
        """

        fact = session.scalar(
            select(NarrativeFact).where(
                NarrativeFact.project_id == project_id,
                NarrativeFact.fact_key == fact_key,
            )
        )
        if fact is None:
            raise LookupError(f"narrative fact not established: {fact_key}")
        position = NarrativePosition(episode, scene_sequence, shot_sequence)
        established = NarrativePosition(
            fact.established_episode,
            fact.established_scene_sequence,
            fact.established_shot_sequence,
        )
        if position < established:
            raise LedgerWriteConflict(
                f"{holder_key} cannot learn {fact_key} at {position.as_dict()}; "
                f"it is established at {established.as_dict()}"
            )
        already = session.scalar(
            select(NarrativeDisclosure).where(
                NarrativeDisclosure.fact_id == fact.id,
                NarrativeDisclosure.holder_key == holder_key,
            )
        )
        if already is not None:
            recorded = NarrativePosition(
                already.disclosed_episode,
                already.disclosed_scene_sequence,
                already.disclosed_shot_sequence,
            )
            if position >= recorded:
                return
            moves = list((already.metadata_json or {}).get("position_moves", []))
            moves.append(
                {
                    "from": recorded.as_dict(),
                    "to": position.as_dict(),
                    "shot_id": shot_id,
                }
            )
            already.disclosed_episode = position.episode
            already.disclosed_scene_sequence = position.scene_sequence
            already.disclosed_shot_sequence = position.shot_sequence
            already.disclosed_shot_id = shot_id
            already.metadata_json = {**(already.metadata_json or {}), "position_moves": moves}
            session.flush()
            return
        session.add(
            NarrativeDisclosure(
                project_id=project_id,
                fact_id=fact.id,
                holder_key=holder_key,
                disclosed_episode=position.episode,
                disclosed_scene_sequence=position.scene_sequence,
                disclosed_shot_sequence=position.shot_sequence,
                disclosed_shot_id=shot_id,
                channel=channel,
            )
        )
        session.flush()

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
    ) -> str:
        try:
            with self.database.session() as session:
                return self.open_obligation_in_session(
                    session,
                    project_id,
                    obligation_key=obligation_key,
                    promise=promise,
                    episode=episode,
                    scene_sequence=scene_sequence,
                    shot_sequence=shot_sequence,
                    shot_id=shot_id,
                    category=category,
                )
        except IntegrityError:
            with self.database.session() as session:
                return self.open_obligation_in_session(
                    session,
                    project_id,
                    obligation_key=obligation_key,
                    promise=promise,
                    episode=episode,
                    scene_sequence=scene_sequence,
                    shot_sequence=shot_sequence,
                    shot_id=shot_id,
                    category=category,
                )

    def open_obligation_in_session(
        self,
        session: Session,
        project_id: str,
        *,
        obligation_key: str,
        promise: str,
        episode: int,
        scene_sequence: int = 0,
        shot_sequence: int = 0,
        shot_id: str | None = None,
        category: str = "GENERIC",
    ) -> str:
        """Open an obligation, idempotently on (project, obligation_key)."""

        existing = session.scalar(
            select(NarrativeObligation).where(
                NarrativeObligation.project_id == project_id,
                NarrativeObligation.obligation_key == obligation_key,
            )
        )
        if existing is not None:
            same = (
                existing.promise == promise
                and existing.opened_episode == episode
                and existing.opened_scene_sequence == scene_sequence
                and existing.opened_shot_sequence == shot_sequence
            )
            if not same:
                raise LedgerWriteConflict(
                    f"narrative obligation already opened with different content or "
                    f"position: {obligation_key}"
                )
            return existing.id
        obligation = NarrativeObligation(
            project_id=project_id,
            obligation_key=obligation_key,
            promise=promise,
            opened_episode=episode,
            opened_scene_sequence=scene_sequence,
            opened_shot_sequence=shot_sequence,
            opened_shot_id=shot_id,
            status="OPEN",
            metadata_json={"category": category},
        )
        session.add(obligation)
        session.flush()
        return obligation.id

    def settle_obligation(
        self,
        project_id: str,
        *,
        obligation_key: str,
        episode: int,
        scene_sequence: int = 0,
        shot_sequence: int = 0,
        shot_id: str | None = None,
        reason: str = "",
        abandoned: bool = False,
    ) -> None:
        with self.database.session() as session:
            self.settle_obligation_in_session(
                session,
                project_id,
                obligation_key=obligation_key,
                episode=episode,
                scene_sequence=scene_sequence,
                shot_sequence=shot_sequence,
                shot_id=shot_id,
                reason=reason,
                abandoned=abandoned,
            )

    def settle_obligation_in_session(
        self,
        session: Session,
        project_id: str,
        *,
        obligation_key: str,
        episode: int,
        scene_sequence: int = 0,
        shot_sequence: int = 0,
        shot_id: str | None = None,
        reason: str = "",
        abandoned: bool = False,
    ) -> None:
        """Settle (or abandon) an open obligation at a position.

        The settlement position is recorded rather than replacing the open
        record, so a historical read earlier than it still sees the obligation
        open. Replaying the same settlement is a no-op; a settlement that was
        already made *elsewhere* raises :class:`SettlementConflict` — it is
        never reported as success.
        """

        obligation = session.scalar(
            select(NarrativeObligation).where(
                NarrativeObligation.project_id == project_id,
                NarrativeObligation.obligation_key == obligation_key,
            )
        )
        if obligation is None:
            raise LookupError(f"narrative obligation not found: {obligation_key}")
        position = NarrativePosition(episode, scene_sequence, shot_sequence)
        opened = NarrativePosition(
            obligation.opened_episode,
            obligation.opened_scene_sequence,
            obligation.opened_shot_sequence,
        )
        target_status = "ABANDONED" if abandoned else "SETTLED"
        if obligation.status != "OPEN":
            same_place = (
                obligation.status == target_status
                and obligation.settled_episode == episode
                and obligation.settled_scene_sequence == scene_sequence
                and obligation.settled_shot_sequence == shot_sequence
                and (shot_id is None or obligation.settled_shot_id == shot_id)
            )
            if same_place:
                return
            raise SettlementConflict(
                f"obligation {obligation_key} was already {obligation.status} at "
                f"episode {obligation.settled_episode} scene "
                f"{obligation.settled_scene_sequence} shot "
                f"{obligation.settled_shot_sequence}"
            )
        if position < opened:
            raise LedgerWriteConflict(
                f"obligation {obligation_key} cannot settle at {position.as_dict()} "
                f"before it was opened at {opened.as_dict()}"
            )
        # Compare-and-set on status so two concurrent settlements produce one
        # winner and one SettlementConflict rather than a silent overwrite.
        claimed = session.execute(
            update(NarrativeObligation)
            .where(
                NarrativeObligation.id == obligation.id,
                NarrativeObligation.status == "OPEN",
            )
            .values(
                status=target_status,
                settled_episode=episode,
                settled_scene_sequence=scene_sequence,
                settled_shot_sequence=shot_sequence,
                settled_shot_id=shot_id,
                settled_reason=reason,
            )
        )
        rowcount = int(getattr(claimed, "rowcount", 0) or 0)
        if rowcount != 1:
            session.expire(obligation)
            self.settle_obligation_in_session(
                session,
                project_id,
                obligation_key=obligation_key,
                episode=episode,
                scene_sequence=scene_sequence,
                shot_sequence=shot_sequence,
                shot_id=shot_id,
                reason=reason,
                abandoned=abandoned,
            )
            return
        session.flush()

    # ------------------------------------------------------------------ reads
    def may_know(
        self,
        project_id: str,
        *,
        holder_key: str,
        fact_key: str,
        episode: int,
        scene_sequence: int | None = None,
        shot_sequence: int | None = None,
    ) -> bool:
        """Is this holder entitled to act on this fact at this position?

        With only an episode supplied the check is episode-granular (anything
        disclosed anywhere in that episode counts), preserving the historical
        reading. With the complete position it is exact: a disclosure in a
        later shot of the same episode does not entitle an earlier shot.
        """

        position = (
            NarrativePosition(episode, scene_sequence, shot_sequence)
            if scene_sequence is not None and shot_sequence is not None
            else NarrativePosition.episode_end(episode)
        )
        with self.database.session() as session:
            row = session.scalar(
                select(NarrativeDisclosure.id)
                .join(NarrativeFact, NarrativeFact.id == NarrativeDisclosure.fact_id)
                .where(
                    NarrativeFact.project_id == project_id,
                    NarrativeFact.fact_key == fact_key,
                    NarrativeDisclosure.holder_key == holder_key,
                    _at_or_before(
                        NarrativeDisclosure.disclosed_episode,
                        NarrativeDisclosure.disclosed_scene_sequence,
                        NarrativeDisclosure.disclosed_shot_sequence,
                        position,
                    ),
                )
            )
            return row is not None

    def assert_may_act_on(
        self,
        project_id: str,
        *,
        holder_key: str,
        fact_keys: list[str],
        episode: int,
        scene_sequence: int | None = None,
        shot_sequence: int | None = None,
    ) -> None:
        """Fail closed when a shot would use knowledge a character lacks.

        The audience knowing a fact is never sufficient: that gap is exactly
        what dramatic irony is, and collapsing it is the classic long-form break.
        """

        undisclosed = [
            key
            for key in fact_keys
            if not self.may_know(
                project_id,
                holder_key=holder_key,
                fact_key=key,
                episode=episode,
                scene_sequence=scene_sequence,
                shot_sequence=shot_sequence,
            )
        ]
        if undisclosed:
            raise KnowledgeViolation(
                f"{holder_key} cannot act on {sorted(undisclosed)} in episode {episode}: "
                "never disclosed to them by this position"
            )

    def series_context(
        self,
        project_id: str,
        *,
        episode: int,
        scene_sequence: int | None = None,
        shot_sequence: int | None = None,
        holder_keys: list[str] | None = None,
    ) -> SeriesContext:
        """Series state visible at one position: heads, not history.

        This is a *time slice*: an obligation settled after the position is
        still open here, and a disclosure made after it is invisible — which is
        what regenerating a historical shot must read. Called with only an
        episode, the slice is the end of that episode (the whole-episode
        reading every earlier caller relied on).
        """

        position = (
            NarrativePosition(episode, scene_sequence, shot_sequence)
            if scene_sequence is not None and shot_sequence is not None
            else NarrativePosition.episode_end(episode)
        )
        holders = list(dict.fromkeys([AUDIENCE, *(holder_keys or [])]))
        known: dict[str, list[str]] = {}
        audience_only: list[str] = []
        with self.database.session() as session:
            rows = session.execute(
                select(NarrativeDisclosure.holder_key, NarrativeFact.fact_key, NarrativeFact.summary)
                .join(NarrativeFact, NarrativeFact.id == NarrativeDisclosure.fact_id)
                .where(
                    NarrativeFact.project_id == project_id,
                    _at_or_before(
                        NarrativeDisclosure.disclosed_episode,
                        NarrativeDisclosure.disclosed_scene_sequence,
                        NarrativeDisclosure.disclosed_shot_sequence,
                        position,
                    ),
                    NarrativeDisclosure.holder_key.in_(holders),
                )
                .order_by(
                    NarrativeFact.established_episode,
                    NarrativeFact.established_scene_sequence,
                    NarrativeFact.established_shot_sequence,
                    NarrativeFact.fact_key,
                )
            ).all()
            character_facts: set[str] = set()
            audience_facts: dict[str, str] = {}
            for holder_key, fact_key, summary in rows:
                known.setdefault(holder_key, []).append(summary)
                if holder_key == AUDIENCE:
                    audience_facts[fact_key] = summary
                else:
                    character_facts.add(fact_key)
            audience_only = [
                summary for key, summary in audience_facts.items() if key not in character_facts
            ]
            obligations = list(
                session.scalars(
                    select(NarrativeObligation.promise)
                    .where(
                        NarrativeObligation.project_id == project_id,
                        _at_or_before(
                            NarrativeObligation.opened_episode,
                            NarrativeObligation.opened_scene_sequence,
                            NarrativeObligation.opened_shot_sequence,
                            position,
                        ),
                        # Open *at this position*: either never settled, or
                        # settled strictly after it. The global status column
                        # alone would erase history.
                        or_(
                            NarrativeObligation.settled_episode.is_(None),
                            ~_at_or_before(
                                NarrativeObligation.settled_episode,
                                NarrativeObligation.settled_scene_sequence,
                                NarrativeObligation.settled_shot_sequence,
                                position,
                            ),
                        ),
                    )
                    .order_by(
                        NarrativeObligation.opened_episode,
                        NarrativeObligation.opened_scene_sequence,
                        NarrativeObligation.opened_shot_sequence,
                    )
                )
            )
        return SeriesContext(
            episode=episode,
            known_facts=known,
            open_obligations=obligations,
            audience_only_facts=audience_only,
            position=position,
        )

    # ------------------------------------------------------------------ fence
    def context_fence_in_session(
        self,
        session: Session,
        project_id: str,
        *,
        position: NarrativePosition,
        shot_id: str | None = None,
    ) -> str:
        """A digest of everything the ledger showed a generation at a position.

        Covers the visible slice — facts, disclosures and obligations at or
        before the position, including where each was settled — plus the
        target shot's declared dependencies and their referents' identity
        (source shot commit status included). A candidate stores this at
        creation; commit recomputes and refuses on mismatch, so a candidate
        generated from an expired narrative context cannot become canon.
        """

        parts: list[Any] = []
        facts = session.execute(
            select(
                NarrativeFact.fact_key,
                NarrativeFact.fact_hash,
                NarrativeFact.established_episode,
                NarrativeFact.established_scene_sequence,
                NarrativeFact.established_shot_sequence,
            )
            .where(
                NarrativeFact.project_id == project_id,
                _at_or_before(
                    NarrativeFact.established_episode,
                    NarrativeFact.established_scene_sequence,
                    NarrativeFact.established_shot_sequence,
                    position,
                ),
            )
            .order_by(NarrativeFact.fact_key)
        ).all()
        parts.append(["facts", [list(row) for row in facts]])
        disclosures = session.execute(
            select(
                NarrativeFact.fact_key,
                NarrativeDisclosure.holder_key,
                NarrativeDisclosure.disclosed_episode,
                NarrativeDisclosure.disclosed_scene_sequence,
                NarrativeDisclosure.disclosed_shot_sequence,
            )
            .join(NarrativeFact, NarrativeFact.id == NarrativeDisclosure.fact_id)
            .where(
                NarrativeFact.project_id == project_id,
                _at_or_before(
                    NarrativeDisclosure.disclosed_episode,
                    NarrativeDisclosure.disclosed_scene_sequence,
                    NarrativeDisclosure.disclosed_shot_sequence,
                    position,
                ),
            )
            .order_by(NarrativeFact.fact_key, NarrativeDisclosure.holder_key)
        ).all()
        parts.append(["disclosures", [list(row) for row in disclosures]])
        obligations = session.execute(
            select(
                NarrativeObligation.obligation_key,
                NarrativeObligation.promise,
                NarrativeObligation.status,
                NarrativeObligation.opened_episode,
                NarrativeObligation.opened_scene_sequence,
                NarrativeObligation.opened_shot_sequence,
                NarrativeObligation.settled_episode,
                NarrativeObligation.settled_scene_sequence,
                NarrativeObligation.settled_shot_sequence,
            )
            .where(
                NarrativeObligation.project_id == project_id,
                _at_or_before(
                    NarrativeObligation.opened_episode,
                    NarrativeObligation.opened_scene_sequence,
                    NarrativeObligation.opened_shot_sequence,
                    position,
                ),
            )
            .order_by(NarrativeObligation.obligation_key)
        ).all()
        parts.append(["obligations", [list(row) for row in obligations]])
        if shot_id is not None:
            # The dependency ROW SET only — which requirements are declared.
            # Source-shot state staleness is the authoritative timeline fence's
            # job, and referent liveness is re-checked by resolving the
            # dependencies again at commit; duplicating either here would make
            # the normal commit-in-order flow read as an expired context.
            dependency_rows = session.execute(
                select(
                    ShotDependency.dependency_key,
                    ShotDependency.dependency_type,
                    ShotDependency.source_shot_id,
                    ShotDependency.fact_key,
                    ShotDependency.obligation_key,
                )
                .where(ShotDependency.target_shot_id == shot_id)
                .order_by(ShotDependency.dependency_key)
            ).all()
            parts.append(["dependencies", [list(row) for row in dependency_rows]])
            effects = session.execute(
                select(
                    ShotNarrativeEffect.effect_key,
                    ShotNarrativeEffect.effect_type,
                    ShotNarrativeEffect.fact_key,
                    ShotNarrativeEffect.obligation_key,
                    ShotNarrativeEffect.holder_key,
                    ShotNarrativeEffect.summary,
                )
                .where(ShotNarrativeEffect.shot_id == shot_id)
                .order_by(ShotNarrativeEffect.effect_key)
            ).all()
            parts.append(["effects", [list(row) for row in effects]])
        encoded = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def context_fence_for_shot(self, shot_id: str) -> dict[str, Any]:
        """The fence and position for one shot, as commit metadata."""

        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if shot is None:
                raise LookupError("shot not found")
            scene = session.get(Scene, shot.scene_id)
            episode = session.get(Episode, scene.episode_id) if scene else None
            if scene is None or episode is None:
                raise LookupError("shot narrative position could not be resolved")
            position = NarrativePosition(episode.episode_number, scene.sequence, shot.sequence)
            fence = self.context_fence_in_session(
                session, episode.project_id, position=position, shot_id=shot_id
            )
        return {
            "fence": fence,
            "position": position.as_dict(),
            "ledger_version": self.version,
        }

    # ---------------------------------------------------- commit-time effects
    def apply_shot_effects_in_session(
        self,
        session: Session,
        shot: Shot,
        *,
        candidate_id: str,
    ) -> list[str]:
        """Apply every declared effect of a committing shot, exactly once.

        Runs inside the candidate-commit transaction. Returns the applied
        effect keys (for the commit decision record). Replays verify: an
        already-applied effect is checked against the ledger rather than
        re-written, and any conflict raises — the commit fails rather than
        pretending the ledger agrees.
        """

        position = NarrativePosition.of_shot_in_session(session, shot)
        scene = session.get(Scene, shot.scene_id)
        episode = session.get(Episode, scene.episode_id)
        project_id = episode.project_id
        applied: list[str] = []
        effects = list(
            session.scalars(
                select(ShotNarrativeEffect)
                .where(ShotNarrativeEffect.shot_id == shot.id)
                .order_by(ShotNarrativeEffect.created_at, ShotNarrativeEffect.id)
            )
        )
        for effect in effects:
            if effect.effect_type == "ESTABLISH_FACT":
                self.establish_fact_in_session(
                    session,
                    project_id,
                    fact_key=effect.fact_key or "",
                    summary=effect.summary,
                    episode=position.episode,
                    scene_sequence=position.scene_sequence,
                    shot_sequence=position.shot_sequence,
                    shot_id=shot.id,
                    subject_character_ids=list(effect.subject_character_ids or []),
                    disclose_to=list(effect.disclose_to or []) or None,
                )
            elif effect.effect_type == "DISCLOSE_FACT":
                holders = list(effect.disclose_to or [])
                if effect.holder_key:
                    holders.append(effect.holder_key)
                for holder in dict.fromkeys(holders):
                    self.disclose_in_session(
                        session,
                        project_id,
                        fact_key=effect.fact_key or "",
                        holder_key=holder,
                        episode=position.episode,
                        scene_sequence=position.scene_sequence,
                        shot_sequence=position.shot_sequence,
                        shot_id=shot.id,
                        channel=effect.channel,
                    )
            elif effect.effect_type == "OPEN_OBLIGATION":
                self.open_obligation_in_session(
                    session,
                    project_id,
                    obligation_key=effect.obligation_key or "",
                    promise=effect.summary,
                    episode=position.episode,
                    scene_sequence=position.scene_sequence,
                    shot_sequence=position.shot_sequence,
                    shot_id=shot.id,
                    category=str((effect.metadata_json or {}).get("category", "GENERIC")),
                )
            elif effect.effect_type == "SETTLE_OBLIGATION":
                self.settle_obligation_in_session(
                    session,
                    project_id,
                    obligation_key=effect.obligation_key or "",
                    episode=position.episode,
                    scene_sequence=position.scene_sequence,
                    shot_sequence=position.shot_sequence,
                    shot_id=shot.id,
                    reason=effect.summary or f"settled by shot {shot.id}",
                )
            else:  # pragma: no cover - constrained by the table check
                raise LedgerWriteConflict(f"unknown effect type: {effect.effect_type}")
            if effect.applied_at is None:
                effect.applied_at = utcnow()
                effect.applied_candidate_id = candidate_id
            applied.append(effect.effect_key)
        if applied:
            session.flush()
        return applied
