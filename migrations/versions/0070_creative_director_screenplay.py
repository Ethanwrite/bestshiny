"""The creative director writes: screenplay revisions, provenance, question states, lineage.

The director's dialogue now records per-field provenance and a per-question
state ledger on every brief revision, an idempotency key, the Director Skill
version and the model execution behind every turn; the DIRECTOR model authors
versioned screenplay revisions (treatment, beats, dialogue, one-action shot
intents) that the key visuals are derived from; anchors are versioned by
content and can be skipped or superseded on record; the visual bible records
the CharacterIdentityVersions and ProjectStyleLock its lock produced; and
every compiled shot gets a lineage row back to the exact approved brief,
screenplay, bible and anchors.

Revision ID: 0070_creative_director_screenplay
Revises: 0069_production_budget
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070_creative_director_screenplay"
down_revision: str | None = "0069_production_budget"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CREATIVE_TABLES = (
    "creative_sessions",
    "creative_turns",
    "creative_briefs",
    "visual_bibles",
    "creative_visual_anchors",
    "creative_actions",
    "creative_beats",
)

SESSION_STATUS_CHECK = (
    "status IN ('INTAKE', 'CLARIFYING', 'BRIEF_PROPOSED', 'BRIEF_APPROVED', "
    "'SCREENPLAY_PROPOSED', 'SCREENPLAY_APPROVED', "
    "'VISUALS_IN_PROGRESS', 'BIBLE_PROPOSED', 'BIBLE_LOCKED', 'BEATS_PROPOSED', "
    "'COMPILED', 'ABANDONED')"
)
LEGACY_SESSION_STATUS_CHECK = (
    "status IN ('INTAKE', 'CLARIFYING', 'BRIEF_PROPOSED', 'BRIEF_APPROVED', "
    "'VISUALS_IN_PROGRESS', 'BIBLE_PROPOSED', 'BIBLE_LOCKED', 'BEATS_PROPOSED', "
    "'COMPILED', 'ABANDONED')"
)
ANCHOR_STATUS_CHECK = (
    "status IN ('PENDING', 'GENERATING', 'READY', 'FAILED', 'SKIPPED', 'SUPERSEDED')"
)
LEGACY_ANCHOR_STATUS_CHECK = "status IN ('PENDING', 'GENERATING', 'READY', 'FAILED')"
ACTION_KIND_CHECK = (
    "kind IN ('GENERATE_KEY_VISUAL', 'CREATE_EPISODE', 'COMPILE_EPISODE', "
    "'OPEN_OBLIGATION', 'ESTABLISH_FACT', 'LOCK_CHARACTER_IDENTITY', 'LOCK_PROJECT_STYLE')"
)
LEGACY_ACTION_KIND_CHECK = (
    "kind IN ('GENERATE_KEY_VISUAL', 'CREATE_EPISODE', 'COMPILE_EPISODE', "
    "'OPEN_OBLIGATION', 'ESTABLISH_FACT')"
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


PLATFORM_TABLES = ("projects", "episodes", "shots", "media_assets", "characters", "generation_jobs")


def _is_platform_database(tables: set[str]) -> bool:
    """Historical integrity fixtures carry only the tables owned by the revision
    under test (SQLite lets 0053 create the creative tables without their
    foreign-key targets); they are not deployable platform databases, and batch
    reflection of a table whose referents are missing cannot succeed there."""

    return set(CREATIVE_TABLES).issubset(tables) and set(PLATFORM_TABLES).issubset(tables)


def upgrade() -> None:
    tables = _tables()
    if not _is_platform_database(tables):
        return
    if "creative_screenplays" in tables or "creative_shot_lineage" in tables:
        raise RuntimeError("creative screenplay migration found partial pre-existing tables")

    # -- sessions: the two screenplay stages and the screenplay head pointer.
    with op.batch_alter_table("creative_sessions") as batch_op:
        batch_op.drop_constraint("ck_creative_session_status", type_="check")
        batch_op.add_column(
            sa.Column(
                "current_screenplay_revision",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_check_constraint("ck_creative_session_status", SESSION_STATUS_CHECK)

    # -- turns: idempotency, skill provenance, model execution, context audit.
    with op.batch_alter_table("creative_turns") as batch_op:
        batch_op.add_column(sa.Column("client_turn_id", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("skill_version", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("skill_content_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("model_execution_record_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("context_json", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.add_column(
            sa.Column("result_json", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.create_unique_constraint(
            "uq_creative_turn_client_id", ["session_id", "client_turn_id"]
        )

    # -- briefs: field provenance, question ledger, source.
    with op.batch_alter_table("creative_briefs") as batch_op:
        batch_op.add_column(
            sa.Column("provenance_json", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.add_column(
            sa.Column("question_state_json", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.add_column(
            sa.Column("source", sa.String(length=40), nullable=False, server_default="TURN")
        )
        batch_op.add_column(sa.Column("turn_id", sa.String(length=36), nullable=True))

    # -- screenplay revisions (model-authored, versioned, approved exactly).
    op.create_table(
        "creative_screenplays",
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
        sa.Column(
            "brief_id", sa.String(length=36), sa.ForeignKey("creative_briefs.id"), nullable=False
        ),
        sa.Column("reasoner", sa.String(length=60), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=True),
        sa.Column("user_notes", sa.Text(), nullable=False),
        sa.Column("skill_version", sa.String(length=80), nullable=True),
        sa.Column("skill_content_hash", sa.String(length=64), nullable=True),
        sa.Column("model_execution_record_id", sa.String(length=36), nullable=True),
        sa.Column("content_json", sa.JSON(), nullable=False),
        sa.Column("script_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "revision", name="uq_creative_screenplay_revision"),
        sa.CheckConstraint(
            "status IN ('PROPOSED', 'APPROVED', 'SUPERSEDED')",
            name="ck_creative_screenplay_status",
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name="ck_creative_screenplay_hash_length"
        ),
        sa.CheckConstraint("revision > 0", name="ck_creative_screenplay_revision_positive"),
    )

    # -- visual bibles: the screenplay they were drawn from and the lock lineage.
    with op.batch_alter_table("visual_bibles") as batch_op:
        batch_op.add_column(sa.Column("screenplay_id", sa.String(length=36), nullable=True))
        batch_op.add_column(
            sa.Column("lineage_json", sa.JSON(), nullable=False, server_default="{}")
        )
        batch_op.create_foreign_key(
            "fk_visual_bibles_screenplay_id_creative_screenplays",
            "creative_screenplays",
            ["screenplay_id"],
            ["id"],
        )

    # -- anchors: content versions, required flag, skip reason, lineage.
    with op.batch_alter_table("creative_visual_anchors") as batch_op:
        batch_op.drop_constraint("uq_creative_anchor_key", type_="unique")
        batch_op.drop_constraint("ck_creative_anchor_status", type_="check")
        batch_op.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("prompt_hash", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(sa.Column("skip_reason", sa.String(length=240), nullable=True))
        batch_op.add_column(sa.Column("brief_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("screenplay_id", sa.String(length=36), nullable=True))
        batch_op.create_unique_constraint(
            "uq_creative_anchor_key", ["session_id", "anchor_key", "version"]
        )
        batch_op.create_check_constraint("ck_creative_anchor_status", ANCHOR_STATUS_CHECK)
        batch_op.create_check_constraint("ck_creative_anchor_version_positive", "version > 0")
        batch_op.create_foreign_key(
            "fk_creative_visual_anchors_brief_id_creative_briefs",
            "creative_briefs",
            ["brief_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_creative_visual_anchors_screenplay_id_creative_screenplays",
            "creative_screenplays",
            ["screenplay_id"],
            ["id"],
        )

    # -- actions: the two lock kinds.
    with op.batch_alter_table("creative_actions") as batch_op:
        batch_op.drop_constraint("ck_creative_action_kind", type_="check")
        batch_op.create_check_constraint("ck_creative_action_kind", ACTION_KIND_CHECK)

    # -- beats: which screenplay revision they were materialized from.
    with op.batch_alter_table("creative_beats") as batch_op:
        batch_op.add_column(sa.Column("screenplay_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_creative_beats_screenplay_id_creative_screenplays",
            "creative_screenplays",
            ["screenplay_id"],
            ["id"],
        )

    # -- per-shot lineage.
    op.create_table(
        "creative_shot_lineage",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=36),
            sa.ForeignKey("creative_sessions.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "shot_id", sa.String(length=36), sa.ForeignKey("shots.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "episode_id",
            sa.String(length=36),
            sa.ForeignKey("episodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "brief_id", sa.String(length=36), sa.ForeignKey("creative_briefs.id"), nullable=False
        ),
        sa.Column(
            "screenplay_id",
            sa.String(length=36),
            sa.ForeignKey("creative_screenplays.id"),
            nullable=False,
        ),
        sa.Column("bible_id", sa.String(length=36), sa.ForeignKey("visual_bibles.id"), nullable=True),
        sa.Column("beat_sequence", sa.Integer(), nullable=False),
        sa.Column("shot_sequence", sa.Integer(), nullable=False),
        sa.Column("anchor_ids", sa.JSON(), nullable=False),
        sa.Column("identity_version_ids", sa.JSON(), nullable=False),
        sa.Column("style_lock_id", sa.String(length=36), nullable=True),
        sa.Column("intent_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("shot_id", name="uq_creative_shot_lineage_shot"),
    )


def downgrade() -> None:
    tables = _tables()
    if not _is_platform_database(tables):
        return
    bind = op.get_bind()
    if "creative_screenplays" in tables:
        populated = bind.execute(sa.text("SELECT 1 FROM creative_screenplays LIMIT 1")).scalar()
        if populated is not None:
            raise RuntimeError(
                "creative screenplay downgrade refuses to drop authored screenplay revisions"
            )
    if "creative_shot_lineage" in tables:
        op.drop_index("ix_creative_shot_lineage_session_id", table_name="creative_shot_lineage")
        op.drop_table("creative_shot_lineage")

    with op.batch_alter_table("creative_beats") as batch_op:
        batch_op.drop_constraint(
            "fk_creative_beats_screenplay_id_creative_screenplays", type_="foreignkey"
        )
        batch_op.drop_column("screenplay_id")

    with op.batch_alter_table("creative_actions") as batch_op:
        batch_op.drop_constraint("ck_creative_action_kind", type_="check")
        batch_op.create_check_constraint("ck_creative_action_kind", LEGACY_ACTION_KIND_CHECK)

    with op.batch_alter_table("creative_visual_anchors") as batch_op:
        batch_op.drop_constraint(
            "fk_creative_visual_anchors_screenplay_id_creative_screenplays", type_="foreignkey"
        )
        batch_op.drop_constraint(
            "fk_creative_visual_anchors_brief_id_creative_briefs", type_="foreignkey"
        )
        batch_op.drop_constraint("ck_creative_anchor_version_positive", type_="check")
        batch_op.drop_constraint("ck_creative_anchor_status", type_="check")
        batch_op.drop_constraint("uq_creative_anchor_key", type_="unique")
        batch_op.drop_column("screenplay_id")
        batch_op.drop_column("brief_id")
        batch_op.drop_column("skip_reason")
        batch_op.drop_column("required")
        batch_op.drop_column("prompt_hash")
        batch_op.drop_column("version")
        batch_op.create_unique_constraint("uq_creative_anchor_key", ["session_id", "anchor_key"])
        batch_op.create_check_constraint("ck_creative_anchor_status", LEGACY_ANCHOR_STATUS_CHECK)

    with op.batch_alter_table("visual_bibles") as batch_op:
        batch_op.drop_constraint(
            "fk_visual_bibles_screenplay_id_creative_screenplays", type_="foreignkey"
        )
        batch_op.drop_column("lineage_json")
        batch_op.drop_column("screenplay_id")

    if "creative_screenplays" in tables:
        op.drop_index("ix_creative_screenplays_session_id", table_name="creative_screenplays")
        op.drop_table("creative_screenplays")

    with op.batch_alter_table("creative_briefs") as batch_op:
        batch_op.drop_column("turn_id")
        batch_op.drop_column("source")
        batch_op.drop_column("question_state_json")
        batch_op.drop_column("provenance_json")

    with op.batch_alter_table("creative_turns") as batch_op:
        batch_op.drop_constraint("uq_creative_turn_client_id", type_="unique")
        batch_op.drop_column("result_json")
        batch_op.drop_column("context_json")
        batch_op.drop_column("model_execution_record_id")
        batch_op.drop_column("skill_content_hash")
        batch_op.drop_column("skill_version")
        batch_op.drop_column("client_turn_id")

    with op.batch_alter_table("creative_sessions") as batch_op:
        batch_op.drop_constraint("ck_creative_session_status", type_="check")
        batch_op.drop_column("current_screenplay_revision")
        batch_op.create_check_constraint("ck_creative_session_status", LEGACY_SESSION_STATUS_CHECK)
