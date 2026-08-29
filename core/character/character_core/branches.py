"""Timeline branch lifecycle: register, use, merge, retire, sweep — never lose history.

``timeline_scope_key`` names a narrative branch (a dream, a flashback, an
alternate line). The strings used to be the whole mechanism; this service
gives each branch identity and a lifecycle:

- **Registered** when its transition is created (see
  ``AuthoritativeTimelineStateEngine``), with a kind, its parent scope and
  the shot it forked from. A dream or flashback with no parent is invalid by
  construction.
- **Merged** only by explicit declaration: the caller names exactly which
  state paths may be written back, and the merge captures a manifest of
  those values from the branch's character heads. Dream states cannot merge
  into main unless the caller explicitly opts in — a dream leaking into
  canon by default is precisely the corruption branches exist to prevent.
  The manifest is a record, not a mutation: applying it to main-timeline
  heads goes through the same audited, human-confirmed character-state
  machinery every other state change uses.
- **Retired / abandoned** safely: the branch stops accepting state writes,
  history stays readable, and replaying the same retirement is a no-op.
- **Swept**: branches that nothing ever referenced and nothing has touched
  inside the idle window are closed as ABANDONED — closed, not deleted.
- **Purged** (physically deleted) only when closed *and* unreferenced: any
  surviving CharacterStateVersion, head, delta or transition on the scope
  refuses the purge, because those rows are audit history and the branch
  row is what anchors them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from platform_database import Database
from production_domain.models import (
    CharacterStateDelta,
    CharacterStateHead,
    CharacterStateVersion,
    TimelineBranch,
    TimelineTransition,
    utcnow,
)
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

MAIN_SCOPE = "main"

_KIND_BY_PREFIX = {
    "dream": "DREAM",
    "flashback": "FLASHBACK",
    "flash_forward": "FLASH_FORWARD",
}


class TimelineBranchError(ValueError):
    """A branch operation that can never be valid."""


class TimelineBranchConflict(RuntimeError):
    """The branch moved concurrently; exactly one caller wins a transition."""


class TimelineBranchReferenced(RuntimeError):
    """Audit history still references the branch; physical deletion is refused."""


@dataclass(frozen=True)
class BranchSweepResult:
    examined: int = 0
    abandoned: list[str] = field(default_factory=list)
    kept_referenced: int = 0
    kept_recent: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "abandoned": self.abandoned,
            "abandoned_count": len(self.abandoned),
            "kept_referenced": self.kept_referenced,
            "kept_recent": self.kept_recent,
        }


def kind_for_scope_key(scope_key: str) -> str:
    if scope_key == MAIN_SCOPE:
        return "MAIN"
    prefix = scope_key.split(":", 1)[0].lower()
    return _KIND_BY_PREFIX.get(prefix, "ALTERNATE")


def ensure_branch_in_session(
    session: Session,
    *,
    project_id: str,
    scope_key: str,
    branch_kind: str | None = None,
    parent_scope_key: str | None = None,
    fork_shot_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> TimelineBranch:
    """Idempotently register one branch inside the caller's transaction."""

    scope = scope_key.strip()
    if not scope:
        raise TimelineBranchError("a branch requires a scope key")
    existing = session.scalar(
        select(TimelineBranch).where(
            TimelineBranch.project_id == project_id,
            TimelineBranch.scope_key == scope,
        )
    )
    if existing is not None:
        return existing
    kind = branch_kind or kind_for_scope_key(scope)
    parent = parent_scope_key
    if kind != "MAIN" and not parent:
        parent = MAIN_SCOPE
    row = TimelineBranch(
        project_id=project_id,
        scope_key=scope,
        branch_kind=kind,
        status="ACTIVE",
        parent_scope_key=None if kind == "MAIN" else parent,
        fork_shot_id=fork_shot_id,
        last_used_at=utcnow(),
        metadata_json=dict(metadata or {}),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        winner = session.scalar(
            select(TimelineBranch).where(
                TimelineBranch.project_id == project_id,
                TimelineBranch.scope_key == scope,
            )
        )
        if winner is None:  # pragma: no cover - the conflict implies a winner
            raise
        return winner
    return row


def assert_branch_writable_in_session(
    session: Session, *, project_id: str, scope_key: str
) -> None:
    """Refuse a character-state write into a closed branch.

    A scope with no branch row is a legacy branch and stays writable — the
    lifecycle governs what it knows about, it does not invent refusals for
    history that predates it.
    """

    row = session.scalar(
        select(TimelineBranch.status).where(
            TimelineBranch.project_id == project_id,
            TimelineBranch.scope_key == scope_key,
        )
    )
    if row is not None and row != "ACTIVE":
        raise TimelineBranchError(
            f"timeline branch {scope_key!r} is {row} and no longer accepts state writes"
        )


def _dotted_get(state: dict[str, Any], path: str) -> tuple[bool, Any]:
    node: Any = state
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


class TimelineBranchService:
    version = "timeline-branch-v1"

    def __init__(self, database: Database):
        self.database = database

    # ----------------------------------------------------------------- reads
    def get(self, project_id: str, scope_key: str) -> dict[str, Any]:
        with self.database.session() as session:
            row = self._require(session, project_id, scope_key)
            return self._view(row)

    def list_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.session() as session:
            return [
                self._view(row)
                for row in session.scalars(
                    select(TimelineBranch)
                    .where(TimelineBranch.project_id == project_id)
                    .order_by(TimelineBranch.created_at)
                )
            ]

    # ----------------------------------------------------------------- writes
    def ensure(self, project_id: str, scope_key: str, **kwargs: Any) -> dict[str, Any]:
        with self.database.session() as session:
            row = ensure_branch_in_session(
                session, project_id=project_id, scope_key=scope_key, **kwargs
            )
            return self._view(row)

    def merge(
        self,
        project_id: str,
        scope_key: str,
        *,
        into_scope_key: str = MAIN_SCOPE,
        allowed_state_paths: list[str],
        merged_by: str,
        allow_dream_states: bool = False,
    ) -> dict[str, Any]:
        """Close a branch as MERGED with a declared, captured write-back set.

        The declaration is mandatory: an empty allowed list is a request to
        merge nothing, which is ``retire``. Dream branches refuse a merge into
        main unless the caller explicitly accepts dream states as canon.
        Exactly one of two concurrent merges wins; the loser gets a conflict,
        never a silent second manifest.
        """

        paths = [path.strip() for path in allowed_state_paths if path.strip()]
        if not paths:
            raise TimelineBranchError(
                "a merge must declare which state paths may be written back; "
                "to write nothing back, retire the branch instead"
            )
        if not merged_by.strip():
            raise TimelineBranchError("a merge requires the identity of who merged")
        with self.database.session() as session:
            row = self._require(session, project_id, scope_key)
            if row.branch_kind == "MAIN":
                raise TimelineBranchError("the main timeline cannot be merged into anything")
            if row.branch_kind == "DREAM" and into_scope_key == MAIN_SCOPE and not allow_dream_states:
                raise TimelineBranchError(
                    "dream states do not merge into the main timeline by default; "
                    "pass allow_dream_states=True to declare that this dream is canon"
                )
            target = session.scalar(
                select(TimelineBranch).where(
                    TimelineBranch.project_id == project_id,
                    TimelineBranch.scope_key == into_scope_key,
                )
            )
            if into_scope_key != MAIN_SCOPE and (target is None or target.status != "ACTIVE"):
                raise TimelineBranchError(
                    f"merge target branch {into_scope_key!r} is not an active branch"
                )
            manifest: dict[str, dict[str, Any]] = {}
            heads = list(
                session.scalars(
                    select(CharacterStateHead).where(
                        CharacterStateHead.project_id == project_id,
                        CharacterStateHead.timeline_scope_key == scope_key,
                    )
                )
            )
            for head in heads:
                version = session.get(CharacterStateVersion, head.state_version_id)
                if version is None:
                    continue
                state = dict(version.narrative_state_json or {})
                captured: dict[str, Any] = {}
                for path in paths:
                    present, value = _dotted_get(state, path)
                    if present:
                        captured[path] = value
                if captured:
                    manifest[head.character_id] = {
                        "state_version_id": version.id,
                        "values": captured,
                    }
            claimed = session.execute(
                update(TimelineBranch)
                .where(TimelineBranch.id == row.id, TimelineBranch.status == "ACTIVE")
                .values(
                    status="MERGED",
                    merged_at=utcnow(),
                    merged_by=merged_by.strip()[:120],
                    merge_policy_json={
                        "into_scope_key": into_scope_key,
                        "allowed_state_paths": paths,
                        "allow_dream_states": allow_dream_states,
                    },
                    merge_manifest_json=manifest,
                )
            )
            if int(getattr(claimed, "rowcount", 0) or 0) != 1:
                raise TimelineBranchConflict(
                    f"branch {scope_key!r} was closed concurrently; its recorded merge stands"
                )
            session.flush()
            session.refresh(row)
            return self._view(row)

    def retire(self, project_id: str, scope_key: str, *, reason: str) -> dict[str, Any]:
        """Close a branch without writing anything back. Replays are no-ops."""

        return self._close(project_id, scope_key, status="RETIRED", reason=reason)

    def abandon(self, project_id: str, scope_key: str, *, reason: str) -> dict[str, Any]:
        return self._close(project_id, scope_key, status="ABANDONED", reason=reason)

    def _close(
        self, project_id: str, scope_key: str, *, status: str, reason: str
    ) -> dict[str, Any]:
        if not reason.strip():
            raise TimelineBranchError("closing a branch requires a reason")
        with self.database.session() as session:
            row = self._require(session, project_id, scope_key)
            if row.branch_kind == "MAIN":
                raise TimelineBranchError("the main timeline cannot be retired or abandoned")
            if row.status == status:
                # Repeated retirement is safe: the first close stands.
                return self._view(row)
            claimed = session.execute(
                update(TimelineBranch)
                .where(TimelineBranch.id == row.id, TimelineBranch.status == "ACTIVE")
                .values(
                    status=status,
                    retired_at=utcnow(),
                    retire_reason=reason.strip()[:500],
                )
            )
            if int(getattr(claimed, "rowcount", 0) or 0) != 1:
                session.expire(row)
                current = self._require(session, project_id, scope_key)
                if current.status == status:
                    return self._view(current)
                raise TimelineBranchConflict(
                    f"branch {scope_key!r} is {current.status}; it cannot become {status}"
                )
            session.flush()
            session.refresh(row)
            return self._view(row)

    # ------------------------------------------------------------------ sweep
    def _references_exist(self, session: Session, project_id: str, scope_key: str) -> bool:
        for query in (
            select(CharacterStateVersion.id).where(
                CharacterStateVersion.project_id == project_id,
                CharacterStateVersion.timeline_scope_key == scope_key,
            ),
            select(CharacterStateHead.id).where(
                CharacterStateHead.project_id == project_id,
                CharacterStateHead.timeline_scope_key == scope_key,
            ),
            select(CharacterStateDelta.id).where(
                CharacterStateDelta.project_id == project_id,
                CharacterStateDelta.timeline_scope_key == scope_key,
            ),
            select(TimelineTransition.id).where(
                TimelineTransition.project_id == project_id,
                TimelineTransition.branch_key == scope_key,
            ),
        ):
            if session.scalar(query.limit(1)) is not None:
                return True
        return False

    def _latest_use(self, session: Session, project_id: str, scope_key: str) -> datetime | None:
        stamps = [
            session.scalar(
                select(func.max(CharacterStateHead.updated_at)).where(
                    CharacterStateHead.project_id == project_id,
                    CharacterStateHead.timeline_scope_key == scope_key,
                )
            ),
            session.scalar(
                select(func.max(CharacterStateDelta.created_at)).where(
                    CharacterStateDelta.project_id == project_id,
                    CharacterStateDelta.timeline_scope_key == scope_key,
                )
            ),
            session.scalar(
                select(func.max(TimelineTransition.updated_at)).where(
                    TimelineTransition.project_id == project_id,
                    TimelineTransition.branch_key == scope_key,
                )
            ),
        ]
        observed = [stamp for stamp in stamps if stamp is not None]
        if not observed:
            return None
        latest = max(observed)
        return latest.replace(tzinfo=UTC) if latest.tzinfo is None else latest

    def sweep_orphans(
        self, project_id: str, *, min_idle_seconds: int = 7 * 24 * 3600, limit: int = 50
    ) -> BranchSweepResult:
        """Close branches nothing references and nothing has touched recently.

        Closed as ABANDONED — never deleted — so a branch that was registered
        by a transition that got recompiled away stops looking like live
        story. ``last_used_at`` is refreshed from the auditable referencing
        rows while sweeping, so the recorded usage is derived from evidence
        rather than trusted from a hot-path write.
        """

        cutoff = utcnow() - timedelta(seconds=max(60, int(min_idle_seconds)))
        abandoned: list[str] = []
        kept_referenced = kept_recent = 0
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(TimelineBranch)
                    .where(
                        TimelineBranch.project_id == project_id,
                        TimelineBranch.status == "ACTIVE",
                        TimelineBranch.branch_kind != "MAIN",
                    )
                    .order_by(TimelineBranch.created_at)
                    .limit(max(1, limit))
                )
            )
            for row in rows:
                latest_use = self._latest_use(session, project_id, row.scope_key)
                if latest_use is not None:
                    row.last_used_at = latest_use
                if self._references_exist(session, project_id, row.scope_key):
                    kept_referenced += 1
                    continue
                created = row.created_at
                if created is not None and created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                if created is not None and created > cutoff:
                    kept_recent += 1
                    continue
                row.status = "ABANDONED"
                row.retired_at = utcnow()
                row.retire_reason = "ORPHAN_SWEEP: no references and idle past the window"
                abandoned.append(row.scope_key)
            session.flush()
        return BranchSweepResult(
            examined=len(rows),
            abandoned=abandoned,
            kept_referenced=kept_referenced,
            kept_recent=kept_recent,
        )

    # ------------------------------------------------------------------ purge
    def purge(self, project_id: str, scope_key: str) -> None:
        """Physically delete one closed, unreferenced branch row.

        Any surviving CharacterStateVersion, head, delta or transition on the
        scope refuses the purge — those rows are audit history (candidates
        reference the branch through their state deltas), and deleting the
        branch row out from under them would orphan the trail.
        """

        with self.database.session() as session:
            row = self._require(session, project_id, scope_key)
            if row.branch_kind == "MAIN":
                raise TimelineBranchError("the main timeline cannot be purged")
            if row.status == "ACTIVE":
                raise TimelineBranchError("an active branch cannot be purged; close it first")
            if self._references_exist(session, project_id, scope_key):
                raise TimelineBranchReferenced(
                    f"branch {scope_key!r} is still referenced by character state or "
                    "timeline history; it can be retired but not deleted"
                )
            session.delete(row)
            session.flush()

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _require(session: Session, project_id: str, scope_key: str) -> TimelineBranch:
        row = session.scalar(
            select(TimelineBranch).where(
                TimelineBranch.project_id == project_id,
                TimelineBranch.scope_key == scope_key,
            )
        )
        if row is None:
            raise LookupError(f"timeline branch not found: {scope_key}")
        return row

    @staticmethod
    def _view(row: TimelineBranch) -> dict[str, Any]:
        return {
            "id": row.id,
            "project_id": row.project_id,
            "scope_key": row.scope_key,
            "branch_kind": row.branch_kind,
            "status": row.status,
            "parent_scope_key": row.parent_scope_key,
            "fork_shot_id": row.fork_shot_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
            "merged_at": row.merged_at.isoformat() if row.merged_at else None,
            "merged_by": row.merged_by,
            "merge_policy": row.merge_policy_json,
            "merge_manifest": row.merge_manifest_json,
            "retired_at": row.retired_at.isoformat() if row.retired_at else None,
            "retire_reason": row.retire_reason,
            "metadata": row.metadata_json,
        }


__all__ = [
    "MAIN_SCOPE",
    "BranchSweepResult",
    "TimelineBranchConflict",
    "TimelineBranchError",
    "TimelineBranchReferenced",
    "TimelineBranchService",
    "assert_branch_writable_in_session",
    "ensure_branch_in_session",
    "kind_for_scope_key",
]
