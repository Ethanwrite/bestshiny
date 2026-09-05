"""Claim a client_turn_id before the director call, not after it.

A dialogue round writes its turn only once the director has answered, so a
crash leaves no orphan message and spends no FREE round. That left a window
the unique key on ``creative_turns`` could not close: two requests carrying
the same ``client_turn_id`` - a double click, a retry racing a slow first
attempt, a duplicate session create resolved onto the session it collided
with - both read "no recorded turn", both paid for a director call, and only
the second *write* was refused. The same window sat under session creation.

``creative_turn_claims`` is the row that says "this message is being answered
right now". It is inserted in the read phase under a unique (session, key)
index, deleted in the transaction that records the turn, freed by a request
that failed, and taken over by lease when the process holding it died.

Revision ID: 0080_creative_turn_claims
Revises: 0079_voyage_video_pixel_price
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0080_creative_turn_claims"
down_revision: str | None = "0079_voyage_video_pixel_price"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

TABLE = "creative_turn_claims"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "creative_sessions" not in tables:
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return
    if TABLE in tables:
        # Present already (a `create_all`, a manual fix, a snapshot restored
        # between the DDL and the alembic_version commit). Raising here would
        # keep the api container restarting for ever; the schema is in the
        # state this migration wanted, so it is skipped and logged.
        logger.warning("%s already exists; skipping its migration", TABLE)
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("client_turn_id", sa.String(length=120), nullable=False),
        sa.Column("claim_token", sa.String(length=36), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["creative_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "client_turn_id", name="uq_creative_turn_claim_key"),
    )
    op.create_index(
        op.f("ix_creative_turn_claims_session_id"), TABLE, ["session_id"], unique=False
    )


def downgrade() -> None:
    if TABLE not in _tables():
        return
    op.drop_index(op.f("ix_creative_turn_claims_session_id"), table_name=TABLE)
    op.drop_table(TABLE)
