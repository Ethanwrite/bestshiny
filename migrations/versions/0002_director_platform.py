"""Upgrade an existing video-platform database to the AI Director domain."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

from migrations.schema_snapshots.platform_v2 import metadata as V2_METADATA

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

ADDED_FOREIGN_KEYS: dict[str, list[tuple[str, list[str], str, list[str]]]] = {
    "projects": [
        ("fk_projects_workspace_id_workspaces", ["workspace_id"], "workspaces", ["id"]),
    ],
    "scenes": [
        ("fk_scenes_world_state_id_timeline_states", ["world_state_id"], "timeline_states", ["id"]),
    ],
    "shots": [
        ("fk_shots_next_shot_id_shots", ["next_shot_id"], "shots", ["id"]),
        ("fk_shots_input_state_id_timeline_states", ["input_state_id"], "timeline_states", ["id"]),
        ("fk_shots_output_state_id_timeline_states", ["output_state_id"], "timeline_states", ["id"]),
        (
            "fk_shots_committed_candidate_id_generation_candidates",
            ["committed_candidate_id"],
            "generation_candidates",
            ["id"],
        ),
    ],
    "media_assets": [
        ("fk_media_assets_parent_asset_id_media_assets", ["parent_asset_id"], "media_assets", ["id"]),
        (
            "fk_media_assets_generation_candidate_id_generation_candidates",
            ["generation_candidate_id"],
            "generation_candidates",
            ["id"],
        ),
    ],
    "generation_jobs": [
        (
            "fk_generation_jobs_candidate_id_generation_candidates",
            ["candidate_id"],
            "generation_candidates",
            ["id"],
        ),
    ],
}

ADDED_INDEXES: dict[str, list[tuple[str, list[str]]]] = {
    "projects": [("ix_projects_workspace_id", ["workspace_id"])],
    "media_assets": [
        ("ix_media_assets_parent_asset_id", ["parent_asset_id"]),
        ("ix_media_assets_generation_candidate_id", ["generation_candidate_id"]),
    ],
    "generation_jobs": [("ix_generation_jobs_candidate_id", ["candidate_id"])],
}


def _foreign_key_signatures(inspector: Inspector, table_name: str) -> set[tuple]:  # type: ignore[type-arg]
    return {
        (
            tuple(item["constrained_columns"]),
            item["referred_table"],
            tuple(item["referred_columns"]),
        )
        for item in inspector.get_foreign_keys(table_name)
    }


def _ensure_foreign_keys(bind) -> None:  # type: ignore[no-untyped-def]
    for table_name, specifications in ADDED_FOREIGN_KEYS.items():
        inspector = sa.inspect(bind)
        if table_name not in inspector.get_table_names():
            continue
        existing = _foreign_key_signatures(inspector, table_name)
        missing = [
            specification
            for specification in specifications
            if (tuple(specification[1]), specification[2], tuple(specification[3])) not in existing
        ]
        if not missing:
            continue
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(table_name) as batch_op:
                for name, local_columns, remote_table, remote_columns in missing:
                    batch_op.create_foreign_key(
                        name,
                        remote_table,
                        local_columns,
                        remote_columns,
                    )
        else:
            for name, local_columns, remote_table, remote_columns in missing:
                op.create_foreign_key(
                    name,
                    table_name,
                    remote_table,
                    local_columns,
                    remote_columns,
                )


def _ensure_indexes(bind) -> None:  # type: ignore[no-untyped-def]
    for table_name, specifications in ADDED_INDEXES.items():
        inspector = sa.inspect(bind)
        if table_name not in inspector.get_table_names():
            continue
        existing = {item["name"] for item in inspector.get_indexes(table_name)}
        for name, columns in specifications:
            if name not in existing:
                op.create_index(name, table_name, columns)


def upgrade() -> None:
    bind = op.get_bind()
    before = set(sa.inspect(bind).get_table_names())
    # Creates V2 tables missing from either an original or freshly frozen V1
    # database. Existing V1 tables are preserved and reconciled below.
    V2_METADATA.create_all(bind=bind, checkfirst=True)
    inspector = sa.inspect(bind)
    for table_name, columns in ADDED_COLUMNS.items():
        if table_name not in before:
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table_name, column)
    _ensure_foreign_keys(bind)
    _ensure_indexes(bind)


def downgrade() -> None:
    # V1 keeps this migration forward-only because it introduces canonical identity and audit records.
    pass
