"""Production observations wide enough to learn from, and somewhere to save the answer.

`model_metrics` records a metric name and a value against a provider and a
model id. It cannot say which *snapshot* ran, what was asked of it, or under
what conditions — and each of those changes what an outcome means. A 40%
acceptance rate on text-to-video dialogue and a 90% rate on image-to-video
product shots average to a number that describes neither, and once averaged
there is no way back.

So this adds a second, wider record written alongside the existing one. Nothing
is migrated and nothing is dropped: `model_metrics` keeps its shape, keeps its
rows and keeps feeding the adaptive router exactly as it does today. Back-filling
the new table from the old one is impossible in the direction that matters —
the old rows do not carry a version, a task type or a scenario, and inventing
them is precisely the contamination the new table exists to prevent. It starts
empty on purpose.

Three tables:

    router_observations   one generation attempt, with its conditions and every
                          observed outcome; one row per generation job
    router_posteriors     saved cells from an offline posterior run
    router_replay_runs    the result of a historical replay, kept because the
                          conservative LCB flag is only allowed to be switched
                          on after one passes

The check constraints are the interesting part. `ck_router_obs_failed_has_no_quality`
refuses a row that failed generation and still carries a quality score or a
rating: nothing was produced, so there is nothing to judge, and allowing it
would let a provider outage read as a quality problem and teach the router to
avoid a good model permanently. `ck_router_posterior_ordered` refuses a cell
whose lower quantile is above its upper one, which is the shape a quantile bug
takes. It deliberately says nothing about the mean: a heavily skewed Beta can
have a mean outside its own central interval, and that is arithmetic rather
than a defect.

Revision ID: 0050_router_evidence
Revises: 0049_live_canary_status
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_router_evidence"
down_revision: str | None = "0049_live_canary_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "router_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model_id", sa.String(length=160), nullable=False),
        sa.Column("exact_version", sa.String(length=120), nullable=False),
        sa.Column("model_is_alias", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("task_type", sa.String(length=8), nullable=False),
        sa.Column("scenario", sa.String(length=40), nullable=False),
        sa.Column("asset_criticality", sa.String(length=40), nullable=False),
        sa.Column("prompt_complexity", sa.String(length=24), nullable=False),
        sa.Column("reference_mode", sa.String(length=32), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("resolution", sa.String(length=32), nullable=False, server_default="n/a"),
        sa.Column("aspect_ratio", sa.String(length=32), nullable=False, server_default="n/a"),
        sa.Column("generation_success", sa.Boolean(), nullable=False),
        sa.Column("provider_failure", sa.String(length=120), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("cost_credits", sa.Float(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("user_rating", sa.Integer(), nullable=True),
        sa.Column("user_preference_ab", sa.String(length=8), nullable=True),
        sa.Column("user_preference_opponent", sa.String(length=200), nullable=True),
        sa.Column("regenerated", sa.Boolean(), nullable=True),
        sa.Column("switched_model", sa.Boolean(), nullable=True),
        sa.Column("downloaded", sa.Boolean(), nullable=True),
        sa.Column("accepted_output", sa.Boolean(), nullable=True),
        sa.Column("used_in_next_shot", sa.Boolean(), nullable=True),
        sa.Column("qc_identity_score", sa.Float(), nullable=True),
        sa.Column("qc_motion_score", sa.Float(), nullable=True),
        sa.Column("qc_prompt_alignment", sa.Float(), nullable=True),
        sa.Column("qc_temporal_consistency", sa.Float(), nullable=True),
        sa.Column("router_version", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("router_decision_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("workspace_id", sa.String(length=64), nullable=True),
        sa.Column("generation_job_id", sa.String(length=36), nullable=True),
        sa.Column("shot_id", sa.String(length=36), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["generation_job_id"], ["generation_jobs.id"]),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_job_id", name="uq_router_observation_job"),
        sa.CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_router_obs_latency_nonneg"),
        sa.CheckConstraint(
            "user_rating IS NULL OR (user_rating >= 1 AND user_rating <= 5)",
            name="ck_router_obs_rating_range",
        ),
        sa.CheckConstraint(
            "qc_identity_score IS NULL OR (qc_identity_score >= 0 AND qc_identity_score <= 1)",
            name="ck_router_obs_qc_identity_range",
        ),
        sa.CheckConstraint(
            "qc_motion_score IS NULL OR (qc_motion_score >= 0 AND qc_motion_score <= 1)",
            name="ck_router_obs_qc_motion_range",
        ),
        sa.CheckConstraint(
            "qc_prompt_alignment IS NULL OR (qc_prompt_alignment >= 0 AND qc_prompt_alignment <= 1)",
            name="ck_router_obs_qc_prompt_range",
        ),
        sa.CheckConstraint(
            "qc_temporal_consistency IS NULL OR "
            "(qc_temporal_consistency >= 0 AND qc_temporal_consistency <= 1)",
            name="ck_router_obs_qc_temporal_range",
        ),
        sa.CheckConstraint(
            "generation_success = true OR (qc_identity_score IS NULL AND qc_motion_score IS NULL "
            "AND qc_prompt_alignment IS NULL AND qc_temporal_consistency IS NULL "
            "AND user_rating IS NULL AND accepted_output IS NULL)",
            name="ck_router_obs_failed_has_no_quality",
        ),
    )
    op.create_index(
        "ix_router_observations_key",
        "router_observations",
        ["provider", "model_id", "exact_version", "task_type", "scenario"],
    )
    op.create_index("ix_router_observations_occurred_at", "router_observations", ["occurred_at"])
    op.create_index(
        op.f("ix_router_observations_project_id"), "router_observations", ["project_id"]
    )
    op.create_index(
        op.f("ix_router_observations_workspace_id"), "router_observations", ["workspace_id"]
    )
    op.create_index(
        op.f("ix_router_observations_generation_job_id"), "router_observations", ["generation_job_id"]
    )
    op.create_index(op.f("ix_router_observations_shot_id"), "router_observations", ["shot_id"])

    op.create_table(
        "router_posteriors",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("engine_version", sa.String(length=60), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model_id", sa.String(length=160), nullable=False),
        sa.Column("exact_version", sa.String(length=120), nullable=False),
        sa.Column("task_type", sa.String(length=8), nullable=False),
        sa.Column("scenario", sa.String(length=40), nullable=False),
        sa.Column("metric_scale_id", sa.String(length=80), nullable=False),
        sa.Column("outcome_name", sa.String(length=48), nullable=False),
        sa.Column("level", sa.String(length=48), nullable=False),
        sa.Column("condition_token", sa.String(length=120), nullable=False, server_default="-"),
        sa.Column("posterior_mean", sa.Float(), nullable=False),
        sa.Column("posterior_lower_quantile", sa.Float(), nullable=False),
        sa.Column("posterior_upper_quantile", sa.Float(), nullable=False),
        sa.Column("lower_quantile_level", sa.Float(), nullable=False),
        sa.Column("upper_quantile_level", sa.Float(), nullable=False),
        sa.Column("effective_sample_size", sa.Float(), nullable=False),
        sa.Column("observation_count", sa.Integer(), nullable=False),
        sa.Column("alpha", sa.Float(), nullable=False),
        sa.Column("beta", sa.Float(), nullable=False),
        sa.Column("prior_alpha", sa.Float(), nullable=False),
        sa.Column("prior_beta", sa.Float(), nullable=False),
        sa.Column("prior_sources", sa.JSON(), nullable=False),
        sa.Column("prior_version", sa.String(length=80), nullable=False, server_default="none"),
        sa.Column("parent_level", sa.String(length=48), nullable=True),
        sa.Column("parent_mean", sa.Float(), nullable=True),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "provider",
            "model_id",
            "exact_version",
            "task_type",
            "scenario",
            "metric_scale_id",
            "outcome_name",
            "level",
            "condition_token",
            name="uq_router_posterior_cell",
        ),
        sa.CheckConstraint(
            "posterior_lower_quantile <= posterior_upper_quantile",
            name="ck_router_posterior_ordered",
        ),
        sa.CheckConstraint("observation_count >= 0", name="ck_router_posterior_count_nonneg"),
        sa.CheckConstraint("effective_sample_size >= 0", name="ck_router_posterior_ess_nonneg"),
    )
    op.create_index(op.f("ix_router_posteriors_run_id"), "router_posteriors", ["run_id"])
    op.create_index(
        "ix_router_posteriors_lookup",
        "router_posteriors",
        ["provider", "model_id", "exact_version", "outcome_name"],
    )

    op.create_table(
        "router_replay_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("harness_version", sa.String(length=60), nullable=False),
        sa.Column("outcome_name", sa.String(length=48), nullable=False),
        sa.Column("posterior_run_id", sa.String(length=64), nullable=True),
        sa.Column("fit_observations", sa.Integer(), nullable=False),
        sa.Column("eval_observations", sa.Integer(), nullable=False),
        sa.Column("contexts", sa.Integer(), nullable=False),
        sa.Column("unscored_contexts", sa.Integer(), nullable=False),
        sa.Column("baseline_json", sa.JSON(), nullable=False),
        sa.Column("posterior_json", sa.JSON(), nullable=False),
        sa.Column("coverage_json", sa.JSON(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "outcome_name", name="uq_router_replay_run_outcome"),
        sa.CheckConstraint("fit_observations >= 0", name="ck_router_replay_fit_nonneg"),
        sa.CheckConstraint("eval_observations >= 0", name="ck_router_replay_eval_nonneg"),
    )
    op.create_index(op.f("ix_router_replay_runs_run_id"), "router_replay_runs", ["run_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_router_replay_runs_run_id"), table_name="router_replay_runs")
    op.drop_table("router_replay_runs")
    op.drop_index("ix_router_posteriors_lookup", table_name="router_posteriors")
    op.drop_index(op.f("ix_router_posteriors_run_id"), table_name="router_posteriors")
    op.drop_table("router_posteriors")
    op.drop_index(op.f("ix_router_observations_shot_id"), table_name="router_observations")
    op.drop_index(
        op.f("ix_router_observations_generation_job_id"), table_name="router_observations"
    )
    op.drop_index(op.f("ix_router_observations_workspace_id"), table_name="router_observations")
    op.drop_index(op.f("ix_router_observations_project_id"), table_name="router_observations")
    op.drop_index("ix_router_observations_occurred_at", table_name="router_observations")
    op.drop_index("ix_router_observations_key", table_name="router_observations")
    op.drop_table("router_observations")
