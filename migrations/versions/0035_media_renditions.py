"""Separate a media asset's original bytes from the copies made to satisfy consumers.

A provider that caps reference size used to be an argument for downscaling on
the way in. That destroys the only copy of a face, a product label or a fabric
weave that the project will ever have. The original is now immutable and every
size- or format-constrained consumer reads a derived rendition instead, keyed by
the constraints that caused it, so changed limits produce a new rendition rather
than silently reusing one built for different ones.

Revision ID: 0035_media_renditions
Revises: 0034_narrative_ledger
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_media_renditions"
down_revision: str | None = "0034_narrative_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_renditions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "media_asset_id",
            sa.String(length=36),
            sa.ForeignKey("media_assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("constraint_key", sa.String(length=200), nullable=False, server_default="original"),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("local_path", sa.String(length=1000), nullable=True),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "media_asset_id",
            "kind",
            "constraint_key",
            name="uq_media_rendition_scope",
        ),
    )
    op.create_index("ix_media_renditions_media_asset_id", "media_renditions", ["media_asset_id"])
    op.create_index("ix_media_renditions_sha256", "media_renditions", ["sha256"])
    op.create_index("ix_media_rendition_asset", "media_renditions", ["media_asset_id", "kind"])


def downgrade() -> None:
    op.drop_index("ix_media_rendition_asset", table_name="media_renditions")
    op.drop_index("ix_media_renditions_sha256", table_name="media_renditions")
    op.drop_index("ix_media_renditions_media_asset_id", table_name="media_renditions")
    op.drop_table("media_renditions")
