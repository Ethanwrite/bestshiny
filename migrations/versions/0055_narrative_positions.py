"""Complete narrative positions on the ledger, and declared shot effects.

The ledger previously ordered everything by episode alone, so a fact disclosed
in a later shot of an episode was visible to that episode's earlier shots, and
an obligation's settlement overwrote the only record of when it had been open.
Facts, disclosures and obligations now carry (episode, scene_sequence,
shot_sequence); 0 sequences mean "start of episode", which preserves the old
episode-granular reading for existing rows while real shots are 1-based.
Settlement keeps its own position, so a historical read at an earlier position
still sees the obligation open.

shot_narrative_effects records what committing a shot does to the ledger
(establish/disclose facts, open/settle obligations); the ledger rows are
written inside the candidate-commit transaction, exactly once, and the
application is recorded on the effect row.

Revision ID: 0055_narrative_positions
Revises: 0054_episode_continuations
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055_narrative_positions"
down_revision: str | None = "0054_episode_continuations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "narrative_facts",
        sa.Column(
            "established_scene_sequence", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "narrative_facts",
        sa.Column(
            "established_shot_sequence", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "narrative_disclosures",
        sa.Column("disclosed_scene_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "narrative_disclosures",
        sa.Column("disclosed_shot_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "narrative_obligations",
        sa.Column("opened_scene_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "narrative_obligations",
        sa.Column("opened_shot_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "narrative_obligations", sa.Column("settled_scene_sequence", sa.Integer(), nullable=True)
    )
    op.add_column(
        "narrative_obligations", sa.Column("settled_shot_sequence", sa.Integer(), nullable=True)
    )

    op.create_table(
        "shot_narrative_effects",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "shot_id",
            sa.String(length=36),
            sa.ForeignKey("shots.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("effect_type", sa.String(length=40), nullable=False),
        sa.Column("episode_number", sa.Integer(), nullable=False),
        sa.Column("scene_sequence", sa.Integer(), nullable=False),
        sa.Column("shot_sequence", sa.Integer(), nullable=False),
        sa.Column("fact_key", sa.String(length=160), nullable=True),
        sa.Column("obligation_key", sa.String(length=160), nullable=True),
        sa.Column("holder_key", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("channel", sa.String(length=40), nullable=False),
        sa.Column("disclose_to", sa.JSON(), nullable=False),
        sa.Column("subject_character_ids", sa.JSON(), nullable=False),
        sa.Column("origin", sa.String(length=40), nullable=False),
        sa.Column("effect_key", sa.String(length=420), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_candidate_id", sa.String(length=36), nullable=True, index=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("shot_id", "effect_key", name="uq_shot_narrative_effect_key"),
        sa.CheckConstraint(
            "effect_type IN ('ESTABLISH_FACT', 'DISCLOSE_FACT', "
            "'OPEN_OBLIGATION', 'SETTLE_OBLIGATION')",
            name="ck_shot_narrative_effect_type",
        ),
        sa.CheckConstraint(
            "origin IN ('SCRIPT_COMPILER', 'MANUAL', 'CREATIVE_DIRECTOR', "
            "'EPISODE_CONTINUATION')",
            name="ck_shot_narrative_effect_origin",
        ),
        sa.CheckConstraint(
            "effect_type NOT IN ('ESTABLISH_FACT', 'DISCLOSE_FACT') OR fact_key IS NOT NULL",
            name="ck_shot_narrative_effect_fact_referent",
        ),
        sa.CheckConstraint(
            "effect_type NOT IN ('OPEN_OBLIGATION', 'SETTLE_OBLIGATION') "
            "OR obligation_key IS NOT NULL",
            name="ck_shot_narrative_effect_obligation_referent",
        ),
        sa.CheckConstraint(
            "episode_number > 0 AND scene_sequence > 0 AND shot_sequence > 0",
            name="ck_shot_narrative_effect_position",
        ),
    )
    op.create_index(
        "ix_shot_narrative_effect_fact", "shot_narrative_effects", ["project_id", "fact_key"]
    )
    op.create_index(
        "ix_shot_narrative_effect_obligation",
        "shot_narrative_effects",
        ["project_id", "obligation_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_shot_narrative_effect_obligation", table_name="shot_narrative_effects")
    op.drop_index("ix_shot_narrative_effect_fact", table_name="shot_narrative_effects")
    op.drop_table("shot_narrative_effects")
    op.drop_column("narrative_obligations", "settled_shot_sequence")
    op.drop_column("narrative_obligations", "settled_scene_sequence")
    op.drop_column("narrative_obligations", "opened_shot_sequence")
    op.drop_column("narrative_obligations", "opened_scene_sequence")
    op.drop_column("narrative_disclosures", "disclosed_shot_sequence")
    op.drop_column("narrative_disclosures", "disclosed_scene_sequence")
    op.drop_column("narrative_facts", "established_shot_sequence")
    op.drop_column("narrative_facts", "established_scene_sequence")
