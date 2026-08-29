"""Rendition garbage-collection lifecycle and access tracking.

Derived renditions were content-addressed and bounded but never retired
(OPEN_ISSUES 2.7): a provider that changes its limits accumulates one copy
per constraint set forever. Renditions now carry a lifecycle — ACTIVE serves,
GC_CLAIMED is a leased claim that keeps two sweepers from double-deleting,
DELETED is a tombstone recording what was removed (sha256, size, reason) so
deletion is reconcilable and the row can be revived in place when the same
constraints are needed again. last_accessed_at is the auditable idle signal
the sweep reads. Originals are never collected.

Revision ID: 0057_rendition_lifecycle
Revises: 0056_character_evidence_submissions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_rendition_lifecycle"
down_revision: str | None = "0056_character_evidence_submissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "media_renditions",
        sa.Column(
            "lifecycle_status", sa.String(length=20), nullable=False, server_default="ACTIVE"
        ),
    )
    op.add_column(
        "media_renditions", sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "media_renditions", sa.Column("gc_claim_id", sa.String(length=36), nullable=True)
    )
    op.add_column(
        "media_renditions", sa.Column("gc_claimed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "media_renditions", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "media_renditions", sa.Column("delete_reason", sa.String(length=240), nullable=True)
    )
    if op.get_bind().dialect.name != "sqlite":
        # SQLite adds a table-level CHECK only through a batch rebuild, and
        # reflecting this table's foreign key trips over the assetless
        # recovery snapshot (0035 created media_renditions there with a
        # dangling FK — SQLite never validated it). SQLite's schema gets the
        # constraint from ORM metadata via create_all; PostgreSQL, the only
        # supported runtime, gets it here.
        op.create_check_constraint(
            "ck_media_rendition_lifecycle",
            "media_renditions",
            "lifecycle_status IN ('ACTIVE', 'GC_CLAIMED', 'DELETED')",
        )
    op.create_index(
        "ix_media_rendition_gc", "media_renditions", ["lifecycle_status", "last_accessed_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_media_rendition_gc", table_name="media_renditions")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("ck_media_rendition_lifecycle", "media_renditions", type_="check")
    op.drop_column("media_renditions", "delete_reason")
    op.drop_column("media_renditions", "deleted_at")
    op.drop_column("media_renditions", "gc_claimed_at")
    op.drop_column("media_renditions", "gc_claim_id")
    op.drop_column("media_renditions", "last_accessed_at")
    op.drop_column("media_renditions", "lifecycle_status")
