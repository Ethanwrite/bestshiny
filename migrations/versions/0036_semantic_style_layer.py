"""Add the second style layer: what the deterministic descriptor structurally cannot see.

The 64-D local descriptor is a histogram of colour, tone, saturation, edge and
spatial statistics. It reliably catches a grade shift, a contrast collapse or a
palette drift. It cannot distinguish oil paint from a 3D render, or 35mm from a
phone camera, because those differ in texture and rendering statistics it never
samples — two frames with matching histograms and entirely different media score
near 1.0 today.

A semantic multimodal embedding sees exactly that, and is correspondingly weak
where the descriptor is strong: a regrade that preserves the medium reads as
"same style" to it. So this is a second layer, not a replacement. Both are
recorded, both must pass, and an unavailable second layer sends the candidate to
review rather than through.

Revision ID: 0036_semantic_style_layer
Revises: 0035_media_renditions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_semantic_style_layer"
down_revision: str | None = "0035_media_renditions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _style_tables_absent() -> bool:
    """A recovery database that never reached the style migration has nothing to alter.

    `0029_project_style_lock` skips itself on the same shape, so this must too;
    otherwise a partial-schema recovery upgrade fails on a table 0029 chose not
    to create.
    """

    return not {"project_style_locks", "candidate_style_evaluations"}.issubset(_tables())


def upgrade() -> None:
    if _style_tables_absent():
        return
    # Plain ADD COLUMN, deliberately not batch_alter_table. On SQLite a batch
    # alter rebuilds the table by renaming it, and `project_style_locks` is
    # guarded by triggers that reference it by name — the rename invalidates
    # them mid-flight and the migration fails. Nullable columns and a
    # server-default float need no rebuild, so none is asked for.
    op.add_column(
        "project_style_locks",
        sa.Column("semantic_style_embedding_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "project_style_locks",
        sa.Column(
            "semantic_similarity_threshold",
            sa.Float(),
            nullable=False,
            server_default="0.80",
        ),
    )
    op.create_index(
        "ix_project_style_locks_semantic_style_embedding_id",
        "project_style_locks",
        ["semantic_style_embedding_id"],
    )

    op.add_column(
        "candidate_style_evaluations",
        sa.Column("semantic_status", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "candidate_style_evaluations",
        sa.Column("semantic_average_similarity", sa.Float(), nullable=True),
    )
    op.add_column(
        "candidate_style_evaluations",
        sa.Column("semantic_minimum_similarity", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_candidate_style_evaluations_semantic_status",
        "candidate_style_evaluations",
        ["semantic_status"],
    )


def downgrade() -> None:
    if _style_tables_absent():
        return
    op.drop_index(
        "ix_candidate_style_evaluations_semantic_status",
        table_name="candidate_style_evaluations",
    )
    op.drop_column("candidate_style_evaluations", "semantic_minimum_similarity")
    op.drop_column("candidate_style_evaluations", "semantic_average_similarity")
    op.drop_column("candidate_style_evaluations", "semantic_status")

    op.drop_index(
        "ix_project_style_locks_semantic_style_embedding_id",
        table_name="project_style_locks",
    )
    op.drop_column("project_style_locks", "semantic_similarity_threshold")
    op.drop_column("project_style_locks", "semantic_style_embedding_id")
