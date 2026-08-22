"""Add immutable project style locks, embeddings, and candidate style QA.

Revision ID: 0029_project_style_lock
Revises: 0028_persistent_character_state
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_project_style_lock"
down_revision: str | None = "0028_persistent_character_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CORE_TABLES = {
    "projects",
    "users",
    "assets",
    "asset_versions",
    "generation_candidates",
    "media_assets",
    "shots",
    "scenes",
    "episodes",
}
STYLE_TABLES = (
    "style_embeddings",
    "project_style_locks",
    "candidate_style_evaluations",
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _skip_recovery_or_require_complete_core() -> bool:
    tables = _tables()
    if not {"projects", "assets"}.intersection(tables) and not set(STYLE_TABLES).intersection(tables):
        return True
    missing = CORE_TABLES.difference(tables)
    if missing:
        raise RuntimeError(f"Project style migration requires missing tables: {sorted(missing)}")
    present = set(STYLE_TABLES).intersection(tables)
    if present:
        raise RuntimeError(f"Project style migration found partial pre-existing tables: {sorted(present)}")
    return False


def _timestamps() -> tuple[sa.Column, sa.Column]:  # type: ignore[type-arg]
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    if _skip_recovery_or_require_complete_core():
        return
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(sa.Column("canonical_style_version_id", sa.String(length=36)))
        batch_op.create_foreign_key(
            "fk_projects_canonical_style_version",
            "asset_versions",
            ["canonical_style_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_projects_canonical_style_version_id",
            ["canonical_style_version_id"],
        )

    op.create_table(
        "style_embeddings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("asset_version_id", sa.String(length=36), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("algorithm_version", sa.String(length=80), nullable=False),
        sa.Column("embedding_hash", sa.String(length=64), nullable=False),
        sa.Column("source_media_ids", sa.JSON(), nullable=False),
        sa.Column("source_media_hashes", sa.JSON(), nullable=False),
        sa.Column("evidence_kind", sa.String(length=40), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("dimension > 0", name="ck_style_embedding_dimension_positive"),
        sa.CheckConstraint("length(embedding_hash) = 64", name="ck_style_embedding_hash_length"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asset_version_id"], ["asset_versions.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("asset_version_id", "model", name="uq_style_embedding_version_model"),
    )
    op.create_index("ix_style_embeddings_project_id", "style_embeddings", ["project_id"])
    op.create_index("ix_style_embeddings_asset_version_id", "style_embeddings", ["asset_version_id"])

    op.create_table(
        "project_style_locks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("style_asset_id", sa.String(length=36), nullable=False),
        sa.Column("style_version_id", sa.String(length=36), nullable=False),
        sa.Column("style_embedding_id", sa.String(length=36), nullable=False),
        sa.Column("similarity_threshold", sa.Float(), nullable=False),
        sa.Column("minimum_similarity_threshold", sa.Float(), nullable=False),
        sa.Column("drift_limit", sa.Float(), nullable=False),
        sa.Column("max_low_score_fraction", sa.Float(), nullable=False),
        sa.Column("locked_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "similarity_threshold >= 0 AND similarity_threshold <= 1",
            name="ck_style_lock_similarity_range",
        ),
        sa.CheckConstraint(
            "minimum_similarity_threshold >= 0 AND minimum_similarity_threshold <= 1",
            name="ck_style_lock_minimum_range",
        ),
        sa.CheckConstraint("drift_limit >= 0 AND drift_limit <= 1", name="ck_style_lock_drift_range"),
        sa.CheckConstraint(
            "max_low_score_fraction >= 0 AND max_low_score_fraction <= 1",
            name="ck_style_lock_low_fraction_range",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["style_asset_id"], ["assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["style_version_id"], ["asset_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["style_embedding_id"], ["style_embeddings.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["locked_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("project_id", name="uq_project_style_lock_project"),
    )
    for column in (
        "project_id",
        "style_asset_id",
        "style_version_id",
        "style_embedding_id",
        "locked_by_user_id",
    ):
        op.create_index(f"ix_project_style_locks_{column}", "project_style_locks", [column])

    op.create_table(
        "candidate_style_evaluations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("output_asset_id", sa.String(length=36), nullable=False),
        sa.Column("style_lock_id", sa.String(length=36), nullable=False),
        sa.Column("style_version_id", sa.String(length=36), nullable=False),
        sa.Column("style_embedding_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("average_similarity", sa.Float()),
        sa.Column("minimum_similarity", sa.Float()),
        sa.Column("p10_similarity", sa.Float()),
        sa.Column("drift_slope", sa.Float()),
        sa.Column("low_score_fraction", sa.Float()),
        sa.Column("sample_positions", sa.JSON(), nullable=False),
        sa.Column("sample_scores", sa.JSON(), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("evaluator_version", sa.String(length=80), nullable=False),
        sa.Column("evidence_kind", sa.String(length=40), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('PASS', 'FAIL', 'REVIEW_REQUIRED')",
            name="ck_candidate_style_evaluation_status",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["candidate_id"], ["generation_candidates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["output_asset_id"], ["media_assets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["style_lock_id"], ["project_style_locks.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["style_version_id"], ["asset_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["style_embedding_id"], ["style_embeddings.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("candidate_id", name="uq_candidate_style_evaluation_candidate"),
    )
    for column in (
        "project_id",
        "candidate_id",
        "output_asset_id",
        "style_lock_id",
        "style_version_id",
        "style_embedding_id",
        "status",
    ):
        op.create_index(
            f"ix_candidate_style_evaluations_{column}",
            "candidate_style_evaluations",
            [column],
        )
    _install_integrity_triggers()


def _install_integrity_triggers() -> None:
    if op.get_bind().dialect.name == "sqlite":
        statements = (
            """CREATE TRIGGER trg_style_embeddings_consistency BEFORE INSERT ON style_embeddings
            WHEN NOT EXISTS (
                SELECT 1 FROM asset_versions AS version JOIN assets AS asset ON asset.id = version.asset_id
                WHERE version.id = NEW.asset_version_id AND asset.project_id = NEW.project_id
                  AND asset.asset_type = 'STYLE'
            ) BEGIN
              SELECT RAISE(ABORT, 'style embedding must belong to a STYLE version in the project');
            END""",
            """CREATE TRIGGER trg_project_style_locks_consistency BEFORE INSERT ON project_style_locks
            WHEN NOT EXISTS (
                SELECT 1 FROM projects AS project JOIN assets AS asset ON asset.project_id = project.id
                JOIN asset_versions AS version
                  ON version.id = NEW.style_version_id AND version.asset_id = asset.id
                JOIN style_embeddings AS embedding ON embedding.id = NEW.style_embedding_id
                  AND embedding.asset_version_id = version.id AND embedding.project_id = project.id
                WHERE project.id = NEW.project_id AND asset.id = NEW.style_asset_id
                  AND asset.asset_type = 'STYLE' AND asset.canonical_version_id = version.id
                  AND version.status = 'READY'
            ) BEGIN
              SELECT RAISE(ABORT, 'project style lock requires a canonical STYLE version and embedding');
            END""",
            """CREATE TRIGGER trg_projects_style_lock_update
            BEFORE UPDATE OF canonical_style_version_id ON projects
            WHEN NOT (NEW.canonical_style_version_id IS OLD.canonical_style_version_id) AND (
                OLD.canonical_style_version_id IS NOT NULL OR NEW.canonical_style_version_id IS NULL
                OR NOT EXISTS (SELECT 1 FROM project_style_locks AS style_lock
                    WHERE style_lock.project_id = NEW.id
                      AND style_lock.style_version_id = NEW.canonical_style_version_id
                      AND style_lock.created_at >= OLD.updated_at)
            ) BEGIN
              SELECT RAISE(ABORT, 'project style can only be locked once through a fresh style lock');
            END""",
            """CREATE TRIGGER trg_candidate_style_evaluations_consistency
            BEFORE INSERT ON candidate_style_evaluations WHEN NOT EXISTS (
                SELECT 1 FROM generation_candidates AS candidate
                JOIN shots AS shot ON shot.id = candidate.shot_id
                JOIN scenes AS scene ON scene.id = shot.scene_id
                JOIN episodes AS episode ON episode.id = scene.episode_id
                JOIN media_assets AS output ON output.id = NEW.output_asset_id
                JOIN project_style_locks AS style_lock ON style_lock.id = NEW.style_lock_id
                WHERE candidate.id = NEW.candidate_id AND candidate.output_asset_id = NEW.output_asset_id
                  AND episode.project_id = NEW.project_id AND output.project_id = NEW.project_id
                  AND style_lock.project_id = NEW.project_id
                  AND style_lock.style_version_id = NEW.style_version_id
                  AND style_lock.style_embedding_id = NEW.style_embedding_id
            ) BEGIN SELECT RAISE(ABORT, 'candidate style evaluation provenance is inconsistent'); END""",
        )
        for statement in statements:
            op.execute(sa.text(statement))
        for table_name in STYLE_TABLES:
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER trg_{table_name}_append_only_{operation.lower()} "
                        f"BEFORE {operation} ON {table_name} BEGIN SELECT RAISE(ABORT, "
                        f"'{table_name} is append-only'); END"
                    )
                )
        return

    op.execute(
        sa.text(
            """CREATE OR REPLACE FUNCTION enforce_project_style_consistency()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
              IF TG_TABLE_NAME = 'style_embeddings' AND NOT EXISTS (
                SELECT 1 FROM asset_versions version JOIN assets asset ON asset.id = version.asset_id
                WHERE version.id = NEW.asset_version_id AND asset.project_id = NEW.project_id
                  AND asset.asset_type = 'STYLE'
              ) THEN RAISE EXCEPTION 'style embedding must belong to a STYLE version in the project';
              ELSIF TG_TABLE_NAME = 'project_style_locks' AND NOT EXISTS (
                SELECT 1 FROM projects project JOIN assets asset ON asset.project_id = project.id
                JOIN asset_versions version
                  ON version.id = NEW.style_version_id AND version.asset_id = asset.id
                JOIN style_embeddings embedding ON embedding.id = NEW.style_embedding_id
                  AND embedding.asset_version_id = version.id AND embedding.project_id = project.id
                WHERE project.id = NEW.project_id AND asset.id = NEW.style_asset_id
                  AND asset.asset_type = 'STYLE' AND asset.canonical_version_id = version.id
                  AND version.status = 'READY'
              ) THEN RAISE EXCEPTION 'project style lock requires a canonical STYLE version and embedding';
              ELSIF TG_TABLE_NAME = 'projects'
                AND NEW.canonical_style_version_id IS DISTINCT FROM OLD.canonical_style_version_id
                AND (OLD.canonical_style_version_id IS NOT NULL OR NEW.canonical_style_version_id IS NULL
                  OR NOT EXISTS (
                    SELECT 1 FROM project_style_locks style_lock
                    WHERE style_lock.project_id = NEW.id
                    AND style_lock.style_version_id = NEW.canonical_style_version_id
                    AND style_lock.created_at >= OLD.updated_at))
              THEN RAISE EXCEPTION 'project style can only be locked once through a fresh style lock';
              ELSIF TG_TABLE_NAME = 'candidate_style_evaluations' AND NOT EXISTS (
                SELECT 1 FROM generation_candidates candidate JOIN shots shot ON shot.id = candidate.shot_id
                JOIN scenes scene ON scene.id = shot.scene_id
                JOIN episodes episode ON episode.id = scene.episode_id
                JOIN media_assets output ON output.id = NEW.output_asset_id
                JOIN project_style_locks style_lock ON style_lock.id = NEW.style_lock_id
                WHERE candidate.id = NEW.candidate_id AND candidate.output_asset_id = NEW.output_asset_id
                  AND episode.project_id = NEW.project_id AND output.project_id = NEW.project_id
                  AND style_lock.project_id = NEW.project_id
                  AND style_lock.style_version_id = NEW.style_version_id
                  AND style_lock.style_embedding_id = NEW.style_embedding_id
              ) THEN RAISE EXCEPTION 'candidate style evaluation provenance is inconsistent';
              END IF; RETURN NEW; END; $$"""
        )
    )
    op.execute(
        sa.text(
            """CREATE OR REPLACE FUNCTION enforce_project_style_append_only()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
              RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '23000';
              RETURN OLD; END; $$"""
        )
    )
    trigger_statements = (
        "CREATE TRIGGER trg_style_embeddings_consistency BEFORE INSERT ON style_embeddings "
        "FOR EACH ROW EXECUTE FUNCTION enforce_project_style_consistency()",
        "CREATE TRIGGER trg_project_style_locks_consistency BEFORE INSERT ON project_style_locks "
        "FOR EACH ROW EXECUTE FUNCTION enforce_project_style_consistency()",
        "CREATE TRIGGER trg_projects_style_lock_update "
        "BEFORE UPDATE OF canonical_style_version_id ON projects "
        "FOR EACH ROW EXECUTE FUNCTION enforce_project_style_consistency()",
        "CREATE TRIGGER trg_candidate_style_evaluations_consistency "
        "BEFORE INSERT ON candidate_style_evaluations "
        "FOR EACH ROW EXECUTE FUNCTION enforce_project_style_consistency()",
    )
    for statement in trigger_statements:
        op.execute(sa.text(statement))
    for table_name in STYLE_TABLES:
        op.execute(
            sa.text(
                f"CREATE TRIGGER trg_{table_name}_append_only BEFORE UPDATE OR DELETE ON {table_name} "
                "FOR EACH ROW EXECUTE FUNCTION enforce_project_style_append_only()"
            )
        )


def downgrade() -> None:
    if not set(STYLE_TABLES).intersection(_tables()):
        return
    bind = op.get_bind()
    populated_tables = [
        table_name
        for table_name in STYLE_TABLES
        if bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first() is not None
    ]
    locked_project = bind.execute(
        sa.text("SELECT 1 FROM projects WHERE canonical_style_version_id IS NOT NULL LIMIT 1")
    ).first()
    if populated_tables or locked_project is not None:
        raise RuntimeError(
            "Refusing to downgrade project style schema because immutable style evidence exists: "
            f"{populated_tables or ['projects.canonical_style_version_id']}"
        )
    if op.get_bind().dialect.name == "sqlite":
        for name in (
            "trg_style_embeddings_consistency",
            "trg_project_style_locks_consistency",
            "trg_projects_style_lock_update",
            "trg_candidate_style_evaluations_consistency",
        ):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
        for table_name in STYLE_TABLES:
            for operation in ("update", "delete"):
                op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only_{operation}"))
    else:
        for table_name in STYLE_TABLES:
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table_name}_append_only ON {table_name}"))
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_style_embeddings_consistency ON style_embeddings"))
        op.execute(
            sa.text("DROP TRIGGER IF EXISTS trg_project_style_locks_consistency ON project_style_locks")
        )
        op.execute(sa.text("DROP TRIGGER IF EXISTS trg_projects_style_lock_update ON projects"))
        op.execute(
            sa.text(
                "DROP TRIGGER IF EXISTS trg_candidate_style_evaluations_consistency "
                "ON candidate_style_evaluations"
            )
        )
        op.execute(sa.text("DROP FUNCTION IF EXISTS enforce_project_style_append_only()"))
        op.execute(sa.text("DROP FUNCTION IF EXISTS enforce_project_style_consistency()"))
    op.drop_table("candidate_style_evaluations")
    op.drop_table("project_style_locks")
    op.drop_table("style_embeddings")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_index("ix_projects_canonical_style_version_id")
        batch_op.drop_constraint("fk_projects_canonical_style_version", type_="foreignkey")
        batch_op.drop_column("canonical_style_version_id")
