"""One-time audit and retirement of empty pre-created CREATED candidates.

Until the batch-atomicity change, a paid image batch pre-created one empty
CREATED candidate row per extra image in its own committed transaction, before
any media existed. A process death between that commit and the completion
transaction stranded the rows: CREATED for ever, bound to no job, owning no
media. The current pipeline creates sibling candidates inside the completion
transaction, so no new rows of this shape can appear — what this script deals
with is the population left behind.

An orphan is recognised by what it lacks, checked from both directions:

    status = CREATED
    generation_job_id IS NULL            (never bound to the job that paid)
    output_asset_id IS NULL              (owns no artefact)
    no GenerationJob.candidate_id = id   (no job claims it either)
    no MediaAsset.generation_candidate_id = id
    older than --older-than-hours        (not a transaction still in flight)

A *legitimate* CREATED candidate — one the candidate pipeline made — is bound
to its GenerationJob in the same transaction that created it, so it fails the
second and fourth checks and is never touched.

Retirement is a status change, not a delete: the row keeps its attempt number
(the shot's attempt sequence stays truthful), gains status RETIRED with a
reason, and a DecisionRecord is appended per shot for the audit trail. The
update is fenced on the same emptiness predicates it audited, so a row that
gained a binding between the read and the write is left alone.

    .venv/bin/python scripts/retire_empty_candidates.py            # audit only
    .venv/bin/python scripts/retire_empty_candidates.py --apply    # retire

Opens no socket. Prints no secret.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from platform_database import Database  # noqa: E402
from platform_shared import Settings, affected_rows  # noqa: E402
from production_domain.models import (  # noqa: E402
    CandidateStatus,
    DecisionRecord,
    Episode,
    GenerationCandidate,
    GenerationJob,
    MediaAsset,
    Scene,
    Shot,
    utcnow,
)
from sqlalchemy import and_, exists, select, update  # noqa: E402

RETIREMENT_REASON = (
    "RETIRED_EMPTY_BATCH_PLACEHOLDER: pre-allocated batch sibling slot orphaned "
    "before media registration; retired by scripts/retire_empty_candidates.py"
)


def _emptiness_predicates() -> list[Any]:
    """The conditions that make a CREATED row an orphan, usable in SELECT and UPDATE."""

    return [
        GenerationCandidate.status == CandidateStatus.CREATED.value,
        GenerationCandidate.generation_job_id.is_(None),
        GenerationCandidate.output_asset_id.is_(None),
        ~exists(select(GenerationJob.id).where(GenerationJob.candidate_id == GenerationCandidate.id)),
        ~exists(
            select(MediaAsset.id).where(MediaAsset.generation_candidate_id == GenerationCandidate.id)
        ),
    ]


def find_orphaned_empty_candidates(
    database: Database,
    *,
    older_than: timedelta,
    limit: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    cutoff = (now or utcnow()) - older_than
    with database.session() as session:
        rows = session.execute(
            select(GenerationCandidate, Episode.project_id)
            .join(Shot, Shot.id == GenerationCandidate.shot_id)
            .join(Scene, Scene.id == Shot.scene_id)
            .join(Episode, Episode.id == Scene.episode_id)
            .where(and_(*_emptiness_predicates()), GenerationCandidate.created_at < cutoff)
            .order_by(GenerationCandidate.created_at)
            .limit(max(1, limit))
        ).all()
        found: list[dict[str, Any]] = []
        for candidate, project_id in rows:
            created_at = candidate.created_at
            if created_at is not None and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            found.append(
                {
                    "candidate_id": candidate.id,
                    "shot_id": candidate.shot_id,
                    "project_id": project_id,
                    "attempt_number": candidate.attempt_number,
                    "batch_index": (candidate.metadata_json or {}).get("batch_index"),
                    "created_at": created_at.isoformat() if created_at else None,
                }
            )
        return found


def retire_candidates(database: Database, found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retire each audited row, re-checking its emptiness under the write.

    One transaction per candidate, so a conflict on one row cannot roll back
    the retirement of the others. The conditional UPDATE is the safety: a row
    that stopped being empty since the audit matches zero rows and is skipped.
    """

    outcomes: list[dict[str, Any]] = []
    for item in found:
        with database.session() as session:
            result = session.execute(
                update(GenerationCandidate)
                .where(
                    GenerationCandidate.id == item["candidate_id"],
                    *_emptiness_predicates(),
                )
                .values(
                    status=CandidateStatus.RETIRED.value,
                    rejection_reason=RETIREMENT_REASON,
                )
            )
            retired = affected_rows(result) == 1
            if retired:
                session.add(
                    DecisionRecord(
                        project_id=item["project_id"],
                        shot_id=item["shot_id"],
                        decision_type="CANDIDATE_RETIREMENT",
                        input_features={
                            "candidate_id": item["candidate_id"],
                            "attempt_number": item["attempt_number"],
                            "batch_index": item["batch_index"],
                            "created_at": item["created_at"],
                            "reason": RETIREMENT_REASON,
                        },
                        selected_action="RETIRE_EMPTY_CANDIDATE",
                        reason_codes=["EMPTY_PRECREATED_BATCH_SIBLING"],
                        model_version="none",
                        policy_version="candidate-retirement-v1",
                    )
                )
        outcomes.append({**item, "retired": retired})
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="retire the audited rows; without this flag the script only reports",
    )
    parser.add_argument(
        "--older-than-hours",
        type=float,
        default=24.0,
        help="only rows created at least this long ago are considered (default 24)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="at most this many rows per run (default 1000)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="override the configured DATABASE_URL (useful for a scratch copy)",
    )
    args = parser.parse_args()

    database = Database(args.database_url or Settings().database_url)
    found = find_orphaned_empty_candidates(
        database,
        older_than=timedelta(hours=max(0.0, args.older_than_hours)),
        limit=args.limit,
    )
    if not args.apply:
        print(
            json.dumps(
                {"mode": "audit", "orphaned_empty_candidates": found, "count": len(found)},
                indent=2,
            )
        )
        return 0
    outcomes = retire_candidates(database, found)
    retired = sum(1 for item in outcomes if item["retired"])
    print(
        json.dumps(
            {
                "mode": "retire",
                "candidates": outcomes,
                "count": len(outcomes),
                "retired": retired,
                "skipped": len(outcomes) - retired,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
