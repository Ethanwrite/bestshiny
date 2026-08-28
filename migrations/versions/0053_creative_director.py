"""Creative director sessions: brief, key visuals, visual bible, beats, actions.

The creative director is stateful and structured. Dialogue turns, brief
revisions, the visual bible and the beat plan are all rows - never one
growing prompt string - and everything that spends money or touches the
production chain is a ``creative_actions`` row the API layer executes through
the existing admission / credit / router / gateway path. Brief revisions and
turns are append-only; the visual bible is versioned and a LOCKED version is
immutable by service contract, superseded only by a later version.

Revision ID: 0053_creative_director
Revises: 0052_shot_dependencies
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053_creative_director"
down_revision: str | None = "0052_shot_dependencies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "creative_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "workspace_id",
            sa.String(length=36),
            sa.ForeignKey("workspaces.id"),
            nullable=True,
            index=True,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("format", sa.String(length=40), nullable=False),
        sa.Column("current_brief_revision", sa.Integer(), nullable=False),
        sa.Column("current_bible_version", sa.Integer(), nullable=False),
        sa.Column("current_beat_revision", sa.Integer(), nullable=False),
        sa.Column(
            "compiled_episode_id",
            sa.String(length=36),
            sa.ForeignKey("episodes.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('INTAKE', 'CLARIFYING', 'BRIEF_PROPOSED', 'BRIEF_APPROVED', "
            "'VISUALS_IN_PROGRESS', 'BIBLE_PROPOSED', 'BIBLE_LOCKED', 'BEATS_PROPOSED', "
            "'COMPILED', 'ABANDONED')",
            name="ck_creative_session_status",
        ),
        sa.CheckConstraint(
            "format IN ('SHORT_DRAMA', 'ADVERTISEMENT', 'PRODUCT_SHOWCASE', 'SOCIAL_SHORT', "
            "'MUSIC_VISUAL', 'FASHION_LOOKBOOK', 'BEAUTY_TUTORIAL', 'CONCEPT_FILM', 'UNSPECIFIED')",
            name="ck_creative_session_format",
        ),
    )
    op.create_table(
        "creative_turns",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("creative_sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("speaker", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("questions_json", sa.JSON(), nullable=False),
        sa.Column("extracted_json", sa.JSON(), nullable=False),
        sa.Column("reasoner", sa.String(length=60), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("brief_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "sequence", name="uq_creative_turn_sequence"),
        sa.CheckConstraint("speaker IN ('USER', 'DIRECTOR')", name="ck_creative_turn_speaker"),
    )
    op.create_table(
        "creative_briefs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("creative_sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("fields_json", sa.JSON(), nullable=False),
        sa.Column("completeness_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "revision", name="uq_creative_brief_revision"),
        sa.CheckConstraint(
            "status IN ('PROPOSED', 'APPROVED', 'SUPERSEDED')", name="ck_creative_brief_status"
        ),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_creative_brief_hash_length"),
    )
    op.create_table(
        "visual_bibles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("creative_sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column(
            "brief_id", sa.String(length=36), sa.ForeignKey("creative_briefs.id"), nullable=False
        ),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "version", name="uq_visual_bible_version"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'LOCKED', 'SUPERSEDED')", name="ck_visual_bible_status"
        ),
        sa.CheckConstraint("version > 0", name="ck_visual_bible_version_positive"),
        sa.CheckConstraint(
            "status != 'LOCKED' OR locked_at IS NOT NULL", name="ck_visual_bible_locked_at"
        ),
    )
    op.create_table(
        "creative_visual_anchors",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("creative_sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("anchor_key", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("prompt_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column(
            "generation_job_id",
            sa.String(length=36),
            sa.ForeignKey("generation_jobs.id"),
            nullable=True,
        ),
        sa.Column(
            "media_asset_id",
            sa.String(length=36),
            sa.ForeignKey("media_assets.id"),
            nullable=True,
        ),
        sa.Column(
            "character_id", sa.String(length=36), sa.ForeignKey("characters.id"), nullable=True
        ),
        sa.Column("failure_code", sa.String(length=240), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "anchor_key", name="uq_creative_anchor_key"),
        sa.CheckConstraint(
            "kind IN ('CHARACTER', 'SCENE', 'STYLE', 'PRODUCT', 'PROP', 'MOOD')",
            name="ck_creative_anchor_kind",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'GENERATING', 'READY', 'FAILED')",
            name="ck_creative_anchor_status",
        ),
    )
    op.create_table(
        "creative_actions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("creative_sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=60), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=250), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "sequence", name="uq_creative_action_sequence"),
        sa.UniqueConstraint("idempotency_key", name="uq_creative_action_idempotency"),
        sa.CheckConstraint(
            "kind IN ('GENERATE_KEY_VISUAL', 'CREATE_EPISODE', 'COMPILE_EPISODE', "
            "'OPEN_OBLIGATION', 'ESTABLISH_FACT')",
            name="ck_creative_action_kind",
        ),
        sa.CheckConstraint(
            "status IN ('PROPOSED', 'EXECUTED', 'FAILED', 'SKIPPED')",
            name="ck_creative_action_status",
        ),
    )
    op.create_table(
        "creative_beats",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("creative_sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("beat_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "session_id", "plan_revision", "sequence", name="uq_creative_beat_sequence"
        ),
        sa.CheckConstraint(
            "status IN ('PROPOSED', 'APPROVED', 'SUPERSEDED')", name="ck_creative_beat_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("creative_beats")
    op.drop_table("creative_actions")
    op.drop_table("creative_visual_anchors")
    op.drop_table("visual_bibles")
    op.drop_table("creative_briefs")
    op.drop_table("creative_turns")
    op.drop_table("creative_sessions")
