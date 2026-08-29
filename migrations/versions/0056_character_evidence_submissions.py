"""Durable Character Evidence submission tracking, one shadow job per candidate.

Previously a submission existed only as candidate metadata written after a
successful POST: nothing enqueued work when a candidate's video output was
registered, a crash between POST and metadata write could resubmit, and an
acceptance that never called back was invisible. This table is the durable
lifecycle: PENDING (enqueued) -> ACCEPTED (202, which is never evidence) ->
REPORTED / FAILED (signed callback), with SKIPPED for candidates that cannot
be covered and RECONCILIATION_REQUIRED for acceptances whose callback never
arrived. The unique candidate key makes dispatch idempotent, and a check
constraint keeps the table shadow-only.

Revision ID: 0056_character_evidence_submissions
Revises: 0055_narrative_positions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_character_evidence_submissions"
down_revision: str | None = "0055_narrative_positions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "character_evidence_submissions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=36),
            sa.ForeignKey("generation_candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "shot_id",
            sa.String(length=36),
            sa.ForeignKey("shots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("character_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("operating_mode", sa.String(length=20), nullable=False),
        sa.Column("threshold_version", sa.String(length=120), nullable=False),
        sa.Column("submission_count", sa.Integer(), nullable=False),
        sa.Column("first_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_callback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("skip_reason", sa.String(length=240), nullable=True),
        sa.Column("reconciliation_note", sa.Text(), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_by", sa.String(length=120), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("candidate_id", name="uq_character_evidence_submission_candidate"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'REPORTED', 'FAILED', 'SKIPPED', "
            "'RECONCILIATION_REQUIRED')",
            name="ck_character_evidence_submission_status",
        ),
        sa.CheckConstraint(
            "operating_mode = 'SHADOW'",
            name="ck_character_evidence_submission_shadow_only",
        ),
        sa.CheckConstraint(
            "submission_count >= 0",
            name="ck_character_evidence_submission_count",
        ),
    )
    op.create_index(
        "ix_character_evidence_submission_status",
        "character_evidence_submissions",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_character_evidence_submission_status", table_name="character_evidence_submissions"
    )
    op.drop_table("character_evidence_submissions")
