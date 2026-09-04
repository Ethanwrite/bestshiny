"""Make opening a creative session idempotent at the database, not just in a read.

A retried create - a proxy timeout, a dropped socket, a second tab - was
guarded by a SELECT for an existing opening turn carrying the same
``client_turn_id`` followed by a create. Between the two, the first request's
session had not committed yet, so the retry took the create branch and opened a
second CreativeSession: a duplicate conversation and a second paid director
call, which is exactly what the key exists to prevent.

The session row now carries the key that opened it, unique per project. Per
project because that is how the browser scopes the key (user, project,
request), so the same literal key in two projects is two different requests and
must stay two sessions.

Revision ID: 0078_creative_session_create_idempotency
Revises: 0077_memory_index_outbox
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0078_creative_session_create_idempotency"
down_revision: str | None = "0077_memory_index_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN = "create_client_turn_id"
INDEX_NAME = "uq_creative_session_create_client_id"


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    columns = _columns("creative_sessions")
    if not columns:
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return
    if COLUMN in columns:
        raise RuntimeError(f"creative_sessions.{COLUMN} already exists before its own migration")
    op.add_column("creative_sessions", sa.Column(COLUMN, sa.String(length=120), nullable=True))
    op.create_index(
        INDEX_NAME,
        "creative_sessions",
        ["project_id", COLUMN],
        unique=True,
        postgresql_where=sa.text(f"{COLUMN} IS NOT NULL"),
        sqlite_where=sa.text(f"{COLUMN} IS NOT NULL"),
    )


def downgrade() -> None:
    if COLUMN not in _columns("creative_sessions"):
        return
    op.drop_index(INDEX_NAME, table_name="creative_sessions")
    op.drop_column("creative_sessions", COLUMN)
