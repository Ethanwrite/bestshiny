"""Enforce logical asset lineage, explicit promotion and immutable history.

Revision ID: 0008_asset_registry_invariants
Revises: 0007_commercial_auth
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_asset_registry_invariants"
down_revision: str | None = "0007_commercial_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SQLITE_TRIGGER_NAMES = (
    "trg_assets_canonical_same_asset_insert",
    "trg_assets_canonical_same_asset_update",
    "trg_assets_canonical_requires_promotion_update",
    "trg_asset_versions_parent_same_asset_insert",
    "trg_asset_promotions_versions_same_asset_insert",
    "trg_asset_versions_append_only_update",
    "trg_asset_versions_append_only_delete",
    "trg_asset_version_media_append_only_update",
    "trg_asset_version_media_append_only_delete",
    "trg_asset_canonical_promotions_append_only_update",
    "trg_asset_canonical_promotions_append_only_delete",
)


SQLITE_CREATE_STATEMENTS = (
    """CREATE TRIGGER IF NOT EXISTS trg_assets_canonical_same_asset_insert
    BEFORE INSERT ON assets WHEN NEW.canonical_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM asset_versions
        WHERE id = NEW.canonical_version_id AND asset_id = NEW.id
    ) BEGIN SELECT RAISE(ABORT, 'canonical version must belong to the same asset'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_assets_canonical_same_asset_update
    BEFORE UPDATE OF id, canonical_version_id ON assets
    WHEN NEW.canonical_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM asset_versions
        WHERE id = NEW.canonical_version_id AND asset_id = NEW.id
    ) BEGIN SELECT RAISE(ABORT, 'canonical version must belong to the same asset'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_assets_canonical_requires_promotion_update
    BEFORE UPDATE OF canonical_version_id ON assets
    WHEN NOT (NEW.canonical_version_id IS OLD.canonical_version_id) AND (
        NEW.canonical_version_id IS NULL OR NOT EXISTS (
            SELECT 1 FROM asset_canonical_promotions
            WHERE asset_id = NEW.id
              AND to_version_id = NEW.canonical_version_id
              AND from_version_id IS OLD.canonical_version_id
              AND created_at >= OLD.updated_at
        )
    ) BEGIN SELECT RAISE(ABORT, 'canonical change requires a fresh promotion record'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_asset_versions_parent_same_asset_insert
    BEFORE INSERT ON asset_versions WHEN NEW.parent_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM asset_versions
        WHERE id = NEW.parent_version_id AND asset_id = NEW.asset_id
    ) BEGIN SELECT RAISE(ABORT, 'parent version must belong to the same asset'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_asset_promotions_versions_same_asset_insert
    BEFORE INSERT ON asset_canonical_promotions WHEN NOT EXISTS (
        SELECT 1 FROM asset_versions WHERE id = NEW.to_version_id AND asset_id = NEW.asset_id
    ) OR (NEW.from_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM asset_versions WHERE id = NEW.from_version_id AND asset_id = NEW.asset_id
    )) BEGIN SELECT RAISE(ABORT, 'promotion versions must belong to the same asset'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_asset_versions_append_only_update
    BEFORE UPDATE ON asset_versions
    BEGIN SELECT RAISE(ABORT, 'asset_versions is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_asset_versions_append_only_delete
    BEFORE DELETE ON asset_versions
    BEGIN SELECT RAISE(ABORT, 'asset_versions is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_asset_version_media_append_only_update
    BEFORE UPDATE ON asset_version_media
    BEGIN SELECT RAISE(ABORT, 'asset_version_media is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_asset_version_media_append_only_delete
    BEFORE DELETE ON asset_version_media
    BEGIN SELECT RAISE(ABORT, 'asset_version_media is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_asset_canonical_promotions_append_only_update
    BEFORE UPDATE ON asset_canonical_promotions
    BEGIN SELECT RAISE(ABORT, 'asset_canonical_promotions is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_asset_canonical_promotions_append_only_delete
    BEFORE DELETE ON asset_canonical_promotions
    BEGIN SELECT RAISE(ABORT, 'asset_canonical_promotions is append-only'); END""",
)


POSTGRES_CREATE_STATEMENTS = (
    """CREATE OR REPLACE FUNCTION enforce_asset_registry_consistency()
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
    END; $$""",
    """CREATE OR REPLACE FUNCTION enforce_asset_registry_append_only()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
        USING ERRCODE = '23000';
        RETURN OLD;
    END; $$""",
    """CREATE TRIGGER trg_assets_canonical_same_asset
    BEFORE INSERT OR UPDATE OF id, canonical_version_id ON assets FOR EACH ROW
    EXECUTE FUNCTION enforce_asset_registry_consistency()""",
    """CREATE TRIGGER trg_asset_versions_parent_same_asset
    BEFORE INSERT ON asset_versions FOR EACH ROW
    EXECUTE FUNCTION enforce_asset_registry_consistency()""",
    """CREATE TRIGGER trg_asset_promotions_versions_same_asset
    BEFORE INSERT ON asset_canonical_promotions FOR EACH ROW
    EXECUTE FUNCTION enforce_asset_registry_consistency()""",
    """CREATE TRIGGER trg_asset_versions_append_only
    BEFORE UPDATE OR DELETE ON asset_versions FOR EACH ROW
    EXECUTE FUNCTION enforce_asset_registry_append_only()""",
    """CREATE TRIGGER trg_asset_version_media_append_only
    BEFORE UPDATE OR DELETE ON asset_version_media FOR EACH ROW
    EXECUTE FUNCTION enforce_asset_registry_append_only()""",
    """CREATE TRIGGER trg_asset_canonical_promotions_append_only
    BEFORE UPDATE OR DELETE ON asset_canonical_promotions FOR EACH ROW
    EXECUTE FUNCTION enforce_asset_registry_append_only()""",
)


def _inconsistent_ids(bind: sa.engine.Connection, query: str) -> list[str]:
    return [str(row[0]) for row in bind.execute(sa.text(query)).fetchmany(10)]


def _validate_existing_rows(bind: sa.engine.Connection) -> None:
    checks = (
        (
            "canonical pointers",
            """SELECT a.id FROM assets AS a
            LEFT JOIN asset_versions AS v ON v.id = a.canonical_version_id
            WHERE a.canonical_version_id IS NOT NULL
              AND (v.id IS NULL OR v.asset_id <> a.id)
            ORDER BY a.id""",
        ),
        (
            "parent version links",
            """SELECT child.id FROM asset_versions AS child
            LEFT JOIN asset_versions AS parent ON parent.id = child.parent_version_id
            WHERE child.parent_version_id IS NOT NULL
              AND (parent.id IS NULL OR parent.asset_id <> child.asset_id)
            ORDER BY child.id""",
        ),
        (
            "promotion target links",
            """SELECT promotion.id FROM asset_canonical_promotions AS promotion
            LEFT JOIN asset_versions AS target ON target.id = promotion.to_version_id
            WHERE target.id IS NULL OR target.asset_id <> promotion.asset_id
            ORDER BY promotion.id""",
        ),
        (
            "promotion source links",
            """SELECT promotion.id FROM asset_canonical_promotions AS promotion
            LEFT JOIN asset_versions AS source ON source.id = promotion.from_version_id
            WHERE promotion.from_version_id IS NOT NULL
              AND (source.id IS NULL OR source.asset_id <> promotion.asset_id)
            ORDER BY promotion.id""",
        ),
    )
    failures = [f"{label}: {ids}" for label, query in checks if (ids := _inconsistent_ids(bind, query))]
    if failures:
        details = "; ".join(failures)
        raise RuntimeError(
            "asset registry contains inconsistent legacy rows; repair them before migration: " + details
        )


def upgrade() -> None:
    bind = op.get_bind()
    required = {"assets", "asset_versions", "asset_version_media", "asset_canonical_promotions"}
    existing = set(sa.inspect(bind).get_table_names())
    present = required.intersection(existing)
    # Recovery snapshots used by pre-Asset-Registry installations can legitimately
    # contain only the runtime tables while already being stamped at a later base
    # revision. There is no asset state to constrain in that case. A partial asset
    # schema is different: silently skipping it would leave real data unprotected.
    if not present:
        return
    missing = required.difference(existing)
    if missing:
        raise RuntimeError(f"asset registry migration requires missing tables: {sorted(missing)}")
    _validate_existing_rows(bind)
    statements: Sequence[str]
    if bind.dialect.name == "postgresql":
        statements = POSTGRES_CREATE_STATEMENTS
    elif bind.dialect.name == "sqlite":
        statements = SQLITE_CREATE_STATEMENTS
    else:
        raise RuntimeError(f"unsupported asset-registry database dialect: {bind.dialect.name}")
    for statement in statements:
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        trigger_tables = (
            ("trg_asset_canonical_promotions_append_only", "asset_canonical_promotions"),
            ("trg_asset_version_media_append_only", "asset_version_media"),
            ("trg_asset_versions_append_only", "asset_versions"),
            ("trg_asset_promotions_versions_same_asset", "asset_canonical_promotions"),
            ("trg_asset_versions_parent_same_asset", "asset_versions"),
            ("trg_assets_canonical_same_asset", "assets"),
        )
        for trigger_name, table_name in trigger_tables:
            op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
        op.execute("DROP FUNCTION IF EXISTS enforce_asset_registry_append_only()")
        op.execute("DROP FUNCTION IF EXISTS enforce_asset_registry_consistency()")
        return
    for trigger_name in reversed(SQLITE_TRIGGER_NAMES):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
