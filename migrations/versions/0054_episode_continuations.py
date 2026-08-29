"""Episode continuations: the bridge from a finished episode to the next.

One row per (project, previous episode, next number) holds the computed
EpisodeContinuationContext snapshot, the proposed brief and beats, and the
compiled result - so preparation is idempotent, confirmation is replayable,
and the declared continuation mode (CONTINUOUS / TIME_JUMP / LOCATION_CHANGE)
is recorded next to the per-continuity-class inheritance verdict it implies.

Revision ID: 0054_episode_continuations
Revises: 0053_creative_director
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054_episode_continuations"
down_revision: str | None = "0053_creative_director"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "episode_continuations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "previous_episode_id",
            sa.String(length=36),
            sa.ForeignKey("episodes.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "next_episode_id",
            sa.String(length=36),
            sa.ForeignKey("episodes.id"),
            nullable=True,
            index=True,
        ),
        sa.Column("next_episode_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("continuation_mode", sa.String(length=40), nullable=False),
        sa.Column("time_gap", sa.String(length=120), nullable=False),
        sa.Column("new_location", sa.String(length=200), nullable=True),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("context_hash", sa.String(length=64), nullable=False),
        sa.Column("context_version", sa.String(length=60), nullable=False),
        sa.Column("brief_json", sa.JSON(), nullable=False),
        sa.Column("beats_json", sa.JSON(), nullable=False),
        sa.Column("revisions_json", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reasoner", sa.String(length=60), nullable=False),
        sa.Column("script_rendered", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project_id",
            "previous_episode_id",
            "next_episode_number",
            name="uq_episode_continuation_target",
        ),
        sa.CheckConstraint(
            "status IN ('BRIEF_PROPOSED', 'CONFIRMED', 'COMPILED', 'ABANDONED')",
            name="ck_episode_continuation_status",
        ),
        sa.CheckConstraint(
            "continuation_mode IN ('CONTINUOUS', 'TIME_JUMP', 'LOCATION_CHANGE')",
            name="ck_episode_continuation_mode",
        ),
        sa.CheckConstraint("next_episode_number > 1", name="ck_episode_continuation_number"),
        sa.CheckConstraint("length(context_hash) = 64", name="ck_episode_continuation_hash"),
    )


def downgrade() -> None:
    op.drop_table("episode_continuations")
