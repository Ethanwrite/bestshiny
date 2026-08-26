"""Give the asset-registry and project-style guards an integrity SQLSTATE.

Both functions raise with no ERRCODE, so PostgreSQL reports SQLSTATE `P0001`
and SQLAlchemy raises `ProgrammingError`. The identical guards under SQLite —
`RAISE(ABORT, ...)` — raise `IntegrityError`. Eight invariants therefore had a
different exception type depending on the engine, and `except IntegrityError`
around any of them caught on the development database and not on the production
one.

Every other trigger in this schema already declares its SQLSTATE; these eight
were the omission. `23514` is check_violation, which SQLAlchemy maps to
`IntegrityError` on both engines. Nothing about *when* a guard fires changes —
only how the failure is classified once it has.

SQLite needs no counterpart: its triggers already abort with a constraint
error, which is the behaviour this brings PostgreSQL into line with.

Revision ID: 0039_integrity_errcodes
Revises: 0038_reference_voice_capability
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_integrity_errcodes"
down_revision: str | None = "0038_reference_voice_capability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# CREATE OR REPLACE keeps every trigger already bound to these functions bound
# to the new body, so no trigger is dropped or recreated here.
ASSET_REGISTRY_WITH_ERRCODE = """\
CREATE OR REPLACE FUNCTION enforce_asset_registry_consistency()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'assets' THEN
        IF NEW.canonical_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.canonical_version_id AND asset_id = NEW.id
        ) THEN RAISE EXCEPTION 'canonical version must belong to the same asset'
            USING ERRCODE = '23514'; END IF;
        IF TG_OP = 'UPDATE'
           AND NEW.canonical_version_id IS DISTINCT FROM OLD.canonical_version_id AND (
            NEW.canonical_version_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM asset_canonical_promotions
                WHERE asset_id = NEW.id
                  AND to_version_id = NEW.canonical_version_id
                  AND from_version_id IS NOT DISTINCT FROM OLD.canonical_version_id
                  AND created_at >= OLD.updated_at
            )
        ) THEN RAISE EXCEPTION 'canonical change requires a fresh promotion record'
            USING ERRCODE = '23514'; END IF;
    ELSIF TG_TABLE_NAME = 'asset_versions' THEN
        IF NEW.parent_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.parent_version_id AND asset_id = NEW.asset_id
        ) THEN RAISE EXCEPTION 'parent version must belong to the same asset'
            USING ERRCODE = '23514'; END IF;
    ELSIF TG_TABLE_NAME = 'asset_canonical_promotions' THEN
        IF NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.to_version_id AND asset_id = NEW.asset_id
        ) OR (NEW.from_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.from_version_id AND asset_id = NEW.asset_id
        )) THEN RAISE EXCEPTION 'promotion versions must belong to the same asset'
            USING ERRCODE = '23514'; END IF;
    END IF;
    RETURN NEW;
END; $$"""

PROJECT_STYLE_WITH_ERRCODE = """\
CREATE OR REPLACE FUNCTION enforce_project_style_consistency()
RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  IF TG_TABLE_NAME = 'style_embeddings' AND NOT EXISTS (
    SELECT 1 FROM asset_versions version JOIN assets asset ON asset.id = version.asset_id
    WHERE version.id = NEW.asset_version_id AND asset.project_id = NEW.project_id
      AND asset.asset_type = 'STYLE'
  ) THEN RAISE EXCEPTION 'style embedding must belong to a STYLE version in the project'
            USING ERRCODE = '23514';
  ELSIF TG_TABLE_NAME = 'project_style_locks' AND NOT EXISTS (
    SELECT 1 FROM projects project JOIN assets asset ON asset.project_id = project.id
    JOIN asset_versions version
      ON version.id = NEW.style_version_id AND version.asset_id = asset.id
    JOIN style_embeddings embedding ON embedding.id = NEW.style_embedding_id
      AND embedding.asset_version_id = version.id AND embedding.project_id = project.id
    WHERE project.id = NEW.project_id AND asset.id = NEW.style_asset_id
      AND asset.asset_type = 'STYLE' AND asset.canonical_version_id = version.id
      AND version.status = 'READY'
  ) THEN RAISE EXCEPTION 'project style lock requires a canonical STYLE version and embedding'
            USING ERRCODE = '23514';
  ELSIF TG_TABLE_NAME = 'projects'
    AND NEW.canonical_style_version_id IS DISTINCT FROM OLD.canonical_style_version_id
    AND (OLD.canonical_style_version_id IS NOT NULL OR NEW.canonical_style_version_id IS NULL
      OR NOT EXISTS (
        SELECT 1 FROM project_style_locks style_lock
        WHERE style_lock.project_id = NEW.id
        AND style_lock.style_version_id = NEW.canonical_style_version_id
        AND style_lock.created_at >= OLD.updated_at))
  THEN RAISE EXCEPTION 'project style can only be locked once through a fresh style lock'
            USING ERRCODE = '23514';
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
  ) THEN RAISE EXCEPTION 'candidate style evaluation provenance is inconsistent'
            USING ERRCODE = '23514';
  END IF; RETURN NEW; END; $$"""

ASSET_REGISTRY_WITHOUT_ERRCODE = """\
CREATE OR REPLACE FUNCTION enforce_asset_registry_consistency()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'assets' THEN
        IF NEW.canonical_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.canonical_version_id AND asset_id = NEW.id
        ) THEN RAISE EXCEPTION 'canonical version must belong to the same asset'; END IF;
        IF TG_OP = 'UPDATE'
           AND NEW.canonical_version_id IS DISTINCT FROM OLD.canonical_version_id AND (
            NEW.canonical_version_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM asset_canonical_promotions
                WHERE asset_id = NEW.id
                  AND to_version_id = NEW.canonical_version_id
                  AND from_version_id IS NOT DISTINCT FROM OLD.canonical_version_id
                  AND created_at >= OLD.updated_at
            )
        ) THEN RAISE EXCEPTION 'canonical change requires a fresh promotion record'; END IF;
    ELSIF TG_TABLE_NAME = 'asset_versions' THEN
        IF NEW.parent_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.parent_version_id AND asset_id = NEW.asset_id
        ) THEN RAISE EXCEPTION 'parent version must belong to the same asset'; END IF;
    ELSIF TG_TABLE_NAME = 'asset_canonical_promotions' THEN
        IF NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.to_version_id AND asset_id = NEW.asset_id
        ) OR (NEW.from_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.from_version_id AND asset_id = NEW.asset_id
        )) THEN RAISE EXCEPTION 'promotion versions must belong to the same asset'; END IF;
    END IF;
    RETURN NEW;
END; $$"""

PROJECT_STYLE_WITHOUT_ERRCODE = """\
CREATE OR REPLACE FUNCTION enforce_project_style_consistency()
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


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text(ASSET_REGISTRY_WITH_ERRCODE))
    op.execute(sa.text(PROJECT_STYLE_WITH_ERRCODE))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text(ASSET_REGISTRY_WITHOUT_ERRCODE))
    op.execute(sa.text(PROJECT_STYLE_WITHOUT_ERRCODE))
