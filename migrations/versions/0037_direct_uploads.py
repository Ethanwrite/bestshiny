"""Authorize a direct-to-storage upload without the bytes passing through the API.

Reads already bypass this service through presigned reference URLs. Writes were
the other half: a user uploading a 38 MB plate streamed it through the control
plane on its way to a bucket that could have received it directly.

The client now PUTs to object storage itself. This table holds what the server
decided in between — project, asset type, key, enforced digest, quota hold — so
the completion call carries only a row id and cannot retarget the upload at a
different project, asset type or key.

Revision ID: 0037_direct_uploads
Revises: 0036_semantic_style_layer
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_direct_uploads"
down_revision: str | None = "0036_semantic_style_layer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "projects" not in set(sa.inspect(op.get_bind()).get_table_names()):
        # A recovery database that never reached the core schema has nothing to
        # attach this to; the earlier migrations skip the same shape.
        return
    op.create_table(
        "direct_uploads",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("asset_type", sa.String(length=50), nullable=False),
        sa.Column("filename", sa.String(length=300), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("declared_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("lineage_key", sa.String(length=500), nullable=False, server_default="shared"),
        sa.Column("shot_id", sa.String(length=36), nullable=True),
        sa.Column("character_id", sa.String(length=36), nullable=True),
        sa.Column("storage_reservation_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="PENDING"),
        sa.Column("media_asset_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_direct_upload_idempotency"),
    )
    for name, columns in (
        ("ix_direct_uploads_project_id", ["project_id"]),
        ("ix_direct_uploads_workspace_id", ["workspace_id"]),
        ("ix_direct_uploads_created_by_user_id", ["created_by_user_id"]),
        ("ix_direct_uploads_sha256", ["sha256"]),
        ("ix_direct_uploads_storage_reservation_id", ["storage_reservation_id"]),
        ("ix_direct_uploads_media_asset_id", ["media_asset_id"]),
        ("ix_direct_uploads_status", ["status"]),
        ("ix_direct_upload_expiry", ["status", "expires_at"]),
    ):
        op.create_index(name, "direct_uploads", columns)


def downgrade() -> None:
    if "direct_uploads" not in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    for name in (
        "ix_direct_upload_expiry",
        "ix_direct_uploads_status",
        "ix_direct_uploads_media_asset_id",
        "ix_direct_uploads_storage_reservation_id",
        "ix_direct_uploads_sha256",
        "ix_direct_uploads_created_by_user_id",
        "ix_direct_uploads_workspace_id",
        "ix_direct_uploads_project_id",
    ):
        op.drop_index(name, table_name="direct_uploads")
    op.drop_table("direct_uploads")
