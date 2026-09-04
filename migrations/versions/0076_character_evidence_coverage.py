"""Per-character coverage for the shadow Character Evidence analysis.

A candidate can bind several characters, but the dispatcher analysed only the
first one that resolved identity references and wrote the rest into a metadata
key nothing read - so a two-hander produced evidence for one face and silence
for the other, with no record of why.

``character_evidence_coverage`` is one row per character the analysis was asked
about: which references it was compared against, which producer run answered,
what similarity evidence came back, and whether it was covered, skipped for
want of references, or failed. The parent's unique candidate key stays exactly
as it is - one remote GPU job per candidate is still the guarantee - and this
table carries the same shadow-only check constraint, so nothing here can gate a
candidate commit.

Revision ID: 0076_character_evidence_coverage
Revises: 0075_legacy_creative_session_recovery
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0076_character_evidence_coverage"
down_revision: str | None = "0075_legacy_creative_session_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLE = "character_evidence_coverage"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "character_evidence_submissions" not in tables:
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return
    if NEW_TABLE in tables:
        raise RuntimeError(f"{NEW_TABLE} already exists before its own migration")
    op.create_table(
        NEW_TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "submission_id",
            sa.String(length=36),
            sa.ForeignKey("character_evidence_submissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=36),
            sa.ForeignKey("generation_candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="REQUESTED"),
        sa.Column("skip_reason", sa.String(length=240), nullable=True),
        sa.Column("reference_asset_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("producer_run_id", sa.String(length=64), nullable=True),
        sa.Column("qa_result_id", sa.String(length=36), nullable=True),
        sa.Column("decision", sa.String(length=40), nullable=True),
        sa.Column("similarity_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("operating_mode", sa.String(length=20), nullable=False, server_default="SHADOW"),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "submission_id", "character_id", name="uq_character_evidence_coverage_character"
        ),
        sa.CheckConstraint(
            "status IN ('REQUESTED', 'SKIPPED', 'REPORTED', 'FAILED')",
            name="ck_character_evidence_coverage_status",
        ),
        sa.CheckConstraint(
            "operating_mode = 'SHADOW'", name="ck_character_evidence_coverage_shadow_only"
        ),
    )
    op.create_index(f"ix_{NEW_TABLE}_submission_id", NEW_TABLE, ["submission_id"])
    op.create_index(f"ix_{NEW_TABLE}_candidate_id", NEW_TABLE, ["candidate_id"])
    op.create_index(f"ix_{NEW_TABLE}_qa_result_id", NEW_TABLE, ["qa_result_id"])
    op.create_index("ix_character_evidence_coverage_candidate", NEW_TABLE, ["candidate_id", "status"])


def downgrade() -> None:
    if NEW_TABLE not in _tables():
        return
    op.drop_index("ix_character_evidence_coverage_candidate", table_name=NEW_TABLE)
    op.drop_index(f"ix_{NEW_TABLE}_qa_result_id", table_name=NEW_TABLE)
    op.drop_index(f"ix_{NEW_TABLE}_candidate_id", table_name=NEW_TABLE)
    op.drop_index(f"ix_{NEW_TABLE}_submission_id", table_name=NEW_TABLE)
    op.drop_table(NEW_TABLE)
