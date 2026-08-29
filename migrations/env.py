from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context
from platform_shared import Settings
from production_domain.models import Base
from sqlalchemy import engine_from_config, pool

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
settings = Settings()
config.set_main_option("sqlalchemy.url", settings.database_url)
target_metadata = Base.metadata

#: Table-level CHECK constraints that migrations deliberately create only on
#: PostgreSQL. SQLite can add one only through a batch table rebuild, and the
#: rebuild trips over this schema's own integrity triggers (media_assets) and
#: the assetless recovery snapshot's dangling foreign keys (media_renditions,
#: created by 0035 with no media_assets to point at — SQLite never validated
#: it). The development SQLite schema still gets these constraints from ORM
#: metadata via create_all; only the migrated-SQLite comparison must not read
#: their absence as drift. On PostgreSQL — the only supported runtime — they
#: are created by the migrations and compared like everything else.
_SQLITE_ONLY_ABSENT_CHECKS = {
    ("media_assets", "ck_media_asset_verification_status"),
    ("media_renditions", "ck_media_rendition_lifecycle"),
}


def _include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    del reflected, compare_to
    if type_ == "check_constraint":
        table = getattr(getattr(obj, "table", None), "name", None)
        bind = context.get_bind() if not context.is_offline_mode() else None
        dialect = bind.dialect.name if bind is not None else ""
        if dialect == "sqlite" and (table, name) in _SQLITE_ONLY_ABSENT_CHECKS:
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
