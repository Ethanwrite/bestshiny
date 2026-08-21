"""Persist the authoritative model capability and manual-prior registry.

Revision ID: 0026_model_capability_registry
Revises: 0025_flow_project_affinity
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0026_model_capability_registry"
down_revision: str | None = "0025_flow_project_affinity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_REVIEWED_VIDEO: dict[str, dict[str, object]] = {
    "kling-3-standard-openrouter": {
        "profile_version": "kling-3.0-manual-v1",
        "flags": {
            "t2v",
            "i2v",
            "reference_image",
            "multi_reference",
            "start_frame",
            "end_frame",
            "start_end",
            "character_reference",
            "camera_instruction",
            "audio",
            "text_rendering",
        },
        "max_reference_images": 4,
        "resolutions": ["720p", "1080p"],
        "priors": [0.90, 0.88, 0.88, 0.82, 0.92, 0.78, 0.82],
        "adapter": "kling",
    },
    "kling-3-pro-openrouter": {
        "profile_version": "kling-3.0-pro-manual-v1",
        "flags": {
            "t2v",
            "i2v",
            "reference_image",
            "multi_reference",
            "start_frame",
            "end_frame",
            "start_end",
            "character_reference",
            "camera_instruction",
            "audio",
            "text_rendering",
        },
        "max_reference_images": 4,
        "resolutions": ["720p", "1080p"],
        "priors": [0.92, 0.90, 0.90, 0.88, 0.93, 0.80, 0.84],
        "adapter": "kling",
    },
    "flow-veo-3.1-internal": {
        "profile_version": "flow-veo-3.1-manual-v1",
        "flags": {
            "t2v",
            "i2v",
            "v2v",
            "reference_image",
            "multi_reference",
            "start_frame",
            "end_frame",
            "start_end",
            "character_reference",
            "video_extension",
            "camera_instruction",
            "audio",
        },
        "max_reference_images": 3,
        "resolutions": ["720p", "1080p"],
        "priors": [0.89, 0.84, 0.90, 0.94, 0.81, 0.75, 0.54],
        "adapter": "veo",
    },
    "seedance-2.5-official": {
        "profile_version": "seedance-2.5-manual-v1",
        "flags": {
            "t2v",
            "i2v",
            "v2v",
            "reference_image",
            "multi_reference",
            "start_frame",
            "camera_instruction",
            "audio",
        },
        "max_reference_images": 4,
        "resolutions": ["720p", "1080p"],
        "priors": [0.84, 0.82, 0.90, 0.86, 0.88, 0.88, 0.58],
        "adapter": "seedance",
    },
    "veo-3.1-quality-official": {
        "profile_version": "veo-3.1-manual-v1",
        "flags": {
            "t2v",
            "i2v",
            "reference_image",
            "multi_reference",
            "start_frame",
            "end_frame",
            "start_end",
            "character_reference",
            "camera_instruction",
            "audio",
        },
        "max_reference_images": 3,
        "resolutions": ["720p", "1080p"],
        "priors": [0.88, 0.82, 0.88, 0.96, 0.80, 0.76, 0.55],
        "adapter": "veo",
    },
    "grok-video-official": {
        "profile_version": "grok-video-manual-v1",
        "flags": {"t2v", "i2v", "start_frame", "camera_instruction", "audio", "text_rendering"},
        "max_reference_images": 0,
        "resolutions": ["720p"],
        "priors": [0.66, 0.66, 0.62, 0.72, 0.68, 0.84, 0.88],
        "adapter": "grok",
    },
    "wan-2.7-official": {
        "profile_version": "wan-2.7-manual-v1",
        "flags": {"t2v", "camera_instruction"},
        "max_reference_images": 0,
        "resolutions": ["720p", "1080p"],
        "priors": [0.50, 0.50, 0.50, 0.50, 0.50, 0.50, 0.50],
        "adapter": "wan",
    },
}

_MANUAL_COSTS: dict[str, dict[str, float]] = {
    "kling-3-standard-openrouter": {"normalized": 0.58, "estimated_per_second": 0.10},
    "kling-3-pro-openrouter": {"normalized": 0.72, "estimated_per_second": 0.14},
    "flow-veo-3.1-internal": {"normalized": 0.82, "estimated_per_second": 0.20},
    "flow-narwhal-image-internal": {"normalized": 0.50, "estimated_per_image": 0.04},
    "seedance-2.5-official": {"normalized": 0.50, "estimated_per_second": 0.09},
    "veo-3.1-quality-official": {"normalized": 0.90, "estimated_per_second": 0.22},
    "grok-video-official": {"normalized": 0.25, "estimated_per_second": 0.04},
    "wan-2.7-official": {"normalized": 0.42, "estimated_per_second": 0.07},
}

_FLAG_NAMES = (
    "t2v",
    "i2v",
    "v2v",
    "reference_image",
    "multi_reference",
    "start_frame",
    "end_frame",
    "start_end",
    "character_reference",
    "video_extension",
    "camera_instruction",
    "audio",
    "text_rendering",
)


def _migration_profile(
    definition: Mapping[str, object],
    *,
    timestamp: datetime,
) -> dict[str, object]:
    logical_name = str(definition["logical_name"])
    capabilities = list(definition["capabilities"] or [])  # type: ignore[arg-type]
    reviewed = _REVIEWED_VIDEO.get(logical_name, {})
    flags = set(reviewed.get("flags", set()))
    priors = list(reviewed.get("priors", [0.5] * 7))
    return {
        "model_definition_id": definition["id"],
        "profile_version": reviewed.get("profile_version", "migration-conservative-v1"),
        "confidence_level": "initial",
        "supported_operations": capabilities,
        "supports_image_generation": "image_generation" in capabilities,
        "supports_video_generation": "video_generation" in capabilities,
        **{f"supports_{name}": name in flags for name in _FLAG_NAMES},
        "max_reference_images": reviewed.get("max_reference_images", 0),
        "min_duration": 1.0 if definition["modality"] == "video" else None,
        "max_duration": definition["max_duration"],
        "supported_aspect_ratios": definition["supported_aspect_ratios"],
        "supported_resolutions": reviewed.get("resolutions", []),
        "physics_prior": priors[0],
        "identity_prior": priors[1],
        "camera_prior": priors[2],
        "render_prior": priors[3],
        "action_prior": priors[4],
        "dialogue_prior": priors[5],
        "text_render_prior": priors[6],
        "provider_metadata": {
            "adapter": reviewed.get("adapter", definition["provider"]),
            "cost": _MANUAL_COSTS.get(logical_name, {}),
            "migration_backfill": True,
        },
        "source": "MANUAL_PRIOR",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _assert_downgrade_is_lossless() -> None:
    """Allow dropping only the exact, reproducible migration projection.

    Profile facts are authoritative after this revision.  A downgrade may
    remove rows generated mechanically from ``model_definitions``, because the
    same migration recreates them.  Any operator-created/deleted/edited fact is
    not reproducible and must never be silently discarded.
    """

    connection = op.get_bind()
    metadata = sa.MetaData()
    definitions = sa.Table("model_definitions", metadata, autoload_with=connection)
    profiles = sa.Table("model_capability_profiles", metadata, autoload_with=connection)
    definition_rows = list(connection.execute(sa.select(definitions)).mappings())
    profile_rows = list(connection.execute(sa.select(profiles)).mappings())
    comparison_timestamp = datetime(2000, 1, 1, tzinfo=UTC)
    expected = {
        str(definition["id"]): _migration_profile(
            definition,
            timestamp=comparison_timestamp,
        )
        for definition in definition_rows
    }
    actual = {str(profile["model_definition_id"]): profile for profile in profile_rows}
    if set(actual) != set(expected):
        raise RuntimeError("Model capability downgrade would discard non-deterministic profile rows")
    fact_columns = tuple(
        column.name for column in profiles.columns if column.name not in {"created_at", "updated_at"}
    )
    for model_definition_id, expected_profile in expected.items():
        actual_profile = actual[model_definition_id]
        for column_name in fact_columns:
            if actual_profile[column_name] != expected_profile[column_name]:
                raise RuntimeError(
                    "Model capability downgrade would discard an operator-edited profile: "
                    f"{model_definition_id}.{column_name}"
                )


def upgrade() -> None:
    op.create_table(
        "model_capability_profiles",
        sa.Column("model_definition_id", sa.String(length=36), nullable=False),
        sa.Column("profile_version", sa.String(length=80), nullable=False),
        sa.Column("confidence_level", sa.String(length=40), nullable=False),
        sa.Column("supported_operations", sa.JSON(), nullable=False),
        sa.Column("supports_image_generation", sa.Boolean(), nullable=False),
        sa.Column("supports_video_generation", sa.Boolean(), nullable=False),
        *[
            sa.Column(f"supports_{name}", sa.Boolean(), nullable=False)
            for name in (
                "t2v",
                "i2v",
                "v2v",
                "reference_image",
                "multi_reference",
                "start_frame",
                "end_frame",
                "start_end",
                "character_reference",
                "video_extension",
                "camera_instruction",
                "audio",
                "text_rendering",
            )
        ],
        sa.Column("max_reference_images", sa.Integer(), nullable=False),
        sa.Column("min_duration", sa.Float(), nullable=True),
        sa.Column("max_duration", sa.Float(), nullable=True),
        sa.Column("supported_aspect_ratios", sa.JSON(), nullable=False),
        sa.Column("supported_resolutions", sa.JSON(), nullable=False),
        sa.Column("physics_prior", sa.Float(), nullable=False),
        sa.Column("identity_prior", sa.Float(), nullable=False),
        sa.Column("camera_prior", sa.Float(), nullable=False),
        sa.Column("render_prior", sa.Float(), nullable=False),
        sa.Column("action_prior", sa.Float(), nullable=False),
        sa.Column("dialogue_prior", sa.Float(), nullable=False),
        sa.Column("text_render_prior", sa.Float(), nullable=False),
        sa.Column("provider_metadata", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("max_reference_images >= 0", name="ck_model_capability_max_references"),
        sa.CheckConstraint(
            "min_duration IS NULL OR min_duration > 0", name="ck_model_capability_min_duration"
        ),
        sa.CheckConstraint(
            "max_duration IS NULL OR max_duration > 0", name="ck_model_capability_max_duration"
        ),
        sa.CheckConstraint(
            "min_duration IS NULL OR max_duration IS NULL OR min_duration <= max_duration",
            name="ck_model_capability_duration_range",
        ),
        sa.CheckConstraint(
            "physics_prior >= 0 AND physics_prior <= 1 AND identity_prior >= 0 AND identity_prior <= 1 "
            "AND camera_prior >= 0 AND camera_prior <= 1 AND render_prior >= 0 AND render_prior <= 1 "
            "AND action_prior >= 0 AND action_prior <= 1 AND dialogue_prior >= 0 AND dialogue_prior <= 1 "
            "AND text_render_prior >= 0 AND text_render_prior <= 1",
            name="ck_model_capability_manual_priors",
        ),
        sa.ForeignKeyConstraint(["model_definition_id"], ["model_definitions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("model_definition_id"),
    )
    op.create_index(
        "ix_model_capability_profiles_source",
        "model_capability_profiles",
        ["source"],
        unique=False,
    )

    connection = op.get_bind()
    metadata = sa.MetaData()
    definitions = sa.Table("model_definitions", metadata, autoload_with=connection)
    profiles = sa.Table("model_capability_profiles", metadata, autoload_with=connection)
    now = datetime.now(UTC)
    values = [
        _migration_profile(definition, timestamp=now)
        for definition in connection.execute(sa.select(definitions)).mappings()
    ]
    if values:
        connection.execute(profiles.insert(), values)


def downgrade() -> None:
    _assert_downgrade_is_lossless()
    op.drop_index("ix_model_capability_profiles_source", table_name="model_capability_profiles")
    op.drop_table("model_capability_profiles")
