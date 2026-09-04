from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from generation_gateway.scheduler import AccountScheduler
from pgvector.sqlalchemy import Vector
from platform_database import (
    REQUIRED_SCHEMA_REVISION,
    Database,
    SchemaRevisionMismatch,
)
from platform_shared import Settings
from production_domain.models import (
    ModelCapabilityProfile,
    ModelDefinition,
    ModelPricingProfile,
    ModelRoleBinding,
)
from sqlalchemy.dialects import postgresql, sqlite
from video_platform_api.container import build_container

ROOT = Path(__file__).resolve().parents[1]
LEGACY_REVISIONS = (
    (ROOT / "migrations/versions/0001_platform_v1.py", "platform_v1"),
    (ROOT / "migrations/versions/0002_director_platform.py", "platform_v2"),
)
SCHEMA_SNAPSHOTS = (
    ROOT / "migrations/schema_snapshots/platform_v1.py",
    ROOT / "migrations/schema_snapshots/platform_v2.py",
)


def _script_head(config: Config) -> str:
    """The single current head, as alembic itself reports it."""

    return ScriptDirectory.from_config(config).get_current_head()



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
                "metadata_json": {"project_id": "legacy-flow-project"},
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


def test_0071_moves_multimodal_embedding_to_official_voyage(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database_url = f"sqlite:///{tmp_path / 'voyage-migration.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "0070_creative_director_screenplay")

    database = Database(database_url)
    with database.session() as session:
        old = ModelDefinition(
            logical_name="voyage-multimodal-3.5-openrouter",
            provider="openrouter",
            provider_model_id="voyageai/voyage-multimodal-3.5",
            modality="multimodal_embedding",
            capabilities=["multimodal_embedding"],
            quality_tier="PREMIUM",
            cost_class="STANDARD",
            provider_trust_level="PRODUCTION",
            criticality_allowed=["STANDARD"],
            enabled=True,
            live_enabled=True,
            pricing_status="VERIFIED",
        )
        session.add(old)
        session.flush()
        session.add(
            ModelCapabilityProfile(
                model_definition_id=old.id,
                profile_version="legacy-openrouter-v1",
                supported_operations=["multimodal_embedding"],
                provider_metadata={"adapter": "openrouter"},
            )
        )
        session.add(
            ModelRoleBinding(
                role="MULTIMODAL_EMBEDDING",
                plan_tier="ALL",
                model_definition_id=old.id,
            )
        )
    database.engine.dispose()

    command.upgrade(config, "head")

    database = Database(database_url)
    with database.session() as session:
        old = session.scalar(
            sa.select(ModelDefinition).where(
                ModelDefinition.logical_name == "voyage-multimodal-3.5-openrouter"
            )
        )
        official = session.scalar(
            sa.select(ModelDefinition).where(
                ModelDefinition.logical_name == "voyage-multimodal-3.5-official"
            )
        )
        assert old is not None and (old.enabled, old.live_enabled) == (False, False)
        assert official is not None
        assert (official.provider, official.provider_model_id) == (
            "voyage",
            "voyage-multimodal-3.5",
        )
        assert (official.enabled, official.live_enabled) == (True, True)
        binding = session.scalar(
            sa.select(ModelRoleBinding).where(ModelRoleBinding.role == "MULTIMODAL_EMBEDDING")
        )
        assert binding is not None and binding.model_definition_id == official.id
        profile = session.get(ModelCapabilityProfile, official.id)
        assert profile is not None and profile.provider_metadata["adapter"] == "voyage"
        pricing = list(
            session.scalars(
                sa.select(ModelPricingProfile).where(
                    ModelPricingProfile.provider == "voyage",
                    ModelPricingProfile.provider_model_id == "voyage-multimodal-3.5",
                )
            )
        )
        # At head, not at 0071: `input_tokens` and `image_input` are 0071's own
        # two rows, and `video_input` is 0079's, added once the vendor's rule
        # for video ("each video frame is considered an image") was read off
        # the same page. All three are official list prices from that page.
        assert {row.input_mode for row in pricing} == {
            "input_tokens",
            "image_input",
            "video_input",
        }
        assert all("docs.voyageai.com" in row.source_url for row in pricing)
    database.engine.dispose()


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

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["projects", "generation_jobs", "cost_records"])
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["projects"].insert(),
            {
                "id": "cost-project",
                "name": "Cost migration",
                "title": "Cost migration",
                "description": "",
                "status": "ACTIVE",
                "default_aspect_ratio": "9:16",
                "default_provider": "google_flow",
                "default_language": "zh-CN",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["generation_jobs"].insert(),
            {
                "id": "cost-job",
                "project_id": "cost-project",
                "generation_type": "video",
                "provider": "google_flow",
                "model": "flow-veo-3.1",
                "status": "COMPLETED",
                "priority": 0,
                "request_json": {"duration": 5},
                "provider_request_json": {},
                "request_hash": "f" * 64,
                "policy": "TEXT_TO_VIDEO",
                "attempt_count": 0,
                "max_attempts": 3,
                "submission_state": "NOT_SENT",
                "safe_to_retry": True,
                "cost_estimate": 0.0,
                "actual_cost": 0.0,
                "created_at": now,
                "updated_at": now,
            },
        )
        common = {
            "project_id": "cost-project",
            "generation_job_id": "cost-job",
            "provider": "google_flow",
            "model": "flow-veo-3.1",
            "duration": 5.0,
            "resolution": "1080p",
            "credits": 120.0,
            "estimated_cost": 1.2,
            "actual_cost": 1.1,
            "retry_cost": 0.0,
            "accepted": False,
            "wasted": False,
            "created_at": now,
            "updated_at": now,
        }
        connection.execute(
            metadata.tables["cost_records"].insert(),
            [
                {"id": "duplicate-cost-a", **common},
                {"id": "duplicate-cost-b", **common},
            ],
        )
    engine.dispose()

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


def test_workspace_credit_lifecycle_migrates_populated_wallet_and_round_trips(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "populated-workspace-credit-wallet.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "0023_workspace_credit_wallet")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(
        engine,
        only=[
            "users",
            "workspaces",
            "projects",
            "generation_jobs",
            "workspace_credit_entries",
        ],
    )
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["users"].insert(),
            {
                "id": "credit-lifecycle-user",
                "email": "credit-lifecycle@example.com",
                "display_name": "Credit lifecycle",
                "password_hash": "not-used-by-migration",
                "status": "ACTIVE",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["workspaces"].insert(),
            {
                "id": "credit-lifecycle-workspace",
                "owner_user_id": "credit-lifecycle-user",
                "name": "Credit lifecycle",
                "status": "ACTIVE",
                "plan_tier": "FREE",
                "credit_balance": 38,
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["projects"].insert(),
            {
                "id": "credit-lifecycle-project",
                "workspace_id": "credit-lifecycle-workspace",
                "name": "Credit migration",
                "title": "Credit migration",
                "description": "",
                "status": "ACTIVE",
                "default_aspect_ratio": "9:16",
                "default_provider": "seedance",
                "default_language": "zh-CN",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["generation_jobs"].insert(),
            {
                "id": "credit-lifecycle-job",
                "project_id": "credit-lifecycle-project",
                "generation_type": "video",
                "provider": "seedance",
                "model": "seedance-1.5-pro",
                "status": "COMPLETED",
                "priority": 0,
                "request_json": {"duration": 5},
                "provider_request_json": {},
                "request_hash": "c" * 64,
                "policy": "TEXT_TO_VIDEO",
                "attempt_count": 1,
                "max_attempts": 3,
                "submission_state": "CONFIRMED",
                "safe_to_retry": False,
                "cost_estimate": 0.12,
                "actual_cost": 0.12,
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["workspace_credit_entries"].insert(),
            {
                "id": "credit-lifecycle-entry",
                "workspace_id": "credit-lifecycle-workspace",
                "project_id": "credit-lifecycle-project",
                "generation_job_id": "credit-lifecycle-job",
                "idempotency_key": "credit-lifecycle-key",
                "credits": 12,
                "balance_after": 38,
                "status": "CHARGED",
                "reason": "GENERATION_SUBMISSION",
                "metadata_json": {"pricing_version": "legacy-v1"},
                "created_at": now,
                "updated_at": now,
            },
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        entry = connection.execute(
            sa.text(
                "SELECT status, credits, settled_credits, refunded_credits, reason "
                "FROM workspace_credit_entries WHERE id = 'credit-lifecycle-entry'"
            )
        ).one()
        event = connection.execute(
            sa.text(
                "SELECT event_type, credits, balance_delta, balance_after, actor_type, "
                "generation_job_id FROM workspace_credit_events "
                "WHERE credit_entry_id = 'credit-lifecycle-entry'"
            )
        ).one()
        job = connection.execute(
            sa.text(
                "SELECT workspace_credit_required, quoted_credits FROM generation_jobs "
                "WHERE id = 'credit-lifecycle-job'"
            )
        ).one()
        foreign_key_violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    engine.dispose()

    assert entry == ("SETTLED", 12, 12, 0, "LEGACY_CHARGE_MIGRATED")
    assert event == ("LEGACY_SETTLED", 12, -12, 38, "MIGRATION", "credit-lifecycle-job")
    assert job == (True, 12)
    assert foreign_key_violations == []

    command.downgrade(config, "0023_workspace_credit_wallet")

    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    with engine.connect() as connection:
        downgraded_entry = connection.execute(
            sa.text("SELECT status, reason FROM workspace_credit_entries WHERE id = 'credit-lifecycle-entry'")
        ).one()
        downgraded_foreign_key_violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    assert "workspace_credit_events" not in inspector.get_table_names()
    assert "settled_credits" not in {
        str(column["name"]) for column in inspector.get_columns("workspace_credit_entries")
    }
    assert "workspace_credit_required" not in {
        str(column["name"]) for column in inspector.get_columns("generation_jobs")
    }
    engine.dispose()
    assert downgraded_entry == ("CHARGED", "GENERATION_SUBMISSION")
    assert downgraded_foreign_key_violations == []

    command.upgrade(config, "head")
    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        remigrated_entry = connection.execute(
            sa.text(
                "SELECT status, settled_credits FROM workspace_credit_entries "
                "WHERE id = 'credit-lifecycle-entry'"
            )
        ).one()
        remigrated_event_count = connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM workspace_credit_events "
                "WHERE credit_entry_id = 'credit-lifecycle-entry' "
                "AND event_type = 'LEGACY_SETTLED'"
            )
        )
        remigrated_foreign_key_violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    engine.dispose()
    assert remigrated_entry == ("SETTLED", 12)
    assert remigrated_event_count == 1
    assert remigrated_foreign_key_violations == []


def test_flow_project_affinity_migrates_populated_identity_and_round_trips(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "populated-flow-project-affinity.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "0024_workspace_credit_lifecycle")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(
        engine,
        only=["projects", "provider_accounts", "provider_projects", "generation_jobs"],
    )
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["projects"].insert(),
            {
                "id": "flow-affinity-project",
                "name": "Flow affinity",
                "title": "Flow affinity",
                "description": "",
                "status": "ACTIVE",
                "default_aspect_ratio": "9:16",
                "default_provider": "google_flow",
                "default_language": "zh-CN",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["provider_accounts"].insert(),
            {
                "id": "flow-affinity-account",
                "provider": "google_flow",
                "account_identifier": "flow-affinity@example.com",
                "tier": "PRO",
                "credits": 100,
                "status": "READY",
                "image_capacity": 1,
                "video_capacity": 1,
                "image_inflight": 0,
                "video_inflight": 1,
                "pending_jobs": 1,
                "supported_models": ["veo"],
                "metadata_json": {},
                "success_count": 0,
                "error_count": 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["provider_projects"].insert(),
            {
                "id": "flow-affinity-binding",
                "local_project_id": "flow-affinity-project",
                "provider": "google_flow",
                "provider_account_id": "flow-affinity-account",
                "provider_project_id": "flow-affinity-remote-project",
                "status": "READY",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["generation_jobs"].insert(),
            {
                "id": "flow-affinity-job",
                "project_id": "flow-affinity-project",
                "generation_type": "video",
                "provider": "google_flow",
                "model": "veo",
                "status": "SUBMITTED",
                "priority": 0,
                "request_json": {"prompt": "one action"},
                "provider_request_json": {},
                "request_hash": "a" * 64,
                "provider_job_id": "flow-affinity-remote-job",
                "account_id": "flow-affinity-account",
                "attempt_count": 1,
                "max_attempts": 3,
                "submission_state": "CONFIRMED",
                "safe_to_retry": False,
                "cost_estimate": 0.1,
                "actual_cost": 0.0,
                "created_at": now,
                "updated_at": now,
            },
        )
    engine.dispose()

    command.upgrade(config, "0025_flow_project_affinity")

    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    with engine.connect() as connection:
        binding = connection.execute(
            sa.text(
                "SELECT status, provider_project_id, version, ready_at "
                "FROM provider_projects WHERE id = 'flow-affinity-binding'"
            )
        ).one()
        job = connection.execute(
            sa.text(
                "SELECT account_id, provider_project_id, provider_job_id "
                "FROM generation_jobs WHERE id = 'flow-affinity-job'"
            )
        ).one()
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        foreign_key_violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    affinity_indexes = {str(index["name"]) for index in inspector.get_indexes("provider_projects")}
    assert binding[:3] == ("READY", "flow-affinity-remote-project", 1)
    assert binding.ready_at is not None
    assert job == (
        "flow-affinity-account",
        "flow-affinity-remote-project",
        "flow-affinity-remote-job",
    )
    assert revision == "0025_flow_project_affinity"
    assert "flow_migration_plans" in inspector.get_table_names()
    assert {
        "uq_flow_active_local_project",
        "uq_flow_remote_project_owner",
        "uq_non_flow_provider_project_account",
    }.issubset(affinity_indexes)
    assert foreign_key_violations == []
    engine.dispose()

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["projects", "provider_projects"])
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["projects"].insert(),
            {
                "id": "flow-affinity-other-project",
                "name": "Other Flow affinity",
                "title": "Other Flow affinity",
                "description": "",
                "status": "ACTIVE",
                "default_aspect_ratio": "9:16",
                "default_provider": "google_flow",
                "default_language": "zh-CN",
                "created_at": now,
                "updated_at": now,
            },
        )
    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                metadata.tables["provider_projects"].insert(),
                {
                    "id": "flow-affinity-stolen-historical-binding",
                    "local_project_id": "flow-affinity-other-project",
                    "provider": "google_flow",
                    "provider_account_id": "flow-affinity-account",
                    "provider_project_id": "flow-affinity-remote-project",
                    "status": "DISABLED",
                    "version": 1,
                    "created_at": now,
                    "updated_at": now,
                },
            )
    engine.dispose()

    command.downgrade(config, "0024_workspace_credit_lifecycle")

    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    binding_columns = {str(column["name"]) for column in inspector.get_columns("provider_projects")}
    job_columns = {str(column["name"]) for column in inspector.get_columns("generation_jobs")}
    legacy_unique_constraints = {
        str(constraint["name"]) for constraint in inspector.get_unique_constraints("provider_projects")
    }
    with engine.connect() as connection:
        downgraded_binding = connection.execute(
            sa.text(
                "SELECT status, provider_project_id FROM provider_projects WHERE id = 'flow-affinity-binding'"
            )
        ).one()
        downgraded_job = connection.execute(
            sa.text("SELECT account_id, provider_job_id FROM generation_jobs WHERE id = 'flow-affinity-job'")
        ).one()
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        foreign_key_violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    assert "flow_migration_plans" not in inspector.get_table_names()
    assert "version" not in binding_columns
    assert "provisioning_token" not in binding_columns
    assert "provider_project_id" not in job_columns
    assert "uq_provider_project" in legacy_unique_constraints
    assert downgraded_binding == ("READY", "flow-affinity-remote-project")
    assert downgraded_job == ("flow-affinity-account", "flow-affinity-remote-job")
    assert revision == "0024_workspace_credit_lifecycle"
    assert foreign_key_violations == []
    engine.dispose()

    command.upgrade(config, "0025_flow_project_affinity")
    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        remigrated_job = connection.execute(
            sa.text("SELECT provider_project_id FROM generation_jobs WHERE id = 'flow-affinity-job'")
        ).one()
        remigrated_foreign_key_violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    engine.dispose()
    assert remigrated_job == ("flow-affinity-remote-project",)
    assert remigrated_foreign_key_violations == []


def test_flow_affinity_upgrade_rejects_historical_remote_owner_duplicates(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "duplicate-historical-flow-owner.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "0024_workspace_credit_lifecycle")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["projects", "provider_accounts", "provider_projects"])
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["projects"].insert(),
            [
                {
                    "id": "historical-flow-owner-a",
                    "name": "Historical owner A",
                    "title": "Historical owner A",
                    "description": "",
                    "status": "ACTIVE",
                    "default_aspect_ratio": "9:16",
                    "default_provider": "google_flow",
                    "default_language": "zh-CN",
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "id": "historical-flow-owner-b",
                    "name": "Historical owner B",
                    "title": "Historical owner B",
                    "description": "",
                    "status": "ACTIVE",
                    "default_aspect_ratio": "9:16",
                    "default_provider": "google_flow",
                    "default_language": "zh-CN",
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )
        connection.execute(
            metadata.tables["provider_accounts"].insert(),
            {
                "id": "historical-flow-owner-account",
                "provider": "google_flow",
                "account_identifier": "historical-flow-owner@example.com",
                "tier": "PRO",
                "credits": 100,
                "status": "READY",
                "image_capacity": 1,
                "video_capacity": 1,
                "image_inflight": 0,
                "video_inflight": 0,
                "pending_jobs": 0,
                "supported_models": ["veo"],
                "metadata_json": {},
                "success_count": 0,
                "error_count": 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["provider_projects"].insert(),
            [
                {
                    "id": "historical-flow-binding-a",
                    "local_project_id": "historical-flow-owner-a",
                    "provider": "google_flow",
                    "provider_account_id": "historical-flow-owner-account",
                    "provider_project_id": "historical-shared-remote-project",
                    "status": "DISABLED",
                    "created_at": now,
                    "updated_at": now,
                },
                {
                    "id": "historical-flow-binding-b",
                    "local_project_id": "historical-flow-owner-b",
                    "provider": "google_flow",
                    "provider_account_id": "historical-flow-owner-account",
                    "provider_project_id": "historical-shared-remote-project",
                    "status": "FAILED",
                    "created_at": now,
                    "updated_at": now,
                },
            ],
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="one permanent ownership row"):
        command.upgrade(config, "0025_flow_project_affinity")

    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    with engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        binding_count = connection.scalar(sa.text("SELECT COUNT(*) FROM provider_projects"))
    engine.dispose()
    assert revision == "0024_workspace_credit_lifecycle"
    assert "version" not in {column["name"] for column in inspector.get_columns("provider_projects")}
    assert binding_count == 2


def test_workspace_credit_lifecycle_rejects_active_free_job_without_reservation(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "unreserved-active-free-job.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "0023_workspace_credit_wallet")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["users", "workspaces", "projects", "generation_jobs"])
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["users"].insert(),
            {
                "id": "unreserved-free-user",
                "email": "unreserved-free@example.com",
                "display_name": "Unreserved Free",
                "password_hash": "not-used-by-migration",
                "status": "ACTIVE",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["workspaces"].insert(),
            {
                "id": "unreserved-free-workspace",
                "owner_user_id": "unreserved-free-user",
                "name": "Unreserved Free",
                "status": "ACTIVE",
                "plan_tier": "FREE",
                "credit_balance": 50,
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["projects"].insert(),
            {
                "id": "unreserved-free-project",
                "workspace_id": "unreserved-free-workspace",
                "name": "Unreserved Free",
                "title": "Unreserved Free",
                "description": "",
                "status": "ACTIVE",
                "default_aspect_ratio": "9:16",
                "default_provider": "seedance",
                "default_language": "zh-CN",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["generation_jobs"].insert(),
            {
                "id": "unreserved-free-job",
                "project_id": "unreserved-free-project",
                "generation_type": "video",
                "provider": "seedance",
                "model": "seedance-1.5-pro",
                "status": "NEW",
                "priority": 0,
                "request_json": {"duration": 5},
                "provider_request_json": {},
                "request_hash": "u" * 64,
                "policy": "TEXT_TO_VIDEO",
                "attempt_count": 0,
                "max_attempts": 3,
                "submission_state": "NOT_SENT",
                "safe_to_retry": True,
                "cost_estimate": 0.12,
                "actual_cost": 0.0,
                "created_at": now,
                "updated_at": now,
            },
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="active FREE generation without a reservation"):
        command.upgrade(config, "head")

    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    with engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        job = connection.execute(
            sa.text("SELECT status, submission_state FROM generation_jobs WHERE id = 'unreserved-free-job'")
        ).one()
    assert revision == "0023_workspace_credit_wallet"
    assert "workspace_credit_required" not in {
        str(column["name"]) for column in inspector.get_columns("generation_jobs")
    }
    assert job == ("NEW", "NOT_SENT")
    engine.dispose()


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


def test_production_evidence_unknown_actual_cost_has_no_database_default(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "production-evidence-actual-cost-default.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))

    command.upgrade(config, "head")

    engine = sa.create_engine(database_url)
    inspector = sa.inspect(engine)
    for table_name in ("generation_jobs", "cost_records", "production_traces"):
        actual_cost = next(
            column for column in inspector.get_columns(table_name) if column["name"] == "actual_cost"
        )
        assert actual_cost["nullable"] is True
        assert actual_cost["default"] is None
    engine.dispose()


def test_production_evidence_downgrade_rejects_populated_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "production-evidence-downgrade-guard.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["live_canary_permits"])
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["live_canary_permits"].insert(),
            {
                "id": "migration-canary-permit",
                "provider": "offline-test",
                "model": "offline-test-model",
                "max_requests": 1,
                "max_cost_usd": 0.01,
                "used_requests": 0,
                "reserved_cost_usd": 0,
                "actual_cost_usd": 0,
                "expires_at": now,
                "purpose": "migration downgrade data-loss guard",
                "status": "ACTIVE",
                "version": 1,
                "created_at": now,
                "updated_at": now,
            },
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="would discard Phase III records"):
        command.downgrade(config, "0026_model_capability_registry")

    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        permit_count = connection.scalar(sa.text("SELECT COUNT(*) FROM live_canary_permits"))
    engine.dispose()
    assert revision == "0027_production_evidence_core"
    assert permit_count == 1


def test_persistent_character_state_supports_assetless_recovery_and_rejects_partial_schema(
    tmp_path,
    monkeypatch,
) -> None:
    recovery_path = tmp_path / "persistent-state-assetless-recovery.db"
    recovery_url = f"sqlite:///{recovery_path}"
    monkeypatch.setenv("DATABASE_URL", recovery_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))

    engine = sa.create_engine(recovery_url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)"
        )
        connection.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": "0027_production_evidence_core"},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(recovery_url)
    with engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    # Read the head rather than naming it: this assertion is about the recovery
    # database reaching the tip, not about which revision the tip happens to be,
    # and hard-coding it made every new migration edit an unrelated test.
    assert revision == _script_head(config)
    assert not {
        "character_state_versions",
        "character_state_deltas",
        "character_state_validations",
        "character_state_commits",
        "character_state_heads",
    }.intersection(sa.inspect(engine).get_table_names())
    engine.dispose()

    partial_path = tmp_path / "persistent-state-partial-schema.db"
    partial_url = f"sqlite:///{partial_path}"
    monkeypatch.setenv("DATABASE_URL", partial_url)
    partial_config = Config(str(ROOT / "alembic.ini"))
    partial_config.set_main_option("script_location", str(ROOT / "migrations"))
    engine = sa.create_engine(partial_url)
    core_tables = (
        "projects",
        "episodes",
        "scenes",
        "shots",
        "characters",
        "character_identity_versions",
        "generation_candidates",
        "timeline_states",
        "model_execution_records",
        "qa_results",
        "media_assets",
        "users",
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)"
        )
        connection.execute(
            sa.text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": "0027_production_evidence_core"},
        )
        for table_name in core_tables:
            connection.exec_driver_sql(f'CREATE TABLE "{table_name}" (id VARCHAR(36) PRIMARY KEY)')
        connection.exec_driver_sql("CREATE TABLE character_state_versions (id VARCHAR(36) PRIMARY KEY)")
    engine.dispose()

    with pytest.raises(RuntimeError, match="partial pre-existing tables"):
        command.upgrade(partial_config, "head")
    engine = sa.create_engine(partial_url)
    with engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    engine.dispose()
    assert revision == "0027_production_evidence_core"


def test_persistent_character_state_downgrade_rejects_audit_records(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "persistent-state-downgrade-guard.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "head")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(
        engine,
        only=[
            "projects",
            "media_assets",
            "characters",
            "character_identity_versions",
            "character_state_versions",
        ],
    )
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["projects"].insert(),
            {
                "id": "persistent-state-project",
                "name": "Persistent state",
                "title": "Persistent state",
                "description": "",
                "status": "ACTIVE",
                "default_aspect_ratio": "9:16",
                "default_provider": "seedance",
                "default_language": "zh-CN",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["media_assets"].insert(),
            {
                "id": "persistent-state-master",
                "project_id": "persistent-state-project",
                "asset_type": "CHARACTER_MASTER",
                "sha256": "a" * 64,
                "lineage_key": "shared",
                "storage_key": "persistent-state/master.png",
                "mime_type": "image/png",
                "size_bytes": 1,
                "metadata_json": {},
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["characters"].insert(),
            {
                "id": "persistent-state-character",
                "project_id": "persistent-state-project",
                "name": "Mira Okonkwo",
                "description": "",
                "canonical_facts": {},
                "status": "DRAFT",
                "current_identity_version_id": None,
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["character_identity_versions"].insert(),
            {
                "id": "persistent-state-identity-v1",
                "character_id": "persistent-state-character",
                "version": 1,
                "master_asset_id": "persistent-state-master",
                "hair_signature": "short braids with silver highlights",
                "costume_signature": "charcoal field jacket",
                "provider_bindings_json": {},
                "status": "LOCKED",
                "locked_at": now,
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["character_state_versions"].insert(),
            {
                "id": "persistent-state-v1",
                "project_id": "persistent-state-project",
                "character_id": "persistent-state-character",
                "timeline_scope_key": "main",
                "version": 1,
                "previous_state_version_id": None,
                "identity_version_id": "persistent-state-identity-v1",
                "source_shot_id": None,
                "source_candidate_id": None,
                "state_schema_version": "character-state-v1",
                "narrative_state_json": {"props": {"flare": {"state": "unlit"}}},
                "identity_fingerprint": "b" * 64,
                "previous_state_hash": None,
                "state_hash": "c" * 64,
                "created_at": now,
                "updated_at": now,
            },
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="would discard audit records"):
        command.downgrade(config, "0027_production_evidence_core")

    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        state_count = connection.scalar(sa.text("SELECT COUNT(*) FROM character_state_versions"))
    engine.dispose()
    assert revision == "0028_persistent_character_state"
    assert state_count == 1


def test_production_evidence_populated_legacy_costs_round_trip(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "production-evidence-populated-round-trip.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "0024_workspace_credit_lifecycle")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(
        engine,
        only=["projects", "generation_jobs", "cost_records", "production_traces"],
    )
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["projects"].insert(),
            {
                "id": "production-evidence-round-trip-project",
                "name": "Production evidence round trip",
                "title": "Production evidence round trip",
                "description": "",
                "status": "ACTIVE",
                "default_aspect_ratio": "9:16",
                "default_provider": "seedance",
                "default_language": "zh-CN",
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["generation_jobs"].insert(),
            {
                "id": "production-evidence-round-trip-job",
                "project_id": "production-evidence-round-trip-project",
                "generation_type": "video",
                "provider": "seedance",
                "model": "seedance-test",
                "status": "COMPLETED",
                "priority": 0,
                "request_json": {"prompt": "round trip"},
                "provider_request_json": {},
                "request_hash": "p" * 64,
                "policy": "TEXT_TO_VIDEO",
                "attempt_count": 1,
                "max_attempts": 3,
                "submission_state": "CONFIRMED",
                "safe_to_retry": False,
                "cost_estimate": 0.25,
                "actual_cost": 0.20,
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["cost_records"].insert(),
            {
                "id": "production-evidence-round-trip-cost",
                "project_id": "production-evidence-round-trip-project",
                "generation_job_id": "production-evidence-round-trip-job",
                "provider": "seedance",
                "model": "seedance-test",
                "duration": 5,
                "resolution": "1080p",
                "credits": 20,
                "estimated_cost": 0.25,
                "actual_cost": 0.20,
                "retry_cost": 0,
                "accepted": False,
                "wasted": False,
                "created_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            metadata.tables["production_traces"].insert(),
            {
                "id": "production-evidence-round-trip-trace",
                "trace_id": "production-evidence-round-trip-trace-id",
                "mode": "PRODUCTION",
                "project_id": "production-evidence-round-trip-project",
                "generation_job_id": "production-evidence-round-trip-job",
                "provider": "seedance",
                "model_id": "seedance-test",
                "prompt_version": "v1",
                "context_asset_ids": [],
                "retrieved_memory_ids": [],
                "router_scores_json": {},
                "generation_latency": 1.0,
                "estimated_cost": 0.25,
                "actual_cost": 0.20,
                "evaluation_json": {},
                "retry_json": {},
                "created_at": now,
                "updated_at": now,
            },
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        upgraded_costs = connection.execute(
            sa.text(
                "SELECT j.actual_cost, c.actual_cost, t.actual_cost "
                "FROM generation_jobs j "
                "JOIN cost_records c ON c.generation_job_id = j.id "
                "JOIN production_traces t ON t.generation_job_id = j.id "
                "WHERE j.id = 'production-evidence-round-trip-job'"
            )
        ).one()
    engine.dispose()
    assert upgraded_costs == (0.20, 0.20, 0.20)

    command.downgrade(config, "0024_workspace_credit_lifecycle")
    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        downgraded_costs = connection.execute(
            sa.text(
                "SELECT j.actual_cost, c.actual_cost, t.actual_cost "
                "FROM generation_jobs j "
                "JOIN cost_records c ON c.generation_job_id = j.id "
                "JOIN production_traces t ON t.generation_job_id = j.id "
                "WHERE j.id = 'production-evidence-round-trip-job'"
            )
        ).one()
        foreign_key_violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    engine.dispose()
    assert revision == "0024_workspace_credit_lifecycle"
    assert downgraded_costs == (0.20, 0.20, 0.20)
    assert foreign_key_violations == []

    command.upgrade(config, "head")


def test_model_capability_downgrade_only_drops_deterministic_backfill(
    tmp_path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "model-capability-lossless-downgrade.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "0025_flow_project_affinity")

    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    metadata.reflect(engine, only=["model_definitions"])
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["model_definitions"].insert(),
            {
                "id": "capability-downgrade-model",
                "logical_name": "kling-3-standard-openrouter",
                "provider": "openrouter",
                "provider_model_id": "kling-test",
                "modality": "video",
                "capabilities": ["video_generation"],
                "quality_tier": "standard",
                "cost_class": "medium",
                "provider_trust_level": "official",
                "criticality_allowed": ["low", "medium"],
                "enabled": True,
                "live_enabled": False,
                "max_duration": 10,
                "supported_aspect_ratios": ["9:16"],
                "metadata_json": {},
                "created_at": now,
                "updated_at": now,
            },
        )
    engine.dispose()

    command.upgrade(config, "0026_model_capability_registry")
    command.downgrade(config, "0025_flow_project_affinity")
    command.upgrade(config, "0026_model_capability_registry")

    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE model_capability_profiles SET physics_prior = 0.123 "
                "WHERE model_definition_id = 'capability-downgrade-model'"
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="operator-edited profile"):
        command.downgrade(config, "0025_flow_project_affinity")

    engine = sa.create_engine(database_url)
    with engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        physics_prior = connection.scalar(
            sa.text(
                "SELECT physics_prior FROM model_capability_profiles "
                "WHERE model_definition_id = 'capability-downgrade-model'"
            )
        )
    engine.dispose()
    assert revision == "0026_model_capability_registry"
    assert physics_prior == pytest.approx(0.123)


def test_prompt_refiner_rebind_moves_only_the_paid_tier_binding(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`0065` repoints the ALL-tier refiner, leaves FREE alone, and round-trips.

    The binding is moved by its current target rather than by id, so a database
    an administrator has already repointed is left as it is.
    """

    database_url = f"sqlite:///{tmp_path / 'refiner-rebind.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "0064_free_tier_defaults")

    now = datetime.now(UTC)
    definition = sa.text(
        "insert into model_definitions ("
        " id, logical_name, provider, provider_model_id, modality, capabilities,"
        " quality_tier, cost_class, provider_trust_level, criticality_allowed, enabled,"
        " live_enabled, supported_aspect_ratios, metadata_json, created_at, updated_at"
        ") values ("
        " :id, :logical, :provider, :model, 'text', '[]',"
        " 'STANDARD', 'LOW', :trust, '[]', 1,"
        " 1, '[]', '{}', :now, :now)"
    )
    binding = sa.text(
        "insert into model_role_bindings ("
        " id, role, plan_tier, model_definition_id, binding_kind, priority, enabled,"
        " metadata_json, created_at, updated_at"
        ") values (:id, 'PROMPT_REFINER_LOW_COST', :tier, :definition, 'PRIMARY', 0, 1,"
        " '{}', :now, :now)"
    )
    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            definition.bindparams(
                id="def-runapi", logical="runapi-prompt-refiner-edge", provider="runapi",
                model="gpt-5.6-luna", trust="EDGE", now=now,
            )
        )
        connection.execute(
            definition.bindparams(
                id="def-openrouter", logical="claude-sonnet-5-openrouter", provider="openrouter",
                model="anthropic/claude-sonnet-5", trust="PRODUCTION", now=now,
            )
        )
        connection.execute(binding.bindparams(id="bind-all", tier="ALL", definition="def-runapi", now=now))
        connection.execute(binding.bindparams(id="bind-free", tier="FREE", definition="def-runapi", now=now))

    def _targets() -> dict[str, str]:
        with engine.connect() as connection:
            return dict(
                connection.execute(
                    sa.text("select id, model_definition_id from model_role_bindings")
                ).all()
            )

    command.upgrade(config, "head")
    after = _targets()
    assert after["bind-all"] == "def-openrouter"
    assert after["bind-free"] == "def-runapi", "FREE keeps its own binding"

    command.downgrade(config, "0064_free_tier_defaults")
    restored = _targets()
    engine.dispose()
    assert restored["bind-all"] == "def-runapi"
    assert restored["bind-free"] == "def-runapi"


def test_every_postgres_trigger_declares_its_sqlstate() -> None:
    """A guard with no ERRCODE raises P0001, which is not an IntegrityError.

    Eight of these were missing, so the same invariant surfaced as
    `IntegrityError` on SQLite and `ProgrammingError` on PostgreSQL, and any
    `except IntegrityError` around them caught only on the development engine.
    """

    import re

    source = (ROOT / "packages/domain/production_domain/models.py").read_text("utf-8")
    silent = [
        match.group(1)[:60]
        for match in re.finditer(r"RAISE EXCEPTION\s+'((?:[^']|'')*)'(.*?);", source, re.S)
        if "ERRCODE" not in match.group(2)
    ]
    assert not silent, "plpgsql guards with no SQLSTATE: " + "; ".join(silent)


def test_every_revision_id_fits_the_version_column() -> None:
    """A too-long revision id only fails on PostgreSQL, and only at stamp time.

    SQLite ignores VARCHAR lengths, so an over-long id is invisible on the
    development engine and breaks every PostgreSQL database the moment it
    becomes head.
    """

    import re

    over_long = {}
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        match = re.search(r'^revision: str = "([^"]+)"', path.read_text("utf-8"), re.M)
        if match and len(match.group(1)) > Database.VERSION_NUM_LENGTH:
            over_long[match.group(1)] = len(match.group(1))
    assert not over_long, f"revision ids longer than the version column: {over_long}"


def test_the_required_schema_revision_is_the_alembic_head() -> None:
    """The application declares the schema it needs; alembic decides what head is.

    Bumping one without the other is the only way this constant can lie, and it
    would lie at deploy time on a running database rather than here.
    """

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert REQUIRED_SCHEMA_REVISION == _script_head(config)


def test_flow_remote_owner_index_reconciles_a_drifted_database(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A restored production volume may carry the pre-owner partial index."""

    database_url = f"sqlite:///{tmp_path / 'flow-index-drift.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "0059_timeline_branches")

    engine = sa.create_engine(database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_flow_remote_project_owner")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_flow_active_remote_project ON provider_projects "
            "(provider, provider_project_id) WHERE provider = 'google_flow' "
            "AND provider_project_id IS NOT NULL "
            "AND status IN ('PROVISIONING', 'READY', 'DEGRADED', "
            "'MIGRATION_REQUIRED', 'MIGRATING')"
        )
    engine.dispose()

    command.upgrade(config, "head")

    engine = sa.create_engine(database_url)
    indexes = {str(index["name"]) for index in sa.inspect(engine).get_indexes("provider_projects")}
    with engine.connect() as connection:
        revision = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
    engine.dispose()
    # Against the constant rather than a literal: this assertion means "the
    # upgrade reached head", and head moves with every migration. A pinned
    # literal here fails on the next one for a reason that has nothing to do
    # with what this test is checking. `REQUIRED_SCHEMA_REVISION` is already
    # proven equal to head above.
    assert revision == REQUIRED_SCHEMA_REVISION
    assert "uq_flow_remote_project_owner" in indexes
    assert "uq_flow_active_remote_project" not in indexes


def test_production_refuses_a_non_postgresql_database(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Under pysqlite a savepoint does not roll back; seven call sites rely on it."""

    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'platform.db'}",
        storage_root=tmp_path / "media",
        deployment_environment="production",
        auth_required=True,
        platform_api_key="0123456789abcdef0123456789abcdef0123456789",
    )
    with pytest.raises(RuntimeError, match="production requires a PostgreSQL"):
        build_container(settings)


def test_startup_refuses_a_database_that_is_not_at_the_required_revision(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`create_all()` no longer runs at startup, so an unmigrated database is loud.

    The defect this replaces was silent: startup created whatever tables were
    missing from ORM metadata, never added a column to a table that already
    existed, and never advanced the stamp — leaving a hybrid schema that no
    migration could repair.
    """

    database = Database(f"sqlite:///{tmp_path / 'unmigrated.db'}")
    with pytest.raises(SchemaRevisionMismatch, match="no revision"):
        database.require_schema_revision()

    database.create_all()
    with pytest.raises(SchemaRevisionMismatch, match="no revision"):
        database.require_schema_revision()

    database.stamp("0020_provider_media_upload_claim")
    with pytest.raises(SchemaRevisionMismatch, match="0020_provider_media_upload_claim"):
        database.require_schema_revision()

    database.stamp(REQUIRED_SCHEMA_REVISION)
    database.require_schema_revision()
