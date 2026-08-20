"""Attach pre-workspace projects to a safely claimable legacy tenant.

Revision ID: 0011_legacy_workspace_backfill
Revises: 0010_shot_lineage_invariants
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0011_legacy_workspace_backfill"
down_revision: str | None = "0010_shot_lineage_invariants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_EMAIL = "local@ai-director.invalid"


def upgrade() -> None:
    bind = op.get_bind()
    required = {"users", "workspaces", "workspace_memberships", "projects"}
    if not required.issubset(sa.inspect(bind).get_table_names()):
        return
    orphan_count = bind.scalar(sa.text("SELECT COUNT(*) FROM projects WHERE workspace_id IS NULL"))
    if not orphan_count:
        return
    real_user_count = bind.scalar(
        sa.text("SELECT COUNT(*) FROM users WHERE lower(email) <> :legacy_email"),
        {"legacy_email": LEGACY_EMAIL},
    )
    if real_user_count:
        raise RuntimeError(
            "unassigned legacy projects coexist with real users; assign them to a workspace "
            "explicitly before upgrading"
        )

    now = datetime.now(UTC)
    legacy_user_id = bind.scalar(
        sa.text("SELECT id FROM users WHERE lower(email) = :legacy_email"),
        {"legacy_email": LEGACY_EMAIL},
    )
    if not legacy_user_id:
        legacy_user_id = str(uuid.uuid4())
        bind.execute(
            sa.text(
                """INSERT INTO users
                (id, email, display_name, password_hash, status, created_at, updated_at)
                VALUES (:id, :email, :display_name, '', 'ACTIVE', :now, :now)"""
            ),
            {
                "id": legacy_user_id,
                "email": LEGACY_EMAIL,
                "display_name": "Local Director",
                "now": now,
            },
        )

    workspace_id = bind.scalar(
        sa.text(
            """SELECT id FROM workspaces
            WHERE owner_user_id = :owner_user_id
            ORDER BY created_at, id LIMIT 1"""
        ),
        {"owner_user_id": legacy_user_id},
    )
    if not workspace_id:
        workspace_id = str(uuid.uuid4())
        bind.execute(
            sa.text(
                """INSERT INTO workspaces
                (id, owner_user_id, name, status, created_at, updated_at)
                VALUES (:id, :owner_user_id, :name, 'ACTIVE', :now, :now)"""
            ),
            {
                "id": workspace_id,
                "owner_user_id": legacy_user_id,
                "name": "Director Workspace",
                "now": now,
            },
        )

    membership_exists = bind.scalar(
        sa.text(
            """SELECT COUNT(*) FROM workspace_memberships
            WHERE workspace_id = :workspace_id AND user_id = :user_id"""
        ),
        {"workspace_id": workspace_id, "user_id": legacy_user_id},
    )
    if not membership_exists:
        bind.execute(
            sa.text(
                """INSERT INTO workspace_memberships
                (id, workspace_id, user_id, role, status, created_at, updated_at)
                VALUES (:id, :workspace_id, :user_id, 'OWNER', 'ACTIVE', :now, :now)"""
            ),
            {
                "id": str(uuid.uuid4()),
                "workspace_id": workspace_id,
                "user_id": legacy_user_id,
                "now": now,
            },
        )

    bind.execute(
        sa.text("UPDATE projects SET workspace_id = :workspace_id WHERE workspace_id IS NULL"),
        {"workspace_id": workspace_id},
    )


def downgrade() -> None:
    # The ownership backfill is intentionally forward-only. Re-orphaning projects
    # would make them inaccessible and could cross a later real tenant boundary.
    pass
