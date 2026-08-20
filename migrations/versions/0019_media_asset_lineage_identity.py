"""Separate content reuse from media lineage identity.

Revision ID: 0019_media_asset_lineage_identity
Revises: 0018_postgres_timeline_vectors
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_media_asset_lineage_identity"
down_revision: str | None = "0018_postgres_timeline_vectors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_CONSTRAINT = "uq_asset_project_hash_type"
NEW_CONSTRAINT = "uq_media_asset_lineage_hash"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _constraints() -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_unique_constraints("media_assets")
        if item.get("name")
    }


def _lineage_key(row: dict[str, object]) -> str:
    associations = (
        ("candidate", row.get("generation_candidate_id")),
        ("shot", row.get("shot_id")),
        ("parent", row.get("parent_asset_id")),
        ("character", row.get("character_id")),
        ("scene", row.get("scene_id")),
    )
    parts = [f"{name}:{value}" for name, value in associations if value]
    return "|".join(parts) if parts else "shared"


def upgrade() -> None:
    if "media_assets" not in _tables():
        return
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("media_assets")}
    if "lineage_key" not in columns:
        op.add_column(
            "media_assets",
            sa.Column("lineage_key", sa.String(length=500), nullable=True),
        )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, generation_candidate_id, shot_id, parent_asset_id, character_id, scene_id "
            "FROM media_assets"
        )
    ).mappings()
    for row in rows:
        bind.execute(
            sa.text("UPDATE media_assets SET lineage_key = :lineage_key WHERE id = :id"),
            {"id": row["id"], "lineage_key": _lineage_key(dict(row))},
        )
    constraints = _constraints()
    with op.batch_alter_table("media_assets") as batch_op:
        batch_op.alter_column(
            "lineage_key",
            existing_type=sa.String(length=500),
            nullable=False,
        )
        if OLD_CONSTRAINT in constraints:
            batch_op.drop_constraint(OLD_CONSTRAINT, type_="unique")
        if NEW_CONSTRAINT not in constraints:
            batch_op.create_unique_constraint(
                NEW_CONSTRAINT,
                ["project_id", "sha256", "asset_type", "lineage_key"],
            )


def downgrade() -> None:
    if "media_assets" not in _tables():
        return
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT project_id, sha256, asset_type, COUNT(*) AS count "
                "FROM media_assets GROUP BY project_id, sha256, asset_type HAVING COUNT(*) > 1"
            )
        )
        .first()
    )
    if duplicates:
        raise RuntimeError(
            "media assets now contain distinct lineage rows for shared content; "
            "downgrade requires explicit reconciliation"
        )
    constraints = _constraints()
    with op.batch_alter_table("media_assets") as batch_op:
        if NEW_CONSTRAINT in constraints:
            batch_op.drop_constraint(NEW_CONSTRAINT, type_="unique")
        if OLD_CONSTRAINT not in constraints:
            batch_op.create_unique_constraint(
                OLD_CONSTRAINT,
                ["project_id", "sha256", "asset_type"],
            )
        batch_op.drop_column("lineage_key")
