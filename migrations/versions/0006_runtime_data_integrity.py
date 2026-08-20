"""Enforce runtime flag scopes, metric idempotency and compatible deduplication.

Revision ID: 0006_runtime_data_integrity
Revises: 0005_visual_runtime
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_runtime_data_integrity"
down_revision: str | None = "0005_visual_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GLOBAL_SCOPE_KEY = "global"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> dict[str, dict]:  # type: ignore[type-arg]
    return {column["name"]: column for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _unique_constraints(table_name: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table_name)
        if constraint.get("name")
    }


def _indexes(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name) if index.get("name")}


def _scope_key(project_id: str | None) -> str:
    return f"project:{project_id}" if project_id else GLOBAL_SCOPE_KEY


def _upgrade_feature_flags() -> None:
    if "feature_flags" not in _tables():
        return
    if "scope_key" not in _columns("feature_flags"):
        op.add_column("feature_flags", sa.Column("scope_key", sa.String(50), nullable=True))

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, name, project_id FROM feature_flags "
            "ORDER BY updated_at DESC, created_at DESC, id DESC"
        )
    ).mappings()
    retained: dict[tuple[str, str], str] = {}
    scopes_by_id: dict[str, str] = {}
    duplicate_ids: list[str] = []
    for row in rows:
        scope_key = _scope_key(row["project_id"])
        uniqueness_key = (row["name"], scope_key)
        if uniqueness_key in retained:
            duplicate_ids.append(row["id"])
        else:
            retained[uniqueness_key] = row["id"]
            scopes_by_id[row["id"]] = scope_key

    # 0005 allowed duplicate global rows because NULL values are distinct in both
    # SQLite and PostgreSQL. Preserve the latest override and discard older copies.
    for duplicate_id in duplicate_ids:
        bind.execute(sa.text("DELETE FROM feature_flags WHERE id = :id"), {"id": duplicate_id})
    for row_id, scope_key in scopes_by_id.items():
        bind.execute(
            sa.text("UPDATE feature_flags SET scope_key = :scope_key WHERE id = :id"),
            {"id": row_id, "scope_key": scope_key},
        )

    unique_constraints = _unique_constraints("feature_flags")
    scope_column = _columns("feature_flags")["scope_key"]
    needs_batch = (
        bool(scope_column.get("nullable", True))
        or "uq_feature_flag_scope" in unique_constraints
        or "uq_feature_flag_name_scope_key" not in unique_constraints
    )
    if needs_batch:
        with op.batch_alter_table("feature_flags") as batch_op:
            if "uq_feature_flag_scope" in unique_constraints:
                batch_op.drop_constraint("uq_feature_flag_scope", type_="unique")
            if bool(scope_column.get("nullable", True)):
                batch_op.alter_column("scope_key", existing_type=sa.String(50), nullable=False)
            if "uq_feature_flag_name_scope_key" not in unique_constraints:
                batch_op.create_unique_constraint(
                    "uq_feature_flag_name_scope_key",
                    ["name", "scope_key"],
                )
    if "ix_feature_flags_scope_key" not in _indexes("feature_flags"):
        op.create_index("ix_feature_flags_scope_key", "feature_flags", ["scope_key"])


def _upgrade_model_metrics() -> None:
    if "model_metrics" not in _tables():
        return
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, generation_job_id, metric_name FROM model_metrics "
            "WHERE generation_job_id IS NOT NULL ORDER BY created_at, id"
        )
    ).mappings()
    retained: set[tuple[str, str]] = set()
    duplicate_ids: list[str] = []
    for row in rows:
        uniqueness_key = (row["generation_job_id"], row["metric_name"])
        if uniqueness_key in retained:
            duplicate_ids.append(row["id"])
        else:
            retained.add(uniqueness_key)
    for duplicate_id in duplicate_ids:
        bind.execute(sa.text("DELETE FROM model_metrics WHERE id = :id"), {"id": duplicate_id})

    if "uq_model_metric_job_name" not in _unique_constraints("model_metrics"):
        with op.batch_alter_table("model_metrics") as batch_op:
            batch_op.create_unique_constraint(
                "uq_model_metric_job_name",
                ["generation_job_id", "metric_name"],
            )


def _upgrade_production_trace_index() -> None:
    if "production_traces" not in _tables():
        return
    inspector = sa.inspect(op.get_bind())
    trace_index = next(
        (
            index
            for index in inspector.get_indexes("production_traces")
            if index.get("name") == "ix_production_traces_trace_id"
        ),
        None,
    )
    trace_constraints = [
        constraint
        for constraint in inspector.get_unique_constraints("production_traces")
        if constraint.get("column_names") == ["trace_id"]
    ]
    if trace_index and trace_index.get("unique") and not trace_constraints:
        return

    # SQLite reports the 0005 column-level UNIQUE constraint without a name.
    # A naming convention lets batch mode address it while recreating the table.
    naming_convention = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
    with op.batch_alter_table("production_traces", naming_convention=naming_convention) as batch_op:
        if trace_index:
            batch_op.drop_index("ix_production_traces_trace_id")
        for constraint in trace_constraints:
            constraint_name = constraint.get("name") or "uq_production_traces_trace_id"
            batch_op.drop_constraint(constraint_name, type_="unique")
        batch_op.create_index("ix_production_traces_trace_id", ["trace_id"], unique=True)


def upgrade() -> None:
    _upgrade_feature_flags()
    _upgrade_model_metrics()
    _upgrade_production_trace_index()


def downgrade() -> None:
    if "production_traces" in _tables():
        inspector = sa.inspect(op.get_bind())
        trace_index = next(
            (
                index
                for index in inspector.get_indexes("production_traces")
                if index.get("name") == "ix_production_traces_trace_id"
            ),
            None,
        )
        trace_constraints = [
            constraint
            for constraint in inspector.get_unique_constraints("production_traces")
            if constraint.get("column_names") == ["trace_id"]
        ]
        if trace_index and trace_index.get("unique") and not trace_constraints:
            with op.batch_alter_table("production_traces") as batch_op:
                batch_op.drop_index("ix_production_traces_trace_id")
                batch_op.create_unique_constraint(
                    "uq_production_traces_trace_id_legacy",
                    ["trace_id"],
                )
                batch_op.create_index("ix_production_traces_trace_id", ["trace_id"], unique=False)

    if "model_metrics" in _tables() and "uq_model_metric_job_name" in _unique_constraints("model_metrics"):
        with op.batch_alter_table("model_metrics") as batch_op:
            batch_op.drop_constraint("uq_model_metric_job_name", type_="unique")

    if "feature_flags" not in _tables() or "scope_key" not in _columns("feature_flags"):
        return
    indexes = _indexes("feature_flags")
    unique_constraints = _unique_constraints("feature_flags")
    with op.batch_alter_table("feature_flags") as batch_op:
        if "ix_feature_flags_scope_key" in indexes:
            batch_op.drop_index("ix_feature_flags_scope_key")
        if "uq_feature_flag_name_scope_key" in unique_constraints:
            batch_op.drop_constraint("uq_feature_flag_name_scope_key", type_="unique")
        if "uq_feature_flag_scope" not in unique_constraints:
            batch_op.create_unique_constraint("uq_feature_flag_scope", ["name", "project_id"])
        batch_op.drop_column("scope_key")
