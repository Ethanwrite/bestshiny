"""Add tenant memberships and hashed, expiring authentication sessions.

Revision ID: 0007_commercial_auth
Revises: 0006_runtime_data_integrity
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_commercial_auth"
down_revision: str | None = "0006_runtime_data_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    # Some recovery/diagnostic databases are stamped at 0005 with only the
    # runtime tables under repair. Do not make that partial-schema workflow
    # fail while upgrading unrelated integrity data.
    if not {"users", "workspaces"}.issubset(tables):
        return
    if "workspace_memberships" not in tables:
        op.create_table(
            "workspace_memberships",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("workspace_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("role", sa.String(length=40), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "workspace_id",
                "user_id",
                name="uq_workspace_membership_user",
            ),
        )
        op.create_index(
            "ix_workspace_memberships_user_id",
            "workspace_memberships",
            ["user_id"],
        )
        op.create_index(
            "ix_workspace_memberships_workspace_id",
            "workspace_memberships",
            ["workspace_id"],
        )

        # Existing owners retain access after the authorization boundary is enabled.
        workspace = sa.table(
            "workspaces",
            sa.column("id", sa.String),
            sa.column("owner_user_id", sa.String),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        )
        membership = sa.table(
            "workspace_memberships",
            sa.column("id", sa.String),
            sa.column("workspace_id", sa.String),
            sa.column("user_id", sa.String),
            sa.column("role", sa.String),
            sa.column("status", sa.String),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        )
        bind = op.get_bind()
        for row in bind.execute(
            sa.select(
                workspace.c.id,
                workspace.c.owner_user_id,
                workspace.c.created_at,
                workspace.c.updated_at,
            )
        ).mappings():
            bind.execute(
                membership.insert().values(
                    id=str(uuid.uuid4()),
                    workspace_id=row["id"],
                    user_id=row["owner_user_id"],
                    role="OWNER",
                    status="ACTIVE",
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )

    if "auth_sessions" not in tables:
        op.create_table(
            "auth_sessions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("user_agent", sa.String(length=500), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
        op.create_index("ix_auth_sessions_token_hash", "auth_sessions", ["token_hash"], unique=True)
        op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])


def downgrade() -> None:
    tables = _tables()
    if "auth_sessions" in tables:
        op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
        op.drop_index("ix_auth_sessions_token_hash", table_name="auth_sessions")
        op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
        op.drop_table("auth_sessions")
    if "workspace_memberships" in tables:
        op.drop_index("ix_workspace_memberships_workspace_id", table_name="workspace_memberships")
        op.drop_index("ix_workspace_memberships_user_id", table_name="workspace_memberships")
        op.drop_table("workspace_memberships")
