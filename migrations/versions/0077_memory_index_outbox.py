"""A durable queue for advisory vector-memory indexing.

Assets the creative director creates - the character identities, the style
plate, the canonical scene, product and prop key visuals - never reached the
vector memory, because `CreativeDirectorService` has no memory engine at all.
Committed shot results were indexed only from the Passenger route, not from a
candidate commit. So a project could hold a full visual bible and still return
nothing on a similarity query.

The fix cannot be a direct call. Embedding is an external HTTPS request to a
third party; making it inside the transaction that locks a bible or commits a
candidate would put Voyage's availability on the critical path of Canon, which
is exactly backwards - the memory is ADVISORY and the Canon is not.
``memory_index_outbox`` is the boundary: the writer enqueues in its own
transaction, a worker drains afterwards, and the idempotency key makes a
replayed lock or a re-run worker produce one ShotMemory rather than several. A
row waits while the ``voyage_memory`` flag is off, because a queue that is not
being drained is a queue, not a loss.

Revision ID: 0077_memory_index_outbox
Revises: 0076_character_evidence_coverage
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0077_memory_index_outbox"
down_revision: str | None = "0076_character_evidence_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLE = "memory_index_outbox"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "projects" not in tables:
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return
    if NEW_TABLE in tables:
        raise RuntimeError(f"{NEW_TABLE} already exists before its own migration")
    op.create_table(
        NEW_TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=250), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_id", sa.String(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shot_memory_id", sa.String(length=36), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_memory_index_outbox_key"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CLAIMED', 'DONE', 'FAILED')",
            name="ck_memory_index_outbox_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_memory_index_outbox_attempts"),
    )
    op.create_index(f"ix_{NEW_TABLE}_project_id", NEW_TABLE, ["project_id"])
    op.create_index("ix_memory_index_outbox_due", NEW_TABLE, ["status", "next_attempt_at"])


def downgrade() -> None:
    if NEW_TABLE not in _tables():
        return
    op.drop_index("ix_memory_index_outbox_due", table_name=NEW_TABLE)
    op.drop_index(f"ix_{NEW_TABLE}_project_id", table_name=NEW_TABLE)
    op.drop_table(NEW_TABLE)
