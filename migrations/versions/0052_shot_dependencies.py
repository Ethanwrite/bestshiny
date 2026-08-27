"""Add explicit shot dependencies: foreshadowing, revelation, obligation, state.

Similarity retrieval decides what a shot resembles; it cannot decide what a
shot requires. Episode 60's payoff shares no vocabulary with episode 7's
setup, so the requirement is recorded as a row — written at script compilation
or by manual editing — and retrieval forces it into generation context instead
of hoping cosine ranking surfaces it.

``source_shot_id`` deliberately carries no ON DELETE action: deleting a shot
that a surviving shot explicitly depends on must fail loudly, while a delete
that removes both ends in one statement passes because the row cascades away
with its target.

Revision ID: 0052_shot_dependencies
Revises: 0051_token_pricing
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_shot_dependencies"
down_revision: str | None = "0051_token_pricing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shot_dependencies",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "target_shot_id",
            sa.String(length=36),
            sa.ForeignKey("shots.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "source_shot_id",
            sa.String(length=36),
            sa.ForeignKey("shots.id"),
            nullable=True,
        ),
        sa.Column("dependency_type", sa.String(length=40), nullable=False),
        sa.Column("fact_key", sa.String(length=160)),
        sa.Column("obligation_key", sa.String(length=160)),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(length=40), nullable=False),
        sa.Column("dependency_key", sa.String(length=420), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("target_shot_id", "dependency_key", name="uq_shot_dependency_key"),
        sa.CheckConstraint(
            "dependency_type IN ('FORESHADOWING', 'FACT_REVELATION', "
            "'OBLIGATION_FULFILLMENT', 'STATE_INHERITANCE')",
            name="ck_shot_dependency_type",
        ),
        sa.CheckConstraint(
            "origin IN ('SCRIPT_COMPILER', 'MANUAL')",
            name="ck_shot_dependency_origin",
        ),
        sa.CheckConstraint(
            "source_shot_id IS NOT NULL OR fact_key IS NOT NULL OR obligation_key IS NOT NULL",
            name="ck_shot_dependency_referent",
        ),
        sa.CheckConstraint(
            "dependency_type != 'FACT_REVELATION' OR fact_key IS NOT NULL",
            name="ck_shot_dependency_fact_referent",
        ),
        sa.CheckConstraint(
            "dependency_type != 'OBLIGATION_FULFILLMENT' OR obligation_key IS NOT NULL",
            name="ck_shot_dependency_obligation_referent",
        ),
        sa.CheckConstraint(
            "dependency_type != 'STATE_INHERITANCE' OR source_shot_id IS NOT NULL",
            name="ck_shot_dependency_state_referent",
        ),
    )
    op.create_index("ix_shot_dependency_target", "shot_dependencies", ["project_id", "target_shot_id"])
    op.create_index("ix_shot_dependency_source_shot", "shot_dependencies", ["source_shot_id"])


def downgrade() -> None:
    op.drop_index("ix_shot_dependency_source_shot", table_name="shot_dependencies")
    op.drop_index("ix_shot_dependency_target", table_name="shot_dependencies")
    op.drop_table("shot_dependencies")
