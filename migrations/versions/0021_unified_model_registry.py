"""Persist unified model definitions and configurable role bindings.

Revision ID: 0021_unified_model_registry
Revises: 0020_provider_media_upload_claim
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_unified_model_registry"
down_revision: str | None = "0020_provider_media_upload_claim"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_definitions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("logical_name", sa.String(length=160), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("provider_model_id", sa.String(length=255), nullable=False),
        sa.Column("modality", sa.String(length=50), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("quality_tier", sa.String(length=40), nullable=False),
        sa.Column("cost_class", sa.String(length=40), nullable=False),
        sa.Column("provider_trust_level", sa.String(length=40), nullable=False),
        sa.Column("criticality_allowed", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("live_enabled", sa.Boolean(), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("max_duration", sa.Float(), nullable=True),
        sa.Column("supported_aspect_ratios", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("logical_name", name="uq_model_definitions_logical_name"),
        sa.UniqueConstraint(
            "provider",
            "provider_model_id",
            "modality",
            name="uq_model_definitions_provider_model_modality",
        ),
    )
    op.create_index(
        "ix_model_definitions_provider_enabled",
        "model_definitions",
        ["provider", "enabled"],
        unique=False,
    )

    op.create_table(
        "model_role_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=80), nullable=False),
        sa.Column("plan_tier", sa.String(length=40), nullable=False),
        sa.Column("model_definition_id", sa.String(length=36), nullable=False),
        sa.Column("binding_kind", sa.String(length=40), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["model_definition_id"],
            ["model_definitions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "role",
            "plan_tier",
            "model_definition_id",
            name="uq_model_role_binding_scope_model",
        ),
    )
    op.create_index(
        "ix_model_role_binding_lookup",
        "model_role_bindings",
        ["role", "plan_tier", "enabled", "priority"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_model_role_binding_lookup", table_name="model_role_bindings")
    op.drop_table("model_role_bindings")
    op.drop_index("ix_model_definitions_provider_enabled", table_name="model_definitions")
    op.drop_table("model_definitions")
