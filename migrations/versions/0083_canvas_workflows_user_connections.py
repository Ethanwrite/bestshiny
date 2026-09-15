"""Canvas workflows and a workspace's own provider connections.

Two features that meet at the generation gateway:

- ``user_connections`` holds a workspace's own API credentials ("bring your
  own key") for chat, image and video providers. The secret is Fernet
  ciphertext bound to the row. A generation on a connection is paid by that
  provider account, so ``generation_jobs.connection_id`` names it and the
  gateway reserves no credits and takes no platform spend authorization for
  the job - everything else about the job is unchanged.
- ``workflows``, ``workflow_runs`` and ``workflow_node_runs`` are the canvas:
  a graph of typed nodes a user builds, and the durable record of each run
  the worker advances. A generation node creates an ordinary generation job,
  which remains the authority for that job's lifecycle and charge.

``generation_jobs.connection_id`` is a plain ``ADD COLUMN`` without a foreign
key, like ``deleted_by`` before it: SQLite would rebuild the table to add one.

Revision ID: 0083_canvas_workflows_user_connections
Revises: 0082_shot_cinematography_plan
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0083_canvas_workflows_user_connections"
down_revision: str | None = "0082_shot_cinematography_plan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

JOB_COLUMN = "connection_id"
JOB_INDEX = "ix_generation_jobs_connection_id"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _create_user_connections() -> None:
    op.create_table(
        "user_connections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("protocol", sa.String(length=40), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("models_json", sa.JSON(), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("secret_hint", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(length=36), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'INVALID', 'DELETED')",
            name="ck_user_connection_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_user_connections_workspace_id"), "user_connections", ["workspace_id"], unique=False
    )
    op.create_index(op.f("ix_user_connections_created_by"), "user_connections", ["created_by"], unique=False)
    op.create_index(
        "ix_user_connections_workspace_live",
        "user_connections",
        ["workspace_id", "created_at"],
        unique=False,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def _create_workflows() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("graph_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("updated_by", sa.String(length=36), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(length=36), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("version >= 1", name="ck_workflow_version"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_workflows_project_id"), "workflows", ["project_id"], unique=False)
    op.create_index(op.f("ix_workflows_workspace_id"), "workflows", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_workflows_created_by"), "workflows", ["created_by"], unique=False)
    op.create_index(
        "ix_workflows_project_live",
        "workflows",
        ["project_id", "updated_at"],
        unique=False,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def _create_workflow_runs() -> None:
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("enforce_plan", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("graph_json", sa.JSON(), nullable=False),
        sa.Column("graph_hash", sa.String(length=64), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column("scope_node_ids_json", sa.JSON(), nullable=False),
        sa.Column("force_node_ids_json", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_advance_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_workflow_run_status",
        ),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_workflow_runs_workflow_id"), "workflow_runs", ["workflow_id"], unique=False)
    op.create_index(op.f("ix_workflow_runs_project_id"), "workflow_runs", ["project_id"], unique=False)
    op.create_index(op.f("ix_workflow_runs_workspace_id"), "workflow_runs", ["workspace_id"], unique=False)
    op.create_index(op.f("ix_workflow_runs_created_by"), "workflow_runs", ["created_by"], unique=False)
    op.create_index("ix_workflow_runs_due", "workflow_runs", ["status", "next_advance_at"], unique=False)


def _create_workflow_node_runs() -> None:
    op.create_table(
        "workflow_node_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("node_type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("inputs_json", sa.JSON(), nullable=False),
        sa.Column("outputs_json", sa.JSON(), nullable=False),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("generation_job_id", sa.String(length=36), nullable=True),
        sa.Column("connection_id", sa.String(length=36), nullable=True),
        sa.Column("cached_from_node_run_id", sa.String(length=36), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED', 'CACHED')",
            name="ck_workflow_node_run_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["workflow_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_id"], ["workflows.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["generation_job_id"], ["generation_jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "node_id", name="uq_workflow_node_run_node"),
    )
    op.create_index(op.f("ix_workflow_node_runs_run_id"), "workflow_node_runs", ["run_id"], unique=False)
    op.create_index(
        op.f("ix_workflow_node_runs_generation_job_id"),
        "workflow_node_runs",
        ["generation_job_id"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_node_runs_cache",
        "workflow_node_runs",
        ["workflow_id", "node_id", "fingerprint"],
        unique=False,
    )


def upgrade() -> None:
    tables = _tables()
    if "generation_jobs" not in tables or "workspaces" not in tables:
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return
    # Each object is skipped and logged when it already exists rather than
    # raised on: the api's start command is `alembic upgrade head && uvicorn`,
    # and a raise here is a permanent restart loop with no health endpoint.
    if JOB_COLUMN in _columns("generation_jobs"):
        logger.warning("generation_jobs.%s already exists; skipping it", JOB_COLUMN)
    else:
        op.add_column("generation_jobs", sa.Column(JOB_COLUMN, sa.String(length=36), nullable=True))
        op.create_index(JOB_INDEX, "generation_jobs", [JOB_COLUMN], unique=False)
    for table, create in (
        ("user_connections", _create_user_connections),
        ("workflows", _create_workflows),
        ("workflow_runs", _create_workflow_runs),
        ("workflow_node_runs", _create_workflow_node_runs),
    ):
        if table in _tables():
            logger.warning("%s already exists; skipping its creation", table)
            continue
        create()


def downgrade() -> None:
    tables = _tables()
    for table in ("workflow_node_runs", "workflow_runs", "workflows", "user_connections"):
        if table in tables:
            op.drop_table(table)
    if JOB_COLUMN in _columns("generation_jobs"):
        indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("generation_jobs")}
        if JOB_INDEX in indexes:
            op.drop_index(JOB_INDEX, table_name="generation_jobs")
        # A plain DROP COLUMN, as 0072 does: batch mode would rebuild a table
        # that carries partial indexes and check constraints.
        op.drop_column("generation_jobs", JOB_COLUMN)
