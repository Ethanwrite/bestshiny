"""Add the narrative ledger: facts, per-holder disclosure and setup/payoff obligations.

Retrieval by similarity cannot answer "may this character know this yet?" or
"what does the series still owe the viewer?". Both are explicit, append-only
records so a 60-episode arc stays checkable rather than hopeful.

Revision ID: 0034_narrative_ledger
Revises: 0033_fixed_depay_pro_offer
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_narrative_ledger"
down_revision: str | None = "0033_fixed_depay_pro_offer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "narrative_facts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("fact_key", sa.String(length=160), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("fact_hash", sa.String(length=64), nullable=False),
        sa.Column("established_episode", sa.Integer(), nullable=False),
        sa.Column(
            "established_shot_id",
            sa.String(length=36),
            sa.ForeignKey("shots.id", ondelete="SET NULL"),
            index=True,
        ),
        sa.Column("subject_character_ids", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "fact_key", name="uq_narrative_fact_key"),
        sa.CheckConstraint("length(fact_key) > 0", name="ck_narrative_fact_key_nonempty"),
        sa.CheckConstraint("length(fact_hash) = 64", name="ck_narrative_fact_hash_length"),
        sa.CheckConstraint("established_episode > 0", name="ck_narrative_fact_episode_positive"),
    )
    op.create_index("ix_narrative_fact_lookup", "narrative_facts", ["project_id", "established_episode"])

    op.create_table(
        "narrative_disclosures",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "fact_id",
            sa.String(length=36),
            sa.ForeignKey("narrative_facts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("holder_key", sa.String(length=64), nullable=False),
        sa.Column("disclosed_episode", sa.Integer(), nullable=False),
        sa.Column(
            "disclosed_shot_id",
            sa.String(length=36),
            sa.ForeignKey("shots.id", ondelete="SET NULL"),
            index=True,
        ),
        sa.Column("channel", sa.String(length=40), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("fact_id", "holder_key", name="uq_narrative_disclosure_holder"),
        sa.CheckConstraint("length(holder_key) > 0", name="ck_narrative_disclosure_holder_nonempty"),
        sa.CheckConstraint("disclosed_episode > 0", name="ck_narrative_disclosure_episode_positive"),
    )
    op.create_index(
        "ix_narrative_disclosure_lookup",
        "narrative_disclosures",
        ["project_id", "holder_key", "disclosed_episode"],
    )

    op.create_table(
        "narrative_obligations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("obligation_key", sa.String(length=160), nullable=False),
        sa.Column("promise", sa.Text(), nullable=False),
        sa.Column("opened_episode", sa.Integer(), nullable=False),
        sa.Column(
            "opened_shot_id",
            sa.String(length=36),
            sa.ForeignKey("shots.id", ondelete="SET NULL"),
            index=True,
        ),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("settled_episode", sa.Integer()),
        sa.Column(
            "settled_shot_id",
            sa.String(length=36),
            sa.ForeignKey("shots.id", ondelete="SET NULL"),
            index=True,
        ),
        sa.Column("settled_reason", sa.Text()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "obligation_key", name="uq_narrative_obligation_key"),
        sa.CheckConstraint("length(obligation_key) > 0", name="ck_narrative_obligation_key_nonempty"),
        sa.CheckConstraint("opened_episode > 0", name="ck_narrative_obligation_open_positive"),
        sa.CheckConstraint(
            "settled_episode IS NULL OR settled_episode >= opened_episode",
            name="ck_narrative_obligation_settled_after_open",
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'SETTLED', 'ABANDONED')",
            name="ck_narrative_obligation_status",
        ),
        sa.CheckConstraint(
            "(status = 'OPEN' AND settled_episode IS NULL) OR "
            "(status != 'OPEN' AND settled_episode IS NOT NULL)",
            name="ck_narrative_obligation_status_settled_pair",
        ),
    )
    op.create_index(
        "ix_narrative_obligation_open",
        "narrative_obligations",
        ["project_id", "status", "opened_episode"],
    )


def downgrade() -> None:
    op.drop_index("ix_narrative_obligation_open", table_name="narrative_obligations")
    op.drop_table("narrative_obligations")
    op.drop_index("ix_narrative_disclosure_lookup", table_name="narrative_disclosures")
    op.drop_table("narrative_disclosures")
    op.drop_index("ix_narrative_fact_lookup", table_name="narrative_facts")
    op.drop_table("narrative_facts")
