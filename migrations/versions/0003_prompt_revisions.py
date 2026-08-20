"""Add auditable user-visible image prompt revisions.

Revision ID: 0003_prompt_revisions
Revises: 0002_director_platform
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_prompt_revisions"
down_revision: str | None = "0002_director_platform"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "prompt_revisions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "prompt_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("mode", sa.String(length=40), nullable=False),
        sa.Column("original_prompt", sa.Text(), nullable=False),
        sa.Column("corrected_prompt", sa.Text(), nullable=False),
        sa.Column("detected_type", sa.String(length=80), nullable=False),
        sa.Column("reference_asset_ids", sa.JSON(), nullable=False),
        sa.Column("preserved_constraints", sa.JSON(), nullable=False),
        sa.Column("editable_variables", sa.JSON(), nullable=False),
        sa.Column("changes_json", sa.JSON(), nullable=False),
        sa.Column("corrector_version", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_prompt_revisions_project_id", "prompt_revisions", ["project_id"])
    op.create_index("ix_prompt_revisions_user_id", "prompt_revisions", ["user_id"])
    op.create_index("ix_prompt_revisions_mode", "prompt_revisions", ["mode"])
    op.create_index("ix_prompt_revisions_detected_type", "prompt_revisions", ["detected_type"])


def downgrade() -> None:
    if "prompt_revisions" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_prompt_revisions_detected_type", table_name="prompt_revisions")
    op.drop_index("ix_prompt_revisions_mode", table_name="prompt_revisions")
    op.drop_index("ix_prompt_revisions_user_id", table_name="prompt_revisions")
    op.drop_index("ix_prompt_revisions_project_id", table_name="prompt_revisions")
    op.drop_table("prompt_revisions")
