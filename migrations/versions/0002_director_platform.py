"""Upgrade an existing video-platform database to the AI Director domain."""

import sqlalchemy as sa
from alembic import op
from production_domain.models import Base

revision = "0002_director_platform"
down_revision = "0001_platform_v1"
branch_labels = None
depends_on = None


ADDED_COLUMNS: dict[str, list[sa.Column]] = {
    "projects": [
        sa.Column("workspace_id", sa.String(36), nullable=True),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("default_aspect_ratio", sa.String(20), nullable=False, server_default="9:16"),
        sa.Column("default_provider", sa.String(80), nullable=False, server_default="google_flow"),
        sa.Column("default_language", sa.String(30), nullable=False, server_default="zh-CN"),
    ],
    "episodes": [
        sa.Column("script_source", sa.Text(), nullable=False, server_default=""),
        sa.Column("script_structured", sa.JSON(), nullable=False, server_default="{}"),
    ],
    "scenes": [
        sa.Column("time_context", sa.String(120), nullable=False, server_default=""),
        sa.Column("scene_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("world_state_id", sa.String(36), nullable=True),
        sa.Column("lighting_preset_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(40), nullable=False, server_default="PLANNED"),
    ],
    "shots": [
        sa.Column("shot_type", sa.String(60), nullable=False, server_default="MEDIUM"),
        sa.Column("user_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("compiled_prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("next_shot_id", sa.String(36), nullable=True),
        sa.Column("input_state_id", sa.String(36), nullable=True),
        sa.Column("output_state_id", sa.String(36), nullable=True),
        sa.Column("camera_state_id", sa.String(36), nullable=True),
        sa.Column("lighting_state_id", sa.String(36), nullable=True),
        sa.Column("blocking_state_id", sa.String(36), nullable=True),
        sa.Column("committed_candidate_id", sa.String(36), nullable=True),
        sa.Column("continuity_policy", sa.String(60), nullable=False, server_default="HYBRID"),
        sa.Column("generation_policy", sa.String(60), nullable=False, server_default="TEXT_TO_VIDEO"),
        sa.Column("preferred_provider", sa.String(80), nullable=False, server_default="google_flow"),
        sa.Column("preferred_model", sa.String(120), nullable=False, server_default="veo"),
    ],
    "media_assets": [
        sa.Column("parent_asset_id", sa.String(36), nullable=True),
        sa.Column("generation_candidate_id", sa.String(36), nullable=True),
    ],
    "generation_jobs": [
        sa.Column("candidate_id", sa.String(36), nullable=True),
        sa.Column("provider_request_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("policy", sa.String(60), nullable=False, server_default="TEXT_TO_VIDEO"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cost_estimate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("actual_cost", sa.Float(), nullable=False, server_default="0"),
    ],
}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    before = set(sa.inspect(bind).get_table_names())
    Base.metadata.create_all(bind=bind, checkfirst=True)
    inspector = sa.inspect(bind)
    for table_name, columns in ADDED_COLUMNS.items():
        if table_name not in before:
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table_name, column)


def downgrade() -> None:
    # V1 keeps this migration forward-only because it introduces canonical identity and audit records.
    pass
