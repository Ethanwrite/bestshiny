"""Prevent cross-project shot continuity links.

Revision ID: 0010_shot_lineage_invariants
Revises: 0009_generation_job_claim_lease
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_shot_lineage_invariants"
down_revision: str | None = "0009_generation_job_claim_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SQLITE_TRIGGER_NAMES = (
    "trg_shots_previous_same_project_insert",
    "trg_shots_previous_same_project_update",
)

SQLITE_CHECK = """(
    NEW.previous_shot_id = NEW.id OR NOT EXISTS (
        SELECT 1
        FROM shots AS previous
        JOIN scenes AS previous_scene ON previous_scene.id = previous.scene_id
        JOIN episodes AS previous_episode ON previous_episode.id = previous_scene.episode_id
        JOIN scenes AS current_scene ON current_scene.id = NEW.scene_id
        JOIN episodes AS current_episode ON current_episode.id = current_scene.episode_id
        WHERE previous.id = NEW.previous_shot_id
          AND previous_episode.project_id = current_episode.project_id
    )
)"""

SQLITE_CREATE_STATEMENTS = (
    f"""CREATE TRIGGER IF NOT EXISTS trg_shots_previous_same_project_insert
    BEFORE INSERT ON shots
    WHEN NEW.previous_shot_id IS NOT NULL AND {SQLITE_CHECK}
    BEGIN SELECT RAISE(ABORT, 'previous shot must belong to the same project'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS trg_shots_previous_same_project_update
    BEFORE UPDATE OF scene_id, previous_shot_id ON shots
    WHEN NEW.previous_shot_id IS NOT NULL AND {SQLITE_CHECK}
    BEGIN SELECT RAISE(ABORT, 'previous shot must belong to the same project'); END""",
)

POSTGRES_CREATE_STATEMENTS = (
    """CREATE OR REPLACE FUNCTION enforce_shot_previous_same_project()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.previous_shot_id IS NOT NULL AND (
            NEW.previous_shot_id = NEW.id OR NOT EXISTS (
                SELECT 1
                FROM shots AS previous
                JOIN scenes AS previous_scene ON previous_scene.id = previous.scene_id
                JOIN episodes AS previous_episode ON previous_episode.id = previous_scene.episode_id
                JOIN scenes AS current_scene ON current_scene.id = NEW.scene_id
                JOIN episodes AS current_episode ON current_episode.id = current_scene.episode_id
                WHERE previous.id = NEW.previous_shot_id
                  AND previous_episode.project_id = current_episode.project_id
            )
        ) THEN
            RAISE EXCEPTION 'previous shot must belong to the same project'
            USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END; $$""",
    """CREATE TRIGGER trg_shots_previous_same_project
    BEFORE INSERT OR UPDATE OF scene_id, previous_shot_id ON shots
    FOR EACH ROW EXECUTE FUNCTION enforce_shot_previous_same_project()""",
)


def _validate_existing_rows(bind: sa.engine.Connection) -> None:
    invalid = bind.execute(
        sa.text(
            """SELECT current.id
            FROM shots AS current
            JOIN scenes AS current_scene ON current_scene.id = current.scene_id
            JOIN episodes AS current_episode ON current_episode.id = current_scene.episode_id
            LEFT JOIN shots AS previous ON previous.id = current.previous_shot_id
            LEFT JOIN scenes AS previous_scene ON previous_scene.id = previous.scene_id
            LEFT JOIN episodes AS previous_episode ON previous_episode.id = previous_scene.episode_id
            WHERE current.previous_shot_id IS NOT NULL
              AND (
                current.previous_shot_id = current.id
                OR previous.id IS NULL
                OR previous_episode.project_id <> current_episode.project_id
              )
            ORDER BY current.id"""
        )
    ).fetchmany(10)
    if invalid:
        ids = [str(row[0]) for row in invalid]
        raise RuntimeError(
            f"shots contain invalid cross-project or self continuity links; repair before migration: {ids}"
        )


def upgrade() -> None:
    bind = op.get_bind()
    if "shots" not in sa.inspect(bind).get_table_names():
        return
    _validate_existing_rows(bind)
    if bind.dialect.name == "sqlite":
        statements: Sequence[str] = SQLITE_CREATE_STATEMENTS
    elif bind.dialect.name == "postgresql":
        statements = POSTGRES_CREATE_STATEMENTS
    else:
        raise RuntimeError(f"unsupported shot-lineage database dialect: {bind.dialect.name}")
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_shots_previous_same_project ON shots")
        op.execute("DROP FUNCTION IF EXISTS enforce_shot_previous_same_project()")
        return
    for trigger_name in SQLITE_TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
