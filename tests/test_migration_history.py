from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from generation_gateway.scheduler import AccountScheduler
from pgvector.sqlalchemy import Vector
from platform_database import Database
from production_domain.models import CostRecord, GenerationJob, JobStatus, Project
from sqlalchemy.dialects import postgresql, sqlite

ROOT = Path(__file__).resolve().parents[1]
LEGACY_REVISIONS = (
    (ROOT / "migrations/versions/0001_platform_v1.py", "platform_v1"),
    (ROOT / "migrations/versions/0002_director_platform.py", "platform_v2"),
)
SCHEMA_SNAPSHOTS = (
    ROOT / "migrations/schema_snapshots/platform_v1.py",
    ROOT / "migrations/schema_snapshots/platform_v2.py",
)


def _legacy_capacity_database(tmp_path, monkeypatch, filename):  # type: ignore[no-untyped-def]
    database_path = tmp_path / filename
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "0012_project_scoped_idempotency")
    return database_url, config


def _seed_legacy_capacity(
    database_url: str,
    *,
    jobs: list[dict[str, object]],
    inflight: int,
) -> None:
    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(
        engine,
        only=["projects", "provider_accounts", "browser_workers", "generation_jobs"],
    )
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["projects"].insert(),
            {
                "id": "legacy-project",
                "title": "Legacy capacity",
                "description": "",
                "status": "ACTIVE",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["provider_accounts"].insert(),
            {
                "id": "legacy-account",
                "provider": "google_flow",
                "account_identifier": "legacy@example.com",
                "tier": "PRO",
                "credits": 100,
                "status": "BUSY",
                "image_capacity": 1,
                "video_capacity": 4,
                "image_inflight": 0,
                "video_inflight": inflight,
                "pending_jobs": inflight,
                "worker_id": "legacy-worker",
                "supported_models": ["veo"],
                "metadata_json": {},
                "success_count": 0,
                "error_count": 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["browser_workers"].insert(),
            {
                "id": "legacy-worker",
                "provider": "google_flow",
                "account_id": "legacy-account",
                "connection_id": "legacy-connection",
                "status": "BUSY",
                "capabilities": ["video"],
                "current_jobs": inflight,
                "max_jobs": 4,
                "last_heartbeat": now,
                "metadata_json": {},
                "created_at": now,
                "updated_at": now,
            },
        )
        base_job: dict[str, object] = {
            "project_id": "legacy-project",
            "generation_type": "video",
            "provider": "google_flow",
            "model": "veo",
            "priority": 0,
            "request_json": {},
            "request_hash": "0" * 64,
            "provider_job_id": None,
            "account_id": "legacy-account",
            "worker_id": "legacy-worker",
            "attempt_count": 1,
            "max_attempts": 3,
            "safe_to_retry": False,
            "created_at": now,
            "updated_at": now,
        }
        connection.execute(
            metadata.tables["generation_jobs"].insert(),
            [{**base_job, **job} for job in jobs],
        )
    engine.dispose()


def test_legacy_revisions_use_frozen_schema_without_pgvector_ddl() -> None:
    for revision, snapshot_name in LEGACY_REVISIONS:
        source = revision.read_text(encoding="utf-8")
        assert "production_domain.models" not in source
        assert re.search(r"\bBase\b", source) is None
        assert "CREATE EXTENSION" not in source
        assert f"migrations.schema_snapshots.{snapshot_name}" in source

    for snapshot in SCHEMA_SNAPSHOTS:
        source = snapshot.read_text(encoding="utf-8")
        assert "from pgvector" not in source
        assert "Vector(" not in source


def test_timeline_embedding_type_is_vector_on_postgres_and_json_on_sqlite() -> None:
    vector_type = Vector(16).with_variant(sa.JSON(), "sqlite")
    assert str(vector_type.compile(dialect=postgresql.dialect())).lower() == "vector(16)"
    assert str(vector_type.compile(dialect=sqlite.dialect())).lower() == "json"
    migration = (ROOT / "migrations/versions/0018_postgres_timeline_vectors.py").read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in migration
    assert "TYPE vector(16)" in migration
    for column_name in (
        "semantic_embedding",
        "visual_embedding",
        "camera_embedding",
        "character_track_embedding",
    ):
        assert f'"{column_name}"' in migration


def test_revision_storage_is_widened_before_long_revision_ids() -> None:
    migration = (ROOT / "migrations/versions/0012_project_scoped_idempotency.py").read_text(encoding="utf-8")
    assert "ALTER COLUMN version_num TYPE VARCHAR({length})" in migration
    assert "_resize_alembic_version(255)" in migration


def test_explicit_database_identifiers_fit_postgresql_limit() -> None:
    sources = [
        *(ROOT / "migrations/versions").glob("*.py"),
        ROOT / "packages/domain/production_domain/models.py",
    ]
    for source_path in sources:
        source = source_path.read_text(encoding="utf-8")
        identifiers = re.findall(r'["\']((?:fk|uq|ix|ck)_[A-Za-z0-9_]+)["\']', source)
        assert all(len(identifier) <= 63 for identifier in identifiers), source_path


def test_empty_sqlite_upgrades_to_head_and_matches_metadata(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database_path = tmp_path / "migration-reproducibility.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))

    command.upgrade(config, "head")
    command.check(config)


def test_cost_record_migration_deduplicates_exact_legacy_job_rows(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database_path = tmp_path / "legacy-duplicate-cost.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "0014_worker_scoped_credentials")

    database = Database(database_url)
    with database.session() as session:
        project = Project(id="cost-project", title="Cost migration")
        session.add(project)
        session.flush()
        job = GenerationJob(
            id="cost-job",
            project_id=project.id,
            generation_type="video",
            provider="google_flow",
            model="flow-veo-3.1",
            status=JobStatus.COMPLETED.value,
            request_json={"duration": 5},
            request_hash="f" * 64,
        )
        session.add(job)
        session.flush()
        common = {
            "project_id": project.id,
            "generation_job_id": job.id,
            "provider": job.provider,
            "model": job.model,
            "duration": 5.0,
            "resolution": "1080p",
            "credits": 120.0,
            "estimated_cost": 1.2,
            "actual_cost": 1.1,
            "retry_cost": 0.0,
        }
        session.add_all(
            [
                CostRecord(id="duplicate-cost-a", **common),
                CostRecord(id="duplicate-cost-b", **common),
            ]
        )

    command.upgrade(config, "head")
    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        count = connection.scalar(
            sa.text("SELECT COUNT(*) FROM cost_records WHERE generation_job_id = 'cost-job'")
        )
        index = next(
            item
            for item in sa.inspect(connection).get_indexes("cost_records")
            if item["name"] == "ix_cost_records_generation_job_id"
        )
    engine.dispose()
    assert count == 1
    assert index["unique"] == 1


def test_reservation_ownership_migration_repairs_legacy_cancelled_capacity(
    tmp_path,
    monkeypatch,
) -> None:
    database_url, config = _legacy_capacity_database(
        tmp_path,
        monkeypatch,
        "legacy-cancelled-capacity.db",
    )
    _seed_legacy_capacity(
        database_url,
        inflight=1,
        jobs=[
            {
                "id": "legacy-cancelled-job",
                "status": "CANCELLED",
                "provider_job_id": "remote-cancelled",
                "submission_state": "CONFIRMED",
            }
        ],
    )

    command.upgrade(config, "head")

    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        account = connection.execute(
            sa.text(
                "SELECT video_inflight, pending_jobs, status FROM provider_accounts "
                "WHERE id = 'legacy-account'"
            )
        ).one()
        worker = connection.execute(
            sa.text("SELECT current_jobs, status FROM browser_workers WHERE id = 'legacy-worker'")
        ).one()
        released_at = connection.scalar(
            sa.text("SELECT reservation_released_at FROM generation_jobs WHERE id = 'legacy-cancelled-job'")
        )
    engine.dispose()
    assert account == (0, 0, "READY")
    assert worker == (0, "READY")
    assert released_at is not None


def test_reservation_ownership_migration_does_not_release_legacy_retry_twice(
    tmp_path,
    monkeypatch,
) -> None:
    database_url, config = _legacy_capacity_database(
        tmp_path,
        monkeypatch,
        "legacy-retry-capacity.db",
    )
    _seed_legacy_capacity(
        database_url,
        inflight=1,
        jobs=[
            {
                "id": "active-submitted-job",
                "status": "SUBMITTED",
                "provider_job_id": "active-remote-job",
                "submission_state": "CONFIRMED",
            },
            {
                "id": "already-released-retry",
                "status": "RETRY_WAIT",
                "submission_state": "NOT_SENT",
                "safe_to_retry": True,
            },
        ],
    )

    command.upgrade(config, "head")

    scheduler = AccountScheduler(Database(database_url))
    assert scheduler.release_job("already-released-retry", success=None) is False
    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        account = connection.execute(
            sa.text(
                "SELECT video_inflight, pending_jobs, status FROM provider_accounts "
                "WHERE id = 'legacy-account'"
            )
        ).one()
        worker = connection.execute(
            sa.text("SELECT current_jobs, status FROM browser_workers WHERE id = 'legacy-worker'")
        ).one()
        ownership = connection.execute(
            sa.text("SELECT id, reservation_released_at FROM generation_jobs ORDER BY id")
        ).all()
    engine.dispose()
    assert account == (1, 1, "BUSY")
    assert worker == (1, "READY")
    assert ownership[0] == ("active-submitted-job", None)
    assert ownership[1][0] == "already-released-retry"
    assert ownership[1][1] is not None
