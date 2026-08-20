"""Align PostgreSQL timeline embeddings with the live vector schema.

Revision ID: 0018_postgres_timeline_vectors
Revises: 0017_explicit_legacy_workspace_claims
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_postgres_timeline_vectors"
down_revision: str | None = "0017_explicit_legacy_workspace_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_COLUMNS = (
    "semantic_embedding",
    "visual_embedding",
    "camera_embedding",
    "character_track_embedding",
)


def _postgresql_columns() -> dict[str, str]:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return {}
    inspector = sa.inspect(bind)
    if "timeline_states" not in inspector.get_table_names():
        return {}
    return {
        str(column["name"]): str(column["type"]).lower()
        for column in inspector.get_columns("timeline_states")
    }


def upgrade() -> None:
    columns = _postgresql_columns()
    if not columns:
        return
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    for column_name in EMBEDDING_COLUMNS:
        existing_type = columns.get(column_name)
        if existing_type is None or existing_type.startswith("vector"):
            continue
        if existing_type not in {"json", "jsonb"}:
            raise RuntimeError(
                f"timeline_states.{column_name} has unsupported type {existing_type}; "
                "reconcile it before upgrading"
            )
        op.execute(
            sa.text(
                "ALTER TABLE timeline_states "
                f"ALTER COLUMN {column_name} TYPE vector(16) "
                f"USING CASE WHEN {column_name} IS NULL THEN NULL "
                f"ELSE ({column_name}::text)::vector(16) END"
            )
        )


def downgrade() -> None:
    columns = _postgresql_columns()
    if not columns:
        return
    for column_name in EMBEDDING_COLUMNS:
        existing_type = columns.get(column_name)
        if existing_type is None or not existing_type.startswith("vector"):
            continue
        op.execute(
            sa.text(
                "ALTER TABLE timeline_states "
                f"ALTER COLUMN {column_name} TYPE JSON "
                f"USING CASE WHEN {column_name} IS NULL THEN NULL "
                f"ELSE ({column_name}::text)::json END"
            )
        )
