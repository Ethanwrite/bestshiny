"""Add automatic Google Flow project affinity and strong poll identity.

Revision ID: 0025_flow_project_affinity
Revises: 0024_workspace_credit_lifecycle
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_flow_project_affinity"
down_revision: str | None = "0024_workspace_credit_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_BINDING_STATUSES = (
    "PROVISIONING",
    "READY",
    "DEGRADED",
    "MIGRATION_REQUIRED",
    "MIGRATING",
)
ALL_BINDING_STATUSES = (*ACTIVE_BINDING_STATUSES, "DISABLED", "FAILED")
ACTIVE_SQL = "('PROVISIONING', 'READY', 'DEGRADED', 'MIGRATION_REQUIRED', 'MIGRATING')"
FLOW_REMOTE_OWNER_SQL = "provider = 'google_flow' AND provider_project_id IS NOT NULL"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _scalar(sql: str) -> object | None:
    return op.get_bind().execute(sa.text(sql)).scalar()


def _json_object(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _preflight_bindings() -> None:
    unsupported = _scalar(
        "SELECT status FROM provider_projects WHERE status NOT IN "
        "('PROVISIONING', 'READY', 'DEGRADED', 'MIGRATION_REQUIRED', "
        "'MIGRATING', 'DISABLED', 'FAILED') LIMIT 1"
    )
    if unsupported is not None:
        raise RuntimeError(f"Flow affinity found an unsupported provider project status: {unsupported}")
    duplicate_local = _scalar(
        "SELECT local_project_id FROM provider_projects "
        "WHERE provider = 'google_flow' AND status IN "
        f"{ACTIVE_SQL} GROUP BY local_project_id, provider HAVING COUNT(*) > 1 LIMIT 1"
    )
    if duplicate_local is not None:
        raise RuntimeError(
            "Flow affinity requires one active binding per local project; reconcile project "
            f"{duplicate_local} before upgrading"
        )
    duplicate_remote = _scalar(
        "SELECT provider_project_id FROM provider_projects "
        "WHERE provider = 'google_flow' AND provider_project_id IS NOT NULL "
        "GROUP BY provider, provider_project_id HAVING COUNT(*) > 1 LIMIT 1"
    )
    if duplicate_remote is not None:
        raise RuntimeError(
            "Flow affinity requires one permanent ownership row per remote project across all statuses; "
            "reconcile remote project "
            f"{duplicate_remote} before upgrading"
        )


def _backfill_flow_poll_projects() -> None:
    metadata = sa.MetaData()
    metadata.reflect(
        op.get_bind(),
        only=["generation_jobs", "provider_accounts", "provider_projects"],
    )
    jobs = metadata.tables["generation_jobs"]
    accounts = metadata.tables["provider_accounts"]
    bindings = metadata.tables["provider_projects"]
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            jobs.c.id,
            jobs.c.project_id,
            jobs.c.status,
            jobs.c.account_id,
            jobs.c.provider_job_id,
            jobs.c.provider_request_json,
        ).where(jobs.c.provider == "google_flow")
    ).mappings()
    for row in rows:
        request = _json_object(row["provider_request_json"])
        provider_project_id = str(request.get("_provider_project_id") or "").strip()
        if not provider_project_id and row["account_id"]:
            binding_project = connection.scalar(
                sa.select(bindings.c.provider_project_id).where(
                    bindings.c.local_project_id == row["project_id"],
                    bindings.c.provider == "google_flow",
                    bindings.c.provider_account_id == row["account_id"],
                    bindings.c.status == "READY",
                )
            )
            provider_project_id = str(binding_project or "").strip()
        if not provider_project_id and row["account_id"]:
            metadata_json = connection.scalar(
                sa.select(accounts.c.metadata_json).where(accounts.c.id == row["account_id"])
            )
            provider_project_id = str(_json_object(metadata_json).get("project_id") or "").strip()
        if (
            row["provider_job_id"]
            and row["status"] not in {"COMPLETED", "CANCELLED", "FAILED"}
            and not provider_project_id
        ):
            raise RuntimeError(
                "Flow poll identity cannot be backfilled for generation job "
                f"{row['id']}; persist or reconcile its remote project before upgrading"
            )
        if provider_project_id:
            connection.execute(
                jobs.update().where(jobs.c.id == row["id"]).values(provider_project_id=provider_project_id)
            )


def upgrade() -> None:
    tables = _tables()
    required = {"provider_projects", "provider_accounts", "projects", "generation_jobs"}
    missing = required.difference(tables)
    if missing == required:
        # Historical assetless recovery snapshots intentionally carry only an
        # Alembic stamp. Preserve the established no-op upgrade path while
        # still rejecting partially present production schemas below.
        return
    if missing:
        raise RuntimeError(f"Flow affinity requires missing tables: {sorted(missing)}")
    _preflight_bindings()

    with op.batch_alter_table("provider_projects") as batch_op:
        batch_op.drop_constraint("uq_provider_project", type_="unique")
        batch_op.alter_column(
            "provider_project_id",
            existing_type=sa.String(length=500),
            nullable=True,
        )
        batch_op.add_column(sa.Column("version", sa.Integer(), server_default="1", nullable=False))
        batch_op.add_column(sa.Column("status_reason", sa.String(length=240), nullable=True))
        batch_op.add_column(sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("migration_required_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("provisioning_token", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("provisioning_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_check_constraint(
            "ck_provider_project_status",
            "status IN ('PROVISIONING', 'READY', 'DEGRADED', 'MIGRATION_REQUIRED', "
            "'MIGRATING', 'DISABLED', 'FAILED')",
        )
        batch_op.create_check_constraint("ck_provider_project_version", "version > 0")
        batch_op.create_check_constraint(
            "ck_provider_project_remote_id",
            "status IN ('PROVISIONING', 'MIGRATION_REQUIRED', 'FAILED') OR provider_project_id IS NOT NULL",
        )
        batch_op.create_index("ix_provider_projects_provisioning_token", ["provisioning_token"], unique=False)
        batch_op.create_index(
            "ix_provider_projects_provisioning_expires_at",
            ["provisioning_expires_at"],
            unique=False,
        )
    op.execute(sa.text("UPDATE provider_projects SET ready_at = updated_at WHERE status = 'READY'"))
    op.create_index(
        "uq_flow_active_local_project",
        "provider_projects",
        ["local_project_id", "provider"],
        unique=True,
        sqlite_where=sa.text(f"provider = 'google_flow' AND status IN {ACTIVE_SQL}"),
        postgresql_where=sa.text(f"provider = 'google_flow' AND status IN {ACTIVE_SQL}"),
    )
    op.create_index(
        "uq_non_flow_provider_project_account",
        "provider_projects",
        ["local_project_id", "provider", "provider_account_id"],
        unique=True,
        sqlite_where=sa.text("provider != 'google_flow'"),
        postgresql_where=sa.text("provider != 'google_flow'"),
    )
    op.create_index(
        "uq_flow_remote_project_owner",
        "provider_projects",
        ["provider", "provider_project_id"],
        unique=True,
        sqlite_where=sa.text(FLOW_REMOTE_OWNER_SQL),
        postgresql_where=sa.text(FLOW_REMOTE_OWNER_SQL),
    )

    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.add_column(sa.Column("provider_project_id", sa.String(length=500), nullable=True))
        batch_op.create_index("ix_generation_jobs_provider_project_id", ["provider_project_id"], unique=False)
    _backfill_flow_poll_projects()
    duplicate_poll = _scalar(
        "SELECT provider_job_id FROM generation_jobs "
        "WHERE provider = 'google_flow' AND provider_job_id IS NOT NULL "
        "GROUP BY provider, account_id, provider_project_id, provider_job_id "
        "HAVING COUNT(*) > 1 LIMIT 1"
    )
    if duplicate_poll is not None:
        raise RuntimeError(
            "Flow poll identity is already claimed by multiple local jobs; reconcile remote job "
            f"{duplicate_poll} before upgrading"
        )
    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.create_check_constraint(
            "ck_generation_flow_poll_identity",
            "provider != 'google_flow' OR provider_job_id IS NULL "
            "OR status IN ('COMPLETED', 'CANCELLED', 'FAILED') "
            "OR (account_id IS NOT NULL AND provider_project_id IS NOT NULL)",
        )
    op.create_index(
        "uq_generation_flow_poll_identity",
        "generation_jobs",
        ["provider", "account_id", "provider_project_id", "provider_job_id"],
        unique=True,
        sqlite_where=sa.text("provider = 'google_flow' AND provider_job_id IS NOT NULL"),
        postgresql_where=sa.text("provider = 'google_flow' AND provider_job_id IS NOT NULL"),
    )

    op.create_table(
        "flow_migration_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_binding_id", sa.String(length=36), nullable=False),
        sa.Column("local_project_id", sa.String(length=36), nullable=False),
        sa.Column("source_account_id", sa.String(length=36), nullable=False),
        sa.Column("target_account_id", sa.String(length=36), nullable=True),
        sa.Column("source_project_id", sa.String(length=500), nullable=True),
        sa.Column("target_project_id", sa.String(length=500), nullable=True),
        sa.Column("characters_json", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("instructions_json", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("assets_json", sa.JSON(), server_default="[]", nullable=False),
        sa.Column(
            "migration_status",
            sa.String(length=40),
            server_default="USER_REVIEW_REQUIRED",
            nullable=False,
        ),
        sa.Column(
            "verification_status",
            sa.String(length=40),
            server_default="USER_REVIEW_REQUIRED",
            nullable=False,
        ),
        sa.Column("trigger_reason", sa.String(length=240), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "migration_status IN ('PLANNED', 'USER_REVIEW_REQUIRED', 'APPROVED', "
            "'MIGRATING', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="ck_flow_migration_status",
        ),
        sa.CheckConstraint(
            "verification_status IN ('PENDING', 'USER_REVIEW_REQUIRED', 'VERIFIED', 'FAILED')",
            name="ck_flow_migration_verification",
        ),
        sa.ForeignKeyConstraint(["source_binding_id"], ["provider_projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["local_project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_account_id"], ["provider_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["target_account_id"], ["provider_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_flow_migration_plans_source_binding_id",
        "flow_migration_plans",
        ["source_binding_id"],
    )
    op.create_index(
        "ix_flow_migration_plans_local_project_id",
        "flow_migration_plans",
        ["local_project_id"],
    )
    op.create_index(
        "ix_flow_migration_plans_source_account_id",
        "flow_migration_plans",
        ["source_account_id"],
    )
    op.create_index(
        "ix_flow_migration_plans_target_account_id",
        "flow_migration_plans",
        ["target_account_id"],
    )
    op.create_index(
        "ix_flow_migration_plans_migration_status",
        "flow_migration_plans",
        ["migration_status"],
    )
    op.create_index(
        "uq_flow_migration_active_binding",
        "flow_migration_plans",
        ["source_binding_id"],
        unique=True,
        sqlite_where=sa.text(
            "migration_status IN ('PLANNED', 'USER_REVIEW_REQUIRED', 'APPROVED', 'MIGRATING')"
        ),
        postgresql_where=sa.text(
            "migration_status IN ('PLANNED', 'USER_REVIEW_REQUIRED', 'APPROVED', 'MIGRATING')"
        ),
    )


def downgrade() -> None:
    tables = _tables()
    if "provider_projects" not in tables:
        return
    if "flow_migration_plans" in tables and _scalar("SELECT id FROM flow_migration_plans LIMIT 1"):
        raise RuntimeError("Flow affinity downgrade would discard migration plans")
    nullable_remote = _scalar("SELECT id FROM provider_projects WHERE provider_project_id IS NULL LIMIT 1")
    if nullable_remote is not None:
        raise RuntimeError(
            "Flow affinity downgrade requires every provider project binding to have a remote project id"
        )
    duplicate_legacy = _scalar(
        "SELECT 1 FROM provider_projects GROUP BY local_project_id, provider, provider_account_id "
        "HAVING COUNT(*) > 1 LIMIT 1"
    )
    if duplicate_legacy is not None:
        raise RuntimeError("Flow affinity downgrade cannot restore the legacy binding uniqueness")

    if "flow_migration_plans" in tables:
        op.drop_index("uq_flow_migration_active_binding", table_name="flow_migration_plans")
        op.drop_index("ix_flow_migration_plans_migration_status", table_name="flow_migration_plans")
        op.drop_index("ix_flow_migration_plans_target_account_id", table_name="flow_migration_plans")
        op.drop_index("ix_flow_migration_plans_source_account_id", table_name="flow_migration_plans")
        op.drop_index("ix_flow_migration_plans_local_project_id", table_name="flow_migration_plans")
        op.drop_index("ix_flow_migration_plans_source_binding_id", table_name="flow_migration_plans")
        op.drop_table("flow_migration_plans")

    op.drop_index("uq_generation_flow_poll_identity", table_name="generation_jobs")
    with op.batch_alter_table("generation_jobs") as batch_op:
        batch_op.drop_constraint("ck_generation_flow_poll_identity", type_="check")
        batch_op.drop_index("ix_generation_jobs_provider_project_id")
        batch_op.drop_column("provider_project_id")

    op.drop_index("uq_flow_remote_project_owner", table_name="provider_projects")
    op.drop_index("uq_non_flow_provider_project_account", table_name="provider_projects")
    op.drop_index("uq_flow_active_local_project", table_name="provider_projects")
    with op.batch_alter_table("provider_projects") as batch_op:
        batch_op.drop_index("ix_provider_projects_provisioning_expires_at")
        batch_op.drop_index("ix_provider_projects_provisioning_token")
        batch_op.drop_constraint("ck_provider_project_remote_id", type_="check")
        batch_op.drop_constraint("ck_provider_project_version", type_="check")
        batch_op.drop_constraint("ck_provider_project_status", type_="check")
        batch_op.drop_column("provisioning_expires_at")
        batch_op.drop_column("provisioning_token")
        batch_op.drop_column("migration_required_at")
        batch_op.drop_column("ready_at")
        batch_op.drop_column("status_reason")
        batch_op.drop_column("version")
        batch_op.alter_column(
            "provider_project_id",
            existing_type=sa.String(length=500),
            nullable=False,
        )
        batch_op.create_unique_constraint(
            "uq_provider_project",
            ["local_project_id", "provider", "provider_account_id"],
        )
