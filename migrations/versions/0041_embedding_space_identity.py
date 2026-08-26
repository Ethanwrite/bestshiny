"""Record which vector space a style embedding belongs to.

A similarity score is only meaningful inside one space. Change the model, its
revision, the dimension count, how the stored vector is normalized, or the
metric, and the same number means something else. Nothing fails loudly on its
own: cosine over two unrelated 1024-vectors returns a perfectly plausible 0.83,
and a project's style gate would go on producing confident verdicts about a
comparison that no longer means anything.

`provider`, `model`, `algorithm_version` and `dimension` were already recorded;
only `model` was ever checked, and only to find a row rather than to decide
whether two vectors could be compared. These three complete the space, and the
service compares the whole of it before taking any score.

Backfill is exact rather than assumed. Every existing row was written by this
codebase, whose `aggregate()` L2-normalizes and whose `similarity()` is cosine,
so those are the values those rows actually have. `model_revision` backfills
empty because no provider wired here publishes one.

The backfill default is then dropped on PostgreSQL and kept on SQLite; see the
comment in `upgrade()` for why the asymmetry is the safe way round.

Revision ID: 0041_embedding_space
Revises: 0040_sync_result_inbox
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041_embedding_space"
down_revision: str | None = "0040_sync_result_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "style_embeddings"
_COLUMNS = (
    ("model_revision", sa.String(120), ""),
    ("normalization", sa.String(40), "L2"),
    ("distance_metric", sa.String(40), "cosine"),
)


def _existing() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    present = _existing()
    if not present:
        return
    for name, column_type, default in _COLUMNS:
        if name in present:
            continue
        op.add_column(
            _TABLE,
            sa.Column(name, column_type, nullable=False, server_default=default),
        )
    # The server default existed to backfill. Dropping it afterwards keeps a
    # raw insert from acquiring a space nobody derived — but only on
    # PostgreSQL, where it is a catalogue edit. SQLite cannot drop a default
    # without rebuilding the table, and rebuilding this one fails: the triggers
    # migration 0029 installed reference `style_embeddings` by name, so the
    # rename batch mode performs leaves them pointing at nothing. The default
    # is left in place there, and it is not a lie — `L2`/`cosine`/empty are the
    # values every row this codebase writes actually has.
    if op.get_bind().dialect.name != "postgresql":
        return
    for name, column_type, _default in _COLUMNS:
        if name in present:
            continue
        op.alter_column(_TABLE, name, existing_type=column_type, server_default=None)


def downgrade() -> None:
    # Plain DROP COLUMN, not batch mode. SQLite has supported it natively since
    # 3.35 and it leaves the table in place; batch mode would rename the table
    # instead, and migration 0029's triggers reference `style_embeddings` by
    # name, so the rename leaves them pointing at nothing and the rename back
    # fails.
    present = _existing()
    for name, _column_type, _default in reversed(_COLUMNS):
        if name in present:
            op.drop_column(_TABLE, name)
