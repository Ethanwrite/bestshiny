"""Timeline branch lifecycle: identity and states for timeline_scope_key branches.

Dream/flashback scope keys previously proliferated as bare strings
(OPEN_ISSUES 2.3) with no merge or retirement policy. Each branch is now a
row with a kind, a required parent for every non-main branch, the fork shot,
usage tracking, and an explicit lifecycle — ACTIVE, MERGED (with the declared
write-back policy and captured manifest), RETIRED, ABANDONED. History stays
readable in every state; only unreferenced, closed branches may ever be
physically purged.

Revision ID: 0059_timeline_branches
Revises: 0058_media_verification
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059_timeline_branches"
down_revision: str | None = "0058_media_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "timeline_branches",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("scope_key", sa.String(length=120), nullable=False),
        sa.Column("branch_kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("parent_scope_key", sa.String(length=120), nullable=True),
        sa.Column(
            "fork_shot_id",
            sa.String(length=36),
            sa.ForeignKey("shots.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_by", sa.String(length=120), nullable=True),
        sa.Column("merge_policy_json", sa.JSON(), nullable=False),
        sa.Column("merge_manifest_json", sa.JSON(), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retire_reason", sa.String(length=500), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "scope_key", name="uq_timeline_branch_scope"),
        sa.CheckConstraint(
            "branch_kind IN ('MAIN', 'DREAM', 'FLASHBACK', 'FLASH_FORWARD', 'ALTERNATE')",
            name="ck_timeline_branch_kind",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'MERGED', 'RETIRED', 'ABANDONED')",
            name="ck_timeline_branch_status",
        ),
        sa.CheckConstraint(
            "branch_kind = 'MAIN' OR parent_scope_key IS NOT NULL",
            name="ck_timeline_branch_parent_required",
        ),
    )
    op.create_index("ix_timeline_branch_status", "timeline_branches", ["project_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_timeline_branch_status", table_name="timeline_branches")
    op.drop_table("timeline_branches")
