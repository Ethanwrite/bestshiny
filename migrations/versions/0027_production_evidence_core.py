"""Add Phase III production evidence, canary, auth, timeline, and quota state.

Revision ID: 0027_production_evidence_core
Revises: 0026_model_capability_registry
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_production_evidence_core"
down_revision: str | None = "0026_model_capability_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FIVE_GIB = 5 * 1024 * 1024 * 1024
CORE_TABLES = {
    "workspaces",
    "projects",
    "users",
    "media_assets",
    "shots",
    "provider_credentials",
    "generation_jobs",
    "generation_candidates",
    "qa_results",
    "cost_records",
    "production_traces",
    "model_definitions",
}
CORE_ANCHORS = {"workspaces", "projects", "generation_jobs"}
PHASE3_TABLES = (
    "auth_login_throttles",
    "password_reset_tokens",
    "storage_reservations",
    "runapi_benchmarks",
    "live_canary_usages",
    "live_canary_permits",
    "timeline_transitions",
    "decision_outcome_records",
    "provider_billing_evidence",
    "embedding_evidence",
    "model_execution_records",
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _scalar(sql: str) -> object | None:
    return op.get_bind().execute(sa.text(sql)).scalar()


def _skip_assetless_or_require_complete_core() -> bool:
    tables = _tables()
    if not CORE_ANCHORS.intersection(tables):
        # A few historical integrity tests intentionally carry only the
        # tables owned by the revision under test plus an Alembic stamp.  They
        # are not deployable platform databases, so preserve the established
        # no-op recovery path without fabricating the rest of the product
        # schema around them.
        return True
    missing = CORE_TABLES.difference(tables)
    if missing:
        raise RuntimeError(f"Production evidence migration requires missing tables: {sorted(missing)}")
    return False


def upgrade() -> None:
    if _skip_assetless_or_require_complete_core():
        return
    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.add_column(
            sa.Column(
                "max_storage_bytes",
                sa.BigInteger(),
                server_default=str(FIVE_GIB),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("used_storage_bytes", sa.BigInteger(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("reserved_storage_bytes", sa.BigInteger(), server_default="0", nullable=False)
        )
        batch_op.create_check_constraint("ck_workspace_max_storage_positive", "max_storage_bytes > 0")
        batch_op.create_check_constraint("ck_workspace_storage_used_nonnegative", "used_storage_bytes >= 0")
        batch_op.create_check_constraint(
            "ck_workspace_storage_reserved_nonnegative", "reserved_storage_bytes >= 0"
        )
        batch_op.create_check_constraint(
            "ck_workspace_storage_capacity",
            "used_storage_bytes + reserved_storage_bytes <= max_storage_bytes",
        )

    with op.batch_alter_table("media_assets") as batch_op:
        batch_op.add_column(sa.Column("size_bytes", sa.BigInteger(), server_default="0", nullable=False))

    with op.batch_alter_table("shots") as batch_op:
        batch_op.add_column(
            sa.Column("downstream_state_stale", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.add_column(sa.Column("stale_reason", sa.String(length=240), nullable=True))
        batch_op.add_column(sa.Column("stale_from_shot_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_shots_stale_from_shot_id",
            "shots",
            ["stale_from_shot_id"],
            ["id"],
        )
        batch_op.create_index("ix_shots_stale_from_shot_id", ["stale_from_shot_id"], unique=False)

    with op.batch_alter_table("provider_credentials") as batch_op:
        batch_op.add_column(
            sa.Column("status", sa.String(length=40), server_default="ACTIVE", nullable=False)
        )
        batch_op.add_column(sa.Column("status_reason", sa.String(length=240), nullable=True))
        batch_op.add_column(sa.Column("redacted_fingerprint", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_provider_credentials_status", ["status"], unique=False)

    for table_name in ("generation_jobs", "cost_records", "production_traces"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                "actual_cost",
                existing_type=sa.Float(),
                nullable=True,
                existing_nullable=False,
                server_default=None,
            )

    _create_model_execution_records()
    _create_embedding_evidence()
    _create_provider_billing_evidence()
    _create_decision_outcomes()
    _create_timeline_transitions()
    _create_live_canary_tables()
    _create_runapi_benchmarks()
    _create_storage_reservations()
    _create_auth_tables()

    # Defaults are needed only for populated-table backfill.  Runtime inserts
    # now use explicit application defaults, keeping ORM/schema parity strict.
    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.alter_column("max_storage_bytes", server_default=None)
        batch_op.alter_column("used_storage_bytes", server_default=None)
        batch_op.alter_column("reserved_storage_bytes", server_default=None)
    with op.batch_alter_table("media_assets") as batch_op:
        batch_op.alter_column("size_bytes", server_default=None)
    with op.batch_alter_table("shots") as batch_op:
        batch_op.alter_column("downstream_state_stale", server_default=None)
    with op.batch_alter_table("provider_credentials") as batch_op:
        batch_op.alter_column("status", server_default=None)


def _create_model_execution_records() -> None:
    op.create_table(
        "model_execution_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=80), nullable=False),
        sa.Column("model_definition_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("provider_model_id", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("token_usage_json", sa.JSON(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("actual_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("cost_source", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("latency_ms >= 0", name="ck_model_execution_latency_nonnegative"),
        sa.CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="ck_model_execution_estimated_cost_nonnegative",
        ),
        sa.CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_model_execution_actual_cost_nonnegative",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_definition_id"], ["model_definitions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    for name, columns in (
        ("ix_model_execution_records_project_id", ["project_id"]),
        ("ix_model_execution_records_role", ["role"]),
        ("ix_model_execution_records_model_definition_id", ["model_definition_id"]),
        ("ix_model_execution_records_provider", ["provider"]),
        ("ix_model_execution_records_request_hash", ["request_hash"]),
        ("ix_model_execution_records_status", ["status"]),
        ("ix_model_execution_records_created_at", ["created_at"]),
        ("ix_model_execution_project_role_created", ["project_id", "role", "created_at"]),
    ):
        op.create_index(name, "model_execution_records", columns)


def _create_embedding_evidence() -> None:
    op.create_table(
        "embedding_evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=True),
        sa.Column("model_definition_id", sa.String(length=36), nullable=False),
        sa.Column("model_execution_record_id", sa.String(length=36), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("embedding_hash", sa.String(length=64), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("embedding_dimension > 0", name="ck_embedding_evidence_dimension_positive"),
        sa.CheckConstraint("latency_ms >= 0", name="ck_embedding_evidence_latency_nonnegative"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["media_assets.id"]),
        sa.ForeignKeyConstraint(["model_definition_id"], ["model_definitions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["model_execution_record_id"],
            ["model_execution_records.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "model_execution_record_id",
            "asset_id",
            "input_hash",
            name="uq_embedding_evidence_execution_input",
        ),
    )
    for column in (
        "project_id",
        "asset_id",
        "model_definition_id",
        "model_execution_record_id",
        "created_at",
    ):
        op.create_index(f"ix_embedding_evidence_{column}", "embedding_evidence", [column])


def _create_provider_billing_evidence() -> None:
    op.create_table(
        "provider_billing_evidence",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("generation_job_id", sa.String(length=36), nullable=False),
        sa.Column("cost_record_id", sa.String(length=36), nullable=True),
        sa.Column("evidence_key", sa.String(length=160), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("provider_reference", sa.String(length=500), nullable=True),
        sa.Column("actual_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("provider_credits", sa.Numeric(18, 6), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_billing_evidence_actual_nonnegative",
        ),
        sa.CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="ck_billing_evidence_estimated_nonnegative",
        ),
        sa.CheckConstraint(
            "provider_credits IS NULL OR provider_credits >= 0",
            name="ck_billing_evidence_credits_nonnegative",
        ),
        sa.ForeignKeyConstraint(["generation_job_id"], ["generation_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cost_record_id"], ["cost_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_job_id", "evidence_key", name="uq_billing_evidence_job_key"),
    )
    for column in ("generation_job_id", "cost_record_id", "provider", "source"):
        op.create_index(
            f"ix_provider_billing_evidence_{column}",
            "provider_billing_evidence",
            [column],
        )
    op.create_index(
        "ix_billing_evidence_provider_model",
        "provider_billing_evidence",
        ["provider", "model", "created_at"],
    )


def _create_decision_outcomes() -> None:
    op.create_table(
        "decision_outcome_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("shot_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("generation_job_id", sa.String(length=36), nullable=True),
        sa.Column("qa_result_id", sa.String(length=36), nullable=True),
        sa.Column("continuity_decision", sa.String(length=80), nullable=False),
        sa.Column("generation_policy", sa.String(length=80), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("shot_features_json", sa.JSON(), nullable=False),
        sa.Column("qa_result_json", sa.JSON(), nullable=False),
        sa.Column("user_outcome", sa.String(length=40), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("actual_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("billing_source", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["generation_candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["generation_job_id"], ["generation_jobs.id"]),
        sa.ForeignKeyConstraint(["qa_result_id"], ["qa_results.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", name="uq_decision_outcome_candidate"),
    )
    for column in (
        "project_id",
        "shot_id",
        "generation_job_id",
        "qa_result_id",
        "provider",
        "user_outcome",
        "created_at",
    ):
        op.create_index(f"ix_decision_outcome_records_{column}", "decision_outcome_records", [column])
    op.create_index(
        "ix_decision_outcome_provider_model",
        "decision_outcome_records",
        ["provider", "model", "created_at"],
    )


def _create_timeline_transitions() -> None:
    op.create_table(
        "timeline_transitions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_shot_id", sa.String(length=36), nullable=True),
        sa.Column("target_shot_id", sa.String(length=36), nullable=False),
        sa.Column("transition_type", sa.String(length=40), nullable=False),
        sa.Column("branch_key", sa.String(length=120), nullable=True),
        sa.Column("reconciliation_required", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_shot_id"], ["shots.id"]),
        sa.ForeignKeyConstraint(["target_shot_id"], ["shots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("target_shot_id", name="uq_timeline_transition_target_shot"),
    )
    for column in ("project_id", "source_shot_id", "target_shot_id"):
        op.create_index(f"ix_timeline_transitions_{column}", "timeline_transitions", [column])
    op.create_index(
        "ix_timeline_transition_project_type",
        "timeline_transitions",
        ["project_id", "transition_type"],
    )


def _create_live_canary_tables() -> None:
    op.create_table(
        "live_canary_permits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("max_requests", sa.Integer(), nullable=False),
        sa.Column("max_cost_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("used_requests", sa.Integer(), nullable=False),
        sa.Column("reserved_cost_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("actual_cost_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purpose", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("max_requests > 0", name="ck_live_canary_max_requests_positive"),
        sa.CheckConstraint("max_cost_usd > 0", name="ck_live_canary_max_cost_positive"),
        sa.CheckConstraint("used_requests >= 0", name="ck_live_canary_used_requests_nonnegative"),
        sa.CheckConstraint("reserved_cost_usd >= 0", name="ck_live_canary_reserved_cost_nonnegative"),
        sa.CheckConstraint("actual_cost_usd >= 0", name="ck_live_canary_actual_cost_nonnegative"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_live_canary_lookup",
        "live_canary_permits",
        ["provider", "model", "status", "expires_at"],
    )
    op.create_index("ix_live_canary_permits_status", "live_canary_permits", ["status"])
    op.create_table(
        "live_canary_usages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("permit_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("actual_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("evidence_reference", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("estimated_cost_usd >= 0", name="ck_live_canary_usage_estimated_nonnegative"),
        sa.CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_live_canary_usage_actual_nonnegative",
        ),
        sa.ForeignKeyConstraint(["permit_id"], ["live_canary_permits.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("permit_id", "idempotency_key", name="uq_live_canary_usage_key"),
    )
    op.create_index("ix_live_canary_usages_permit_id", "live_canary_usages", ["permit_id"])
    op.create_index("ix_live_canary_usages_status", "live_canary_usages", ["status"])


def _create_runapi_benchmarks() -> None:
    op.create_table(
        "runapi_benchmarks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=200), nullable=False),
        sa.Column("task_type", sa.String(length=100), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("output_quality", sa.Float(), nullable=True),
        sa.Column("fact_lock_pass", sa.Boolean(), nullable=False),
        sa.Column("fallback_required", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("actual_cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("user_acceptance", sa.Boolean(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", name="uq_runapi_benchmark_task"),
    )
    op.create_index("ix_runapi_benchmarks_task_type", "runapi_benchmarks", ["task_type"])


def _create_storage_reservations() -> None:
    op.create_table(
        "storage_reservations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("asset_id", sa.String(length=36), nullable=True),
        sa.Column("storage_key", sa.String(length=1000), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reserved_bytes > 0", name="ck_storage_reservation_bytes_positive"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["media_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "idempotency_key", name="uq_storage_reservation_key"),
    )
    for column in ("workspace_id", "project_id", "asset_id"):
        op.create_index(f"ix_storage_reservations_{column}", "storage_reservations", [column])
    op.create_index("ix_storage_reservation_status", "storage_reservations", ["workspace_id", "status"])


def _create_auth_tables() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_token_hash"),
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])
    op.create_index("ix_password_reset_tokens_expires_at", "password_reset_tokens", ["expires_at"])
    op.create_index("ix_password_reset_tokens_created_at", "password_reset_tokens", ["created_at"])
    op.create_table(
        "auth_login_throttles",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_login_throttles_blocked_until", "auth_login_throttles", ["blocked_until"])


def downgrade() -> None:
    if _skip_assetless_or_require_complete_core():
        return
    for table in PHASE3_TABLES:
        if _scalar(f'SELECT 1 FROM "{table}" LIMIT 1') is not None:
            raise RuntimeError("Production evidence downgrade would discard Phase III records from " + table)
    if (
        _scalar(
            "SELECT id FROM workspaces WHERE max_storage_bytes != "
            f"{FIVE_GIB} OR used_storage_bytes != 0 OR reserved_storage_bytes != 0 LIMIT 1"
        )
        is not None
    ):
        raise RuntimeError("Production evidence downgrade would discard workspace storage state")
    if _scalar("SELECT id FROM media_assets WHERE size_bytes != 0 LIMIT 1") is not None:
        raise RuntimeError("Production evidence downgrade would discard media asset size evidence")
    if (
        _scalar(
            "SELECT id FROM shots WHERE downstream_state_stale IS TRUE "
            "OR stale_reason IS NOT NULL OR stale_from_shot_id IS NOT NULL LIMIT 1"
        )
        is not None
    ):
        raise RuntimeError("Production evidence downgrade would discard downstream stale state")
    if (
        _scalar(
            "SELECT id FROM provider_credentials WHERE status != 'ACTIVE' "
            "OR status_reason IS NOT NULL OR redacted_fingerprint IS NOT NULL "
            "OR last_validated_at IS NOT NULL LIMIT 1"
        )
        is not None
    ):
        raise RuntimeError("Production evidence downgrade would discard credential status evidence")
    for table in ("generation_jobs", "cost_records", "production_traces"):
        if _scalar(f'SELECT id FROM "{table}" WHERE actual_cost IS NULL LIMIT 1') is not None:
            raise RuntimeError(
                "Production evidence downgrade cannot represent unknown actual cost in " + table
            )

    for table in PHASE3_TABLES:
        op.drop_table(table)

    for table_name in ("production_traces", "cost_records", "generation_jobs"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(
                "actual_cost",
                existing_type=sa.Float(),
                nullable=False,
                existing_nullable=True,
                server_default="0" if table_name == "generation_jobs" else None,
            )

    with op.batch_alter_table("provider_credentials") as batch_op:
        batch_op.drop_index("ix_provider_credentials_status")
        batch_op.drop_column("last_validated_at")
        batch_op.drop_column("redacted_fingerprint")
        batch_op.drop_column("status_reason")
        batch_op.drop_column("status")
    with op.batch_alter_table("shots") as batch_op:
        batch_op.drop_index("ix_shots_stale_from_shot_id")
        batch_op.drop_constraint("fk_shots_stale_from_shot_id", type_="foreignkey")
        batch_op.drop_column("stale_from_shot_id")
        batch_op.drop_column("stale_reason")
        batch_op.drop_column("downstream_state_stale")
    with op.batch_alter_table("media_assets") as batch_op:
        batch_op.drop_column("size_bytes")
    with op.batch_alter_table("workspaces") as batch_op:
        batch_op.drop_constraint("ck_workspace_storage_capacity", type_="check")
        batch_op.drop_constraint("ck_workspace_storage_reserved_nonnegative", type_="check")
        batch_op.drop_constraint("ck_workspace_storage_used_nonnegative", type_="check")
        batch_op.drop_constraint("ck_workspace_max_storage_positive", type_="check")
        batch_op.drop_column("reserved_storage_bytes")
        batch_op.drop_column("used_storage_bytes")
        batch_op.drop_column("max_storage_bytes")
