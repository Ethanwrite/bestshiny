from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from platform_database import Database
from production_domain.models import FeatureFlag, new_id, utcnow
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


@dataclass(frozen=True)
class FeatureFlagDefaults:
    voyage_memory: bool = False
    auto_evaluation: bool = False
    adaptive_router: bool = False
    auto_retry: bool = False


class FeatureFlagService:
    """Database overrides on top of safe environment defaults."""

    known_flags = frozenset(FeatureFlagDefaults.__dataclass_fields__)

    def __init__(self, database: Database, defaults: FeatureFlagDefaults):
        self.database = database
        self.defaults = defaults

    @staticmethod
    def _scope_key(project_id: str | None) -> str:
        return f"project:{project_id}" if project_id else "global"

    def enabled(self, name: str, *, project_id: str | None = None) -> bool:
        if name not in self.known_flags:
            raise KeyError(f"unknown feature flag: {name}")
        with self.database.session() as session:
            if project_id:
                override = session.scalar(
                    select(FeatureFlag).where(
                        FeatureFlag.name == name,
                        FeatureFlag.scope_key == self._scope_key(project_id),
                    )
                )
                if override:
                    return override.enabled
            global_override = session.scalar(
                select(FeatureFlag).where(
                    FeatureFlag.name == name,
                    FeatureFlag.scope_key == self._scope_key(None),
                )
            )
            if global_override:
                return global_override.enabled
        return bool(getattr(self.defaults, name))

    def set(self, name: str, enabled: bool, *, project_id: str | None = None) -> FeatureFlag:
        if name not in self.known_flags:
            raise KeyError(f"unknown feature flag: {name}")
        scope_key = self._scope_key(project_id)
        with self.database.session() as session:
            now = utcnow()
            values = {
                "id": new_id(),
                "name": name,
                "project_id": project_id,
                "scope_key": scope_key,
                "enabled": enabled,
                "metadata_json": {},
                "created_at": now,
                "updated_at": now,
            }
            dialect = session.get_bind().dialect.name
            insert_statement: Any
            if dialect == "postgresql":
                insert_statement = postgresql_insert(FeatureFlag).values(**values)
            elif dialect == "sqlite":
                insert_statement = sqlite_insert(FeatureFlag).values(**values)
            else:  # pragma: no cover - the platform supports SQLite and PostgreSQL.
                raise RuntimeError(f"unsupported database dialect for feature flag upsert: {dialect}")
            statement = insert_statement.on_conflict_do_update(
                index_elements=["name", "scope_key"],
                set_={
                    "project_id": project_id,
                    "enabled": enabled,
                    "updated_at": now,
                },
            )
            session.execute(statement)
            value = session.scalar(
                select(FeatureFlag).where(
                    FeatureFlag.name == name,
                    FeatureFlag.scope_key == scope_key,
                )
            )
            if value is None:  # pragma: no cover - defensive guard for a failed database upsert.
                raise RuntimeError("feature flag upsert did not return a persisted value")
            session.flush()
            return value

    def snapshot(self, *, project_id: str | None = None) -> dict[str, bool]:
        return {name: self.enabled(name, project_id=project_id) for name in sorted(self.known_flags)}
