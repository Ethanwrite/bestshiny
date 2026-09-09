"""Per-shot cinematography plan: the Cinematography Skill's decisions, on the shot.

The Skill runtime now resolves ``cinematography_design`` to the installed
``cinematography`` Skill and runs it per compiled shot. Its validated output -
framing, angle, height, lens intent, one dominant movement, motivated light,
start and end composition - plus the invocation audit (Skill name, version,
content hash, execution mode, fallback reason) lives on the shot, where the
prompt compiler reads it between the timeline state and a caller's explicit
overrides. A shot with no plan compiles exactly as before (locked-off camera,
preserved light) and the compilation record says the stage fell back.

One plain ``ADD COLUMN``, like ``0073`` before it: SQLite batch mode would
recreate ``shots`` and trip the character-state trigger that names the table.

Revision ID: 0082_shot_cinematography_plan
Revises: 0081_veo_discrete_durations
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0082_shot_cinematography_plan"
down_revision: str | None = "0081_veo_discrete_durations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

COLUMN = "cinematography_json"


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    columns = _columns("shots")
    if not columns:
        # Historical integrity fixtures carry only the tables owned by the
        # revision under test; they are not deployable platform databases.
        return
    if COLUMN in columns:
        # Skipped and logged rather than raised: the api's start command is
        # `alembic upgrade head && uvicorn`, and a raise here is a permanent
        # restart loop with no health endpoint.
        logger.warning("shots.%s already exists; skipping its migration", COLUMN)
        return
    op.add_column(
        "shots",
        sa.Column(COLUMN, sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    if COLUMN not in _columns("shots"):
        return
    op.drop_column("shots", COLUMN)
