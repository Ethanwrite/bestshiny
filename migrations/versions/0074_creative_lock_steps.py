"""The visual-bible lock becomes resumable instead of half-written.

Locking a bible writes immutable Canon through three services that cannot
share one transaction: ProjectStyleService, CharacterIdentityService and the
AssetRegistry. Asset versions, canonical promotions and style locks are
append-only by database trigger, and a project has exactly one style lock, so a
failure part-way could not be rolled back - and the retry, whose only replay
guard was an in-memory dict persisted after the fact, minted a *second*
identity version and a second canonical asset version.

``creative_lock_steps`` is the durable step ledger: one row per step, a stable
idempotency key, the status, the attempt count and what the step produced. A
retry continues the missing steps. The row alone is not the guarantee - a
process can die between the Canon write and the COMPLETED stamp - so each step
also re-discovers its own output from the Canon before acting; this table makes
the resume cheap, ordered and auditable. ``claimed_at`` is what stops two
concurrent approvals from running the same step at once, with a lease so a
process that died mid-step does not wedge the bible.

Revision ID: 0074_creative_lock_steps
Revises: 0073_director_shot_intent
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074_creative_lock_steps"
down_revision: str | None = "0073_director_shot_intent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLE = "creative_lock_steps"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "visual_bibles" not in tables:
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return
    if NEW_TABLE in tables:
        raise RuntimeError(f"{NEW_TABLE} already exists before its own migration")
    op.create_table(
        NEW_TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("creative_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bible_id",
            sa.String(length=36),
            sa.ForeignKey("visual_bibles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_kind", sa.String(length=40), nullable=False),
        sa.Column("step_key", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.String(length=250), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("produced_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("resolution", sa.String(length=20), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_creative_lock_step_key"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="ck_creative_lock_step_status",
        ),
    )
    op.create_index("ix_creative_lock_steps_session_id", NEW_TABLE, ["session_id"])
    op.create_index("ix_creative_lock_steps_bible_id", NEW_TABLE, ["bible_id"])
    op.create_index("ix_creative_lock_step_bible", NEW_TABLE, ["bible_id", "status"])


def downgrade() -> None:
    if NEW_TABLE not in _tables():
        return
    op.drop_index("ix_creative_lock_step_bible", table_name=NEW_TABLE)
    op.drop_index("ix_creative_lock_steps_bible_id", table_name=NEW_TABLE)
    op.drop_index("ix_creative_lock_steps_session_id", table_name=NEW_TABLE)
    op.drop_table(NEW_TABLE)
