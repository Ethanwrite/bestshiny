"""Fence provider-media uploads with a durable claim lease.

Revision ID: 0020_provider_media_upload_claim
Revises: 0019_media_asset_lineage_identity
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_provider_media_upload_claim"
down_revision: str | None = "0019_media_asset_lineage_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TOKEN_INDEX = "ix_media_provider_bindings_upload_claim_token"
EXPIRY_INDEX = "ix_media_provider_bindings_upload_claim_expires_at"


def _table_exists() -> bool:
    return "media_provider_bindings" in sa.inspect(op.get_bind()).get_table_names()


def _columns() -> set[str]:
    if not _table_exists():
        return set()
    return {
        str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns("media_provider_bindings")
    }


def _indexes() -> set[str]:
    if not _table_exists():
        return set()
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes("media_provider_bindings")
        if index.get("name")
    }


def upgrade() -> None:
    columns = _columns()
    if not columns:
        return
    with op.batch_alter_table("media_provider_bindings") as batch_op:
        batch_op.alter_column(
            "provider_media_id",
            existing_type=sa.String(length=500),
            nullable=True,
        )
        if "upload_claim_token" not in columns:
            batch_op.add_column(sa.Column("upload_claim_token", sa.String(length=36), nullable=True))
        if "upload_claim_expires_at" not in columns:
            batch_op.add_column(
                sa.Column("upload_claim_expires_at", sa.DateTime(timezone=True), nullable=True)
            )
        if "upload_started_at" not in columns:
            batch_op.add_column(sa.Column("upload_started_at", sa.DateTime(timezone=True), nullable=True))

    indexes = _indexes()
    if TOKEN_INDEX not in indexes:
        op.create_index(
            TOKEN_INDEX,
            "media_provider_bindings",
            ["upload_claim_token"],
            unique=False,
        )
    if EXPIRY_INDEX not in indexes:
        op.create_index(
            EXPIRY_INDEX,
            "media_provider_bindings",
            ["upload_claim_expires_at"],
            unique=False,
        )


def downgrade() -> None:
    columns = _columns()
    if not columns:
        return
    if "provider_media_id" in columns:
        unresolved = (
            op.get_bind()
            .execute(
                sa.text("SELECT id FROM media_provider_bindings WHERE provider_media_id IS NULL LIMIT 1")
            )
            .first()
        )
        if unresolved:
            raise RuntimeError(
                "provider media bindings contain unresolved uploads; reconcile them before downgrade"
            )

    indexes = _indexes()
    if EXPIRY_INDEX in indexes:
        op.drop_index(EXPIRY_INDEX, table_name="media_provider_bindings")
    if TOKEN_INDEX in indexes:
        op.drop_index(TOKEN_INDEX, table_name="media_provider_bindings")

    with op.batch_alter_table("media_provider_bindings") as batch_op:
        if "upload_started_at" in columns:
            batch_op.drop_column("upload_started_at")
        if "upload_claim_expires_at" in columns:
            batch_op.drop_column("upload_claim_expires_at")
        if "upload_claim_token" in columns:
            batch_op.drop_column("upload_claim_token")
        batch_op.alter_column(
            "provider_media_id",
            existing_type=sa.String(length=500),
            nullable=False,
        )
