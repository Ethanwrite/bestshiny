"""Add runtime feature flags, multimodal memory, evaluation, metrics, benchmarks and traces.

Revision ID: 0005_visual_runtime
Revises: 0004_unified_asset_registry
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_visual_runtime"
down_revision: str | None = "0004_unified_asset_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "feature_flags" not in _tables():
        op.create_table(
            "feature_flags",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("project_id", sa.String(36), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("name", "project_id", name="uq_feature_flag_scope"),
        )
        op.create_index("ix_feature_flags_name", "feature_flags", ["name"])
        op.create_index("ix_feature_flags_project_id", "feature_flags", ["project_id"])

    if "shot_memories" not in _tables():
        op.create_table(
            "shot_memories",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), nullable=False),
            sa.Column("layer", sa.String(8), nullable=False),
            sa.Column("memory_type", sa.String(80), nullable=False),
            sa.Column("text_content", sa.Text(), nullable=False),
            sa.Column("image_urls", sa.JSON(), nullable=False),
            sa.Column("video_urls", sa.JSON(), nullable=False),
            sa.Column("entity_ids", sa.JSON(), nullable=False),
            sa.Column("scene_id", sa.String(36), nullable=True),
            sa.Column("shot_id", sa.String(36), nullable=True),
            sa.Column("asset_version_ids", sa.JSON(), nullable=False),
            sa.Column("temporal_position", sa.Float(), nullable=True),
            sa.Column("canonical", sa.Boolean(), nullable=False),
            sa.Column("embedding", sa.JSON(), nullable=False),
            sa.Column("embedding_dimension", sa.Integer(), nullable=False),
            sa.Column("embedding_provider", sa.String(80), nullable=False),
            sa.Column("embedding_model", sa.String(120), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"]),
            sa.ForeignKeyConstraint(["shot_id"], ["shots.id"]),
        )
        for column in ("project_id", "layer", "memory_type", "scene_id", "shot_id", "canonical"):
            op.create_index(f"ix_shot_memories_{column}", "shot_memories", [column])
        op.create_index(
            "ix_shot_memories_scope",
            "shot_memories",
            ["project_id", "layer", "scene_id", "memory_type"],
        )

    if "evaluation_results" not in _tables():
        op.create_table(
            "evaluation_results",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), nullable=False),
            sa.Column("shot_id", sa.String(36), nullable=True),
            sa.Column("generation_job_id", sa.String(36), nullable=True),
            sa.Column("generated_asset_id", sa.String(36), nullable=True),
            sa.Column("decision", sa.String(40), nullable=False),
            sa.Column("overall_score", sa.Float(), nullable=False),
            sa.Column("critical_failure", sa.Boolean(), nullable=False),
            sa.Column("scores_json", sa.JSON(), nullable=False),
            sa.Column("checks_json", sa.JSON(), nullable=False),
            sa.Column("retry_reasons", sa.JSON(), nullable=False),
            sa.Column("retry_patch", sa.Text(), nullable=False),
            sa.Column("evidence_complete", sa.Boolean(), nullable=False),
            sa.Column("evaluator_version", sa.String(80), nullable=False),
            sa.Column("judge_provider", sa.String(80), nullable=False),
            sa.Column("judge_model", sa.String(120), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(80), nullable=False),
            sa.Column("model_id", sa.String(120), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["shot_id"], ["shots.id"]),
            sa.ForeignKeyConstraint(["generation_job_id"], ["generation_jobs.id"]),
            sa.ForeignKeyConstraint(["generated_asset_id"], ["media_assets.id"]),
        )
        for column in ("project_id", "shot_id", "generation_job_id", "decision", "provider", "model_id"):
            op.create_index(f"ix_evaluation_results_{column}", "evaluation_results", [column])

    if "model_metrics" not in _tables():
        op.create_table(
            "model_metrics",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), nullable=True),
            sa.Column("shot_id", sa.String(36), nullable=True),
            sa.Column("generation_job_id", sa.String(36), nullable=True),
            sa.Column("provider", sa.String(80), nullable=False),
            sa.Column("model_id", sa.String(120), nullable=False),
            sa.Column("model_version", sa.String(80), nullable=False),
            sa.Column("metric_name", sa.String(80), nullable=False),
            sa.Column("value", sa.Float(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
            sa.ForeignKeyConstraint(["shot_id"], ["shots.id"]),
            sa.ForeignKeyConstraint(["generation_job_id"], ["generation_jobs.id"]),
        )
        for column in ("project_id", "shot_id", "generation_job_id", "provider", "model_id", "metric_name"):
            op.create_index(f"ix_model_metrics_{column}", "model_metrics", [column])
        op.create_index(
            "ix_model_metrics_model_name",
            "model_metrics",
            ["provider", "model_id", "metric_name"],
        )

    if "model_benchmark_results" not in _tables():
        op.create_table(
            "model_benchmark_results",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("suite_version", sa.String(80), nullable=False),
            sa.Column("case_key", sa.String(100), nullable=False),
            sa.Column("provider", sa.String(80), nullable=False),
            sa.Column("model_id", sa.String(120), nullable=False),
            sa.Column("model_version", sa.String(80), nullable=False),
            sa.Column("passed", sa.Boolean(), nullable=False),
            sa.Column("scores_json", sa.JSON(), nullable=False),
            sa.Column("evidence_asset_ids", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in ("suite_version", "case_key", "provider", "model_id"):
            op.create_index(f"ix_model_benchmark_results_{column}", "model_benchmark_results", [column])
        op.create_index(
            "ix_benchmark_model_case",
            "model_benchmark_results",
            ["provider", "model_id", "case_key", "suite_version"],
        )

    if "production_traces" not in _tables():
        op.create_table(
            "production_traces",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("trace_id", sa.String(64), nullable=False, unique=True),
            sa.Column("mode", sa.String(40), nullable=False),
            sa.Column("project_id", sa.String(36), nullable=False),
            sa.Column("shot_id", sa.String(36), nullable=True),
            sa.Column("generation_job_id", sa.String(36), nullable=True),
            sa.Column("provider", sa.String(80), nullable=False),
            sa.Column("model_id", sa.String(120), nullable=False),
            sa.Column("prompt_version", sa.String(80), nullable=False),
            sa.Column("context_asset_ids", sa.JSON(), nullable=False),
            sa.Column("retrieved_memory_ids", sa.JSON(), nullable=False),
            sa.Column("router_scores_json", sa.JSON(), nullable=False),
            sa.Column("generation_latency", sa.Float(), nullable=True),
            sa.Column("estimated_cost", sa.Float(), nullable=False),
            sa.Column("actual_cost", sa.Float(), nullable=False),
            sa.Column("evaluation_json", sa.JSON(), nullable=False),
            sa.Column("retry_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["shot_id"], ["shots.id"]),
            sa.ForeignKeyConstraint(["generation_job_id"], ["generation_jobs.id"]),
        )
        for column in (
            "trace_id",
            "mode",
            "project_id",
            "shot_id",
            "generation_job_id",
            "provider",
            "model_id",
        ):
            op.create_index(f"ix_production_traces_{column}", "production_traces", [column])


def downgrade() -> None:
    for table in (
        "production_traces",
        "model_benchmark_results",
        "model_metrics",
        "evaluation_results",
        "shot_memories",
        "feature_flags",
    ):
        if table in _tables():
            op.drop_table(table)
