"""Require an explicit audited admin claim for isolated legacy workspaces.

Revision ID: 0017_explicit_legacy_workspace_claims
Revises: 0016_worker_command_claim_binding
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_explicit_legacy_workspace_claims"
down_revision: str | None = "0016_worker_command_claim_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "legacy_workspace_claims" in tables:
        return
    if not {"users", "workspaces"}.issubset(tables):
        return
    op.create_table(
        "legacy_workspace_claims",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("legacy_user_id", sa.String(length=36), nullable=False),
        sa.Column("target_user_id", sa.String(length=36), nullable=False),
        sa.Column("actor_type", sa.String(length=80), nullable=False),
        sa.Column("workspace_ids", sa.JSON(), nullable=False),
        sa.Column("project_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["legacy_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_legacy_workspace_claim_idempotency",
        ),
        sa.UniqueConstraint(
            "legacy_user_id",
            name="uq_legacy_workspace_claim_legacy_user",
        ),
    )
    op.create_index(
        "ix_legacy_workspace_claims_legacy_user_id",
        "legacy_workspace_claims",
        ["legacy_user_id"],
    )
    op.create_index(
        "ix_legacy_workspace_claims_target_user_id",
        "legacy_workspace_claims",
        ["target_user_id"],
    )


def downgrade() -> None:
    if "legacy_workspace_claims" not in _tables():
        return
    op.drop_index(
        "ix_legacy_workspace_claims_target_user_id",
        table_name="legacy_workspace_claims",
    )
    op.drop_index(
        "ix_legacy_workspace_claims_legacy_user_id",
        table_name="legacy_workspace_claims",
    )
    op.drop_table("legacy_workspace_claims")
