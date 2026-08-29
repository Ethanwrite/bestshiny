"""Asynchronous full-content verification for directly uploaded media.

A direct upload was adopted from a HEAD plus a 64 KB header read
(OPEN_ISSUES 2.10): a truncated or internally corrupt file registered fine
and failed later, at first use. Assets now carry a verification status —
paths that validate full bytes inline register READY unchanged; a direct
upload registers PENDING_VERIFICATION, the async verifier claims it to
VERIFYING under a lease (crash-recoverable) and promotes it only after a
complete decode (Pillow for images, ffprobe + full-stream ffmpeg decode for
videos) plus a stored-object SHA re-check. Files that do not decode become
INVALID; files whose bytes contradict their declaration (forged MIME, SHA
mismatch) become QUARANTINED. Providers and build chains refuse anything
that is not READY.

Revision ID: 0058_media_verification
Revises: 0057_rendition_lifecycle
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058_media_verification"
down_revision: str | None = "0057_rendition_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "media_assets" not in set(sa.inspect(op.get_bind()).get_table_names()):
        # The assetless recovery snapshot (see 0008/0028): a database restored
        # without the media tables skips media-only migrations entirely.
        return
    op.add_column(
        "media_assets",
        sa.Column(
            "verification_status", sa.String(length=30), nullable=False, server_default="READY"
        ),
    )
    op.add_column(
        "media_assets",
        sa.Column("verification_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "media_assets", sa.Column("verification_error", sa.String(length=500), nullable=True)
    )
    if op.get_bind().dialect.name != "sqlite":
        # SQLite adds a table-level CHECK only by rebuilding the table, and the
        # rebuild trips the plpgsql-equivalent triggers that reference
        # media_assets. SQLite is the development engine; its schema gets the
        # constraint from the ORM metadata via create_all, and PostgreSQL — the
        # only supported runtime — gets it here.
        op.create_check_constraint(
            "ck_media_asset_verification_status",
            "media_assets",
            "verification_status IN ('READY', 'PENDING_VERIFICATION', 'VERIFYING', "
            "'INVALID', 'QUARANTINED')",
        )
    op.create_index(
        "ix_media_asset_verification",
        "media_assets",
        ["verification_status", "verification_claimed_at"],
    )


def downgrade() -> None:
    if "media_assets" not in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.drop_index("ix_media_asset_verification", table_name="media_assets")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint(
            "ck_media_asset_verification_status", "media_assets", type_="check"
        )
    op.drop_column("media_assets", "verification_error")
    op.drop_column("media_assets", "verification_claimed_at")
    op.drop_column("media_assets", "verification_status")
