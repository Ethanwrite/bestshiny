"""The series-level ledger: what is true, who may know it, and what is still owed.

`TimelineState` carries physical state and `CharacterStateVersion` carries
appearance and condition. Neither carries *knowledge*, so nothing previously
stopped a character in episode 40 from acting on something they learned in a
scene they were not in. Nor could anything answer "what does this series still
owe the viewer?", because an obligation is owed rather than similar and so is
invisible to embedding retrieval.

Both are explicit and append-only here. The cost is O(1) per shot and does not
grow with episode count, which is what keeps a 60-episode arc checkable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from platform_database import Database
from production_domain.models import (
    NarrativeDisclosure,
    NarrativeFact,
    NarrativeObligation,
)
from sqlalchemy import select

AUDIENCE = "AUDIENCE"


class KnowledgeViolation(ValueError):
    """A shot would let a holder act on a fact never disclosed to them."""


@dataclass(frozen=True)
class SeriesContext:
    """Everything episode N needs from episodes 1..N-1, at constant cost."""

    episode: int
    known_facts: dict[str, list[str]] = field(default_factory=dict)
    open_obligations: list[str] = field(default_factory=list)
    audience_only_facts: list[str] = field(default_factory=list)

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
    """Append-only series ledger. Nothing here is ever rewritten in place."""

    version = "narrative-ledger-v1"

    def __init__(self, database: Database):
        self.database = database

    def establish_fact(
        self,
        project_id: str,
        *,
        fact_key: str,
        summary: str,
        episode: int,
        shot_id: str | None = None,
        subject_character_ids: list[str] | None = None,
        disclose_to: list[str] | None = None,
    ) -> str:
        """Record that a fact became true, and who witnessed it becoming true."""

        with self.database.session() as session:
            existing = session.scalar(
                select(NarrativeFact).where(
                    NarrativeFact.project_id == project_id,
                    NarrativeFact.fact_key == fact_key,
                )
            )
            if existing is not None:
                raise ValueError(f"narrative fact already established: {fact_key}")
            fact = NarrativeFact(
                project_id=project_id,
                fact_key=fact_key,
                summary=summary,
                fact_hash=_fact_hash(project_id, fact_key, summary),
                established_episode=episode,
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
        shot_id: str | None = None,
        channel: str = "ON_SCREEN",
    ) -> None:
        """Record that a holder learned an already-established fact."""

        with self.database.session() as session:
            fact = session.scalar(
                select(NarrativeFact).where(
                    NarrativeFact.project_id == project_id,
                    NarrativeFact.fact_key == fact_key,
                )
            )
            if fact is None:
                raise LookupError(f"narrative fact not established: {fact_key}")
            if episode < fact.established_episode:
                raise ValueError(
                    f"{holder_key} cannot learn {fact_key} in episode {episode}; "
                    f"it is established in episode {fact.established_episode}"
                )
            already = session.scalar(
                select(NarrativeDisclosure).where(
                    NarrativeDisclosure.fact_id == fact.id,
                    NarrativeDisclosure.holder_key == holder_key,
                )
            )
            if already is not None:
                return
            session.add(
                NarrativeDisclosure(
                    project_id=project_id,
                    fact_id=fact.id,
                    holder_key=holder_key,
                    disclosed_episode=episode,
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
        shot_id: str | None = None,
    ) -> str:
        with self.database.session() as session:
            obligation = NarrativeObligation(
                project_id=project_id,
                obligation_key=obligation_key,
                promise=promise,
                opened_episode=episode,
                opened_shot_id=shot_id,
                status="OPEN",
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
        shot_id: str | None = None,
        reason: str = "",
        abandoned: bool = False,
    ) -> None:
        with self.database.session() as session:
            obligation = session.scalar(
                select(NarrativeObligation).where(
                    NarrativeObligation.project_id == project_id,
                    NarrativeObligation.obligation_key == obligation_key,
                )
            )
            if obligation is None:
                raise LookupError(f"narrative obligation not found: {obligation_key}")
            if obligation.status != "OPEN":
                return
            obligation.status = "ABANDONED" if abandoned else "SETTLED"
            obligation.settled_episode = episode
            obligation.settled_shot_id = shot_id
            obligation.settled_reason = reason
            session.flush()

    def may_know(self, project_id: str, *, holder_key: str, fact_key: str, episode: int) -> bool:
        """Is this holder entitled to act on this fact by this episode?"""

        with self.database.session() as session:
            row = session.scalar(
                select(NarrativeDisclosure.id)
                .join(NarrativeFact, NarrativeFact.id == NarrativeDisclosure.fact_id)
                .where(
                    NarrativeFact.project_id == project_id,
                    NarrativeFact.fact_key == fact_key,
                    NarrativeDisclosure.holder_key == holder_key,
                    NarrativeDisclosure.disclosed_episode <= episode,
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
    ) -> None:
        """Fail closed when a shot would use knowledge a character lacks.

        The audience knowing a fact is never sufficient: that gap is exactly
        what dramatic irony is, and collapsing it is the classic long-form break.
        """

        undisclosed = [
            key
            for key in fact_keys
            if not self.may_know(project_id, holder_key=holder_key, fact_key=key, episode=episode)
        ]
        if undisclosed:
            raise KnowledgeViolation(
                f"{holder_key} cannot act on {sorted(undisclosed)} in episode {episode}: "
                "never disclosed to them"
            )

    def series_context(
        self,
        project_id: str,
        *,
        episode: int,
        holder_keys: list[str] | None = None,
    ) -> SeriesContext:
        """Constant-cost series state for one episode: heads, not history."""

        holders = list(dict.fromkeys([AUDIENCE, *(holder_keys or [])]))
        known: dict[str, list[str]] = {}
        audience_only: list[str] = []
        with self.database.session() as session:
            rows = session.execute(
                select(NarrativeDisclosure.holder_key, NarrativeFact.fact_key, NarrativeFact.summary)
                .join(NarrativeFact, NarrativeFact.id == NarrativeDisclosure.fact_id)
                .where(
                    NarrativeFact.project_id == project_id,
                    NarrativeDisclosure.disclosed_episode <= episode,
                    NarrativeDisclosure.holder_key.in_(holders),
                )
                .order_by(NarrativeFact.established_episode, NarrativeFact.fact_key)
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
                        NarrativeObligation.status == "OPEN",
                        NarrativeObligation.opened_episode <= episode,
                    )
                    .order_by(NarrativeObligation.opened_episode)
                )
            )
        return SeriesContext(
            episode=episode,
            known_facts=known,
            open_obligations=obligations,
            audience_only_facts=audience_only,
        )
