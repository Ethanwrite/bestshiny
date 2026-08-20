"""Add the unified logical asset and immutable version registry.

Revision ID: 0004_unified_asset_registry
Revises: 0003_prompt_revisions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_unified_asset_registry"
down_revision: str | None = "0003_prompt_revisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _tables()
    if "assets" not in existing:
        op.create_table(
            "assets",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("asset_type", sa.String(length=40), nullable=False),
            sa.Column("name", sa.String(length=240), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("canonical_metadata", sa.JSON(), nullable=False),
            sa.Column("canonical_version_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_assets_asset_type", "assets", ["asset_type"])
        op.create_index("ix_assets_canonical_version_id", "assets", ["canonical_version_id"])
        op.create_index("ix_assets_created_by_user_id", "assets", ["created_by_user_id"])
        op.create_index("ix_assets_project_id", "assets", ["project_id"])
        op.create_index("ix_assets_status", "assets", ["status"])
        op.create_index("ix_assets_project_kind_status", "assets", ["project_id", "asset_type", "status"])

    existing = _tables()
    if "asset_versions" not in existing:
        op.create_table(
            "asset_versions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("asset_id", sa.String(length=36), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("label", sa.String(length=240), nullable=False),
            sa.Column("primary_media_asset_id", sa.String(length=36), nullable=True),
            sa.Column("parent_version_id", sa.String(length=36), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("continuity_state", sa.JSON(), nullable=False),
            sa.Column("embedding_refs", sa.JSON(), nullable=False),
            sa.Column("source", sa.String(length=40), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["parent_version_id"], ["asset_versions.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["primary_media_asset_id"], ["media_assets.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("asset_id", "version", name="uq_asset_version"),
        )
        op.create_index("ix_asset_versions_asset_id", "asset_versions", ["asset_id"])
        op.create_index("ix_asset_versions_created_by_user_id", "asset_versions", ["created_by_user_id"])
        op.create_index("ix_asset_versions_parent_version_id", "asset_versions", ["parent_version_id"])
        op.create_index(
            "ix_asset_versions_primary_media_asset_id", "asset_versions", ["primary_media_asset_id"]
        )
        op.create_index("ix_asset_versions_source", "asset_versions", ["source"])
        op.create_index("ix_asset_versions_status", "asset_versions", ["status"])

    existing = _tables()
    if "asset_version_media" not in existing:
        op.create_table(
            "asset_version_media",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("asset_version_id", sa.String(length=36), nullable=False),
            sa.Column("media_asset_id", sa.String(length=36), nullable=False),
            sa.Column("role", sa.String(length=80), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["asset_version_id"], ["asset_versions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["media_asset_id"], ["media_assets.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "asset_version_id", "media_asset_id", "role", name="uq_asset_version_media_role"
            ),
        )
        op.create_index(
            "ix_asset_version_media_asset_version_id", "asset_version_media", ["asset_version_id"]
        )
        op.create_index("ix_asset_version_media_media_asset_id", "asset_version_media", ["media_asset_id"])
        op.create_index("ix_asset_version_media_role", "asset_version_media", ["role"])
        op.create_index(
            "ix_asset_version_media_version_role", "asset_version_media", ["asset_version_id", "role"]
        )

    existing = _tables()
    if "asset_canonical_promotions" not in existing:
        op.create_table(
            "asset_canonical_promotions",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("asset_id", sa.String(length=36), nullable=False),
            sa.Column("from_version_id", sa.String(length=36), nullable=True),
            sa.Column("to_version_id", sa.String(length=36), nullable=False),
            sa.Column("promoted_by_user_id", sa.String(length=36), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["from_version_id"], ["asset_versions.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["promoted_by_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["to_version_id"], ["asset_versions.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_asset_canonical_promotions_asset_id", "asset_canonical_promotions", ["asset_id"])
        op.create_index(
            "ix_asset_canonical_promotions_from_version_id",
            "asset_canonical_promotions",
            ["from_version_id"],
        )
        op.create_index(
            "ix_asset_canonical_promotions_promoted_by_user_id",
            "asset_canonical_promotions",
            ["promoted_by_user_id"],
        )
        op.create_index(
            "ix_asset_canonical_promotions_to_version_id",
            "asset_canonical_promotions",
            ["to_version_id"],
        )


def downgrade() -> None:
    existing = _tables()
    if "asset_canonical_promotions" in existing:
        op.drop_table("asset_canonical_promotions")
    existing = _tables()
    if "asset_version_media" in existing:
        op.drop_table("asset_version_media")
    existing = _tables()
    if "asset_versions" in existing:
        op.drop_table("asset_versions")
    existing = _tables()
    if "assets" in existing:
        op.drop_table("assets")
