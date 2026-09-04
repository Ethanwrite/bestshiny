"""The director's shot intent, carried on the shot itself.

``apply_shot_intents`` wrote only ``shot_type`` and ``duration`` back onto a
compiled shot, so everything else the director approved - the staged action
description, the start and end states, the gaze target, the per-shot continuity
obligations and the key-visual anchors the shot depends on - survived only in
``creative_shot_lineage.intent_json``, an audit record nothing in the
generation path reads. The prompt the model finally saw was therefore compiled
from the parsed action line alone.

This adds one JSON column, ``shots.director_intent_json``. One column rather
than six scalars because the shape is already versioned by the screenplay
schema, and ``creative_shot_lineage.intent_json`` set the precedent. It is live
input to prompt compilation, re-read on every recompile and every retry; the
lineage row stays what it always was, history.

Revision ID: 0073_director_shot_intent
Revises: 0072_creation_soft_delete
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0073_director_shot_intent"
down_revision: str | None = "0072_creation_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN = "director_intent_json"


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    columns = _columns("shots")
    if not columns:
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return
    if COLUMN in columns:
        raise RuntimeError(f"shots.{COLUMN} already exists before its own migration")
    op.add_column(
        "shots",
        sa.Column(COLUMN, sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    if COLUMN not in _columns("shots"):
        return
    op.drop_column("shots", COLUMN)
