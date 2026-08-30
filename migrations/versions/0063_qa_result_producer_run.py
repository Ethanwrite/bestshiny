"""QAResult rows carry their Character Evidence producer run, uniquely.

The signed-callback path checked a candidate's completed run ids in one
transaction, inserted the QAResult in a second, and appended the run id to the
candidate's JSON metadata in a third. Two concurrent deliveries of the same
callback therefore both passed the check and both inserted, and the JSON
read-modify-write could lose one of the appends. The run id now lives on the
row itself under a unique (candidate_id, producer_run_id) index, so the
database — not a racy pre-check — is what makes replays converge.

Backfill: existing rows record their run id inside
``metrics_json.character_evidence.producer_run_id``. The earliest row per
(candidate, run) receives the column; any later duplicate the old race
produced keeps a NULL run id and stays in place as history rather than being
deleted.

Revision ID: 0063_qa_result_producer_run
Revises: 0062_canonical_list_pricing
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_qa_result_producer_run"
down_revision: str | None = "0062_canonical_list_pricing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _producer_run_id(metrics_json: object) -> str | None:
    if isinstance(metrics_json, (str, bytes)):
        try:
            metrics_json = json.loads(metrics_json)
        except (TypeError, ValueError):
            return None
    if not isinstance(metrics_json, dict):
        return None
    evidence = metrics_json.get("character_evidence")
    if not isinstance(evidence, dict):
        return None
    run_id = evidence.get("producer_run_id")
    if isinstance(run_id, str) and 0 < len(run_id) <= 64:
        return run_id
    return None


def upgrade() -> None:
    if "qa_results" not in set(sa.inspect(op.get_bind()).get_table_names()):
        # The assetless recovery snapshot (see 0008/0028): a database restored
        # without the QA tables skips QA-only migrations entirely.
        return
    op.add_column("qa_results", sa.Column("producer_run_id", sa.String(length=64), nullable=True))
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, candidate_id, metrics_json FROM qa_results ORDER BY created_at, id")
    ).fetchall()
    claimed: set[tuple[str, str]] = set()
    for row_id, candidate_id, metrics_json in rows:
        run_id = _producer_run_id(metrics_json)
        if run_id is None:
            continue
        key = (candidate_id, run_id)
        if key in claimed:
            # A duplicate the old race produced: leave its run id NULL so the
            # unique index can be created without rewriting history.
            continue
        claimed.add(key)
        bind.execute(
            sa.text("UPDATE qa_results SET producer_run_id = :run_id WHERE id = :row_id"),
            {"run_id": run_id, "row_id": row_id},
        )
    op.create_index(
        "uq_qa_result_candidate_producer_run",
        "qa_results",
        ["candidate_id", "producer_run_id"],
        unique=True,
    )


def downgrade() -> None:
    if "qa_results" not in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.drop_index("uq_qa_result_candidate_producer_run", table_name="qa_results")
    op.drop_column("qa_results", "producer_run_id")
