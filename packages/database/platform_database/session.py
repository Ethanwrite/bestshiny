from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from production_domain.models import Base
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

# The schema this build of the application requires. Alembic is the only
# authority that may create or alter a database; this constant is what the
# application checks it against at startup, so a binary can never run against a
# schema it was not written for. Bump it in the same commit as the migration
# that moves head — a test asserts the two agree, so forgetting is a gate
# failure rather than a runtime surprise.
REQUIRED_SCHEMA_REVISION = "0056_character_evidence_submissions"


class SchemaRevisionMismatch(RuntimeError):
    """The database is not at the revision this build of the application needs."""


class Database:
    def __init__(self, url: str):
        if url.startswith("sqlite:///"):
            db_path = url.removeprefix("sqlite:///")
            if db_path and db_path != ":memory:":
                Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def _sqlite_foreign_keys(dbapi_connection, _record):  # type: ignore[no-untyped-def]
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.Session = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Build the schema from ORM metadata.

        Not a deployment path. Alembic owns production and development schemas;
        running this against either gives a database two authorities, which is
        how a stamp and a set of tables drift apart until no migration can
        repair them. It exists for throwaway databases — a per-test tmp file, a
        scratch simulation — that cannot afford to replay every revision.
        """

        Base.metadata.create_all(self.engine)

    def current_revision(self) -> str | None:
        """The alembic revision this database is stamped at, if any."""

        with self.engine.connect() as connection:
            if not self.engine.dialect.has_table(connection, "alembic_version"):
                return None
            return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()

    # Alembic's own default is VARCHAR(32), but this project already carries a
    # 37-character revision id (`0013_generation_reservation_ownership`), and
    # PostgreSQL is the only engine that enforces the limit — SQLite ignores
    # VARCHAR lengths entirely, which is why a too-narrow column was invisible
    # here for thirteen revisions. A test asserts every revision id fits.
    VERSION_NUM_LENGTH = 255

    def stamp(self, revision: str) -> None:
        """Record a revision without running migrations, as `alembic stamp` does."""

        if len(revision) > self.VERSION_NUM_LENGTH:
            raise ValueError(
                f"revision id {revision!r} exceeds {self.VERSION_NUM_LENGTH} characters"
            )
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS alembic_version "
                    f"(version_num VARCHAR({self.VERSION_NUM_LENGTH}) NOT NULL, "
                    "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                )
            )
            connection.execute(text("DELETE FROM alembic_version"))
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": revision},
            )

    def create_all_and_stamp(self, revision: str = REQUIRED_SCHEMA_REVISION) -> None:
        """Throwaway-database bootstrap: ORM metadata, then the matching stamp.

        The stamp is not decoration. Without it such a database answers
        "no revision" to the startup check, and the check would have to carry an
        exception for tests — an exception that would also excuse a real
        unmigrated deployment.
        """

        self.create_all()
        self.stamp(revision)

    def require_schema_revision(self, revision: str = REQUIRED_SCHEMA_REVISION) -> None:
        current = self.current_revision()
        if current == revision:
            return
        raise SchemaRevisionMismatch(
            f"database schema is at {current or 'no revision'}, this build requires {revision}; "
            "run `alembic upgrade head` before starting the application"
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        db = self.Session()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
