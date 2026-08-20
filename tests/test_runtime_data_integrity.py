from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from asset_registry_core import AssetRegistry
from memory_core import MemoryLayer, MultimodalContent, ShotMemoryInput
from production_domain.models import (
    Episode,
    FeatureFlag,
    GenerationJob,
    ModelMetric,
    Project,
    Scene,
    Shot,
)
from sqlalchemy import func, select


def _project_graph(container, title: str):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        project = Project(title=title)
        session.add(project)
        session.flush()
        episode = Episode(project_id=project.id, title="Pilot", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Room")
        session.add(scene)
        session.flush()
        shot = Shot(scene_id=scene.id, sequence=1, prompt="Turn once")
        session.add(shot)
        session.flush()
        job = GenerationJob(
            project_id=project.id,
            shot_id=shot.id,
            generation_type="VIDEO",
            provider="test",
            model="test-video",
            request_json={},
            request_hash=f"hash-{project.id}",
        )
        session.add(job)
        session.flush()
        return project, scene, shot, job


def test_feature_flag_upsert_is_unique_for_global_and_project_scopes(container, project):
    barrier = threading.Barrier(4)

    def write(enabled: bool) -> str:
        barrier.wait()
        return container.feature_flags.set("voyage_memory", enabled).id

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(write, [True, False, True, False]))

    assert len(set(ids)) == 1
    project_override = container.feature_flags.set("voyage_memory", True, project_id=project.id)
    updated_override = container.feature_flags.set("voyage_memory", False, project_id=project.id)
    assert project_override.id == updated_override.id
    assert project_override.id != ids[0]
    with container.database.session() as session:
        values = list(
            session.scalars(
                select(FeatureFlag).where(FeatureFlag.name == "voyage_memory").order_by(FeatureFlag.scope_key)
            )
        )
    assert [value.scope_key for value in values] == ["global", f"project:{project.id}"]


def test_record_once_is_atomic_and_idempotent_under_concurrency(container):
    project, _scene, shot, job = _project_graph(container, "Metrics")
    barrier = threading.Barrier(4)

    def write() -> str:
        barrier.wait()
        return container.model_metrics.record_once(
            provider="test",
            model_id="test-video",
            metric="generation_success",
            generation_job_id=job.id,
            project_id=project.id,
            shot_id=shot.id,
        ).id

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(lambda _index: write(), range(4)))

    assert len(set(ids)) == 1
    with container.database.session() as session:
        count = session.scalar(
            select(func.count(ModelMetric.id)).where(
                ModelMetric.generation_job_id == job.id,
                ModelMetric.metric_name == "generation_success",
            )
        )
    assert count == 1


def test_memory_index_rejects_cross_project_scene_shot_and_asset_version(container, project):
    foreign_project, scene, shot, _job = _project_graph(container, "Foreign")
    registry = AssetRegistry(container.database)
    asset = registry.create(foreign_project.id, "CHARACTER", "Foreign actor")
    version = registry.add_version(asset.id)
    associations = (
        {"scene_id": scene.id},
        {"shot_id": shot.id},
        {"asset_version_ids": [version.id]},
    )

    for association in associations:
        with pytest.raises(ValueError, match="different project"):
            container.memory.index(
                ShotMemoryInput(
                    project_id=project.id,
                    layer=MemoryLayer.EPISODIC,
                    memory_type="CROSS_PROJECT_TEST",
                    content=MultimodalContent(text="must not be indexed"),
                    **association,
                )
            )

    accepted = container.memory.index(
        ShotMemoryInput(
            project_id=foreign_project.id,
            layer=MemoryLayer.EPISODIC,
            memory_type="VALID_PROJECT_TEST",
            content=MultimodalContent(text="valid memory"),
            scene_id=scene.id,
            shot_id=shot.id,
            asset_version_ids=[version.id],
        )
    )
    assert accepted.project_id == foreign_project.id


def test_metrics_validate_and_infer_association_project(container, project):
    foreign_project, _scene, shot, job = _project_graph(container, "Foreign metrics")

    with pytest.raises(ValueError, match="different projects"):
        container.model_metrics.record(
            provider="test",
            model_id="test-video",
            metric="camera_failure",
            project_id=project.id,
            shot_id=shot.id,
        )
    with pytest.raises(ValueError, match="different projects"):
        container.model_metrics.record(
            provider="test",
            model_id="test-video",
            metric="physics_failure",
            project_id=project.id,
            generation_job_id=job.id,
        )

    inferred = container.model_metrics.record(
        provider="test",
        model_id="test-video",
        metric="latency",
        value=1.25,
        shot_id=shot.id,
        generation_job_id=job.id,
    )
    assert inferred.project_id == foreign_project.id


def test_0006_migrates_legacy_duplicates(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy-0005.db"
    database_url = f"sqlite:///{database_path}"
    engine = sa.create_engine(database_url)
    metadata = sa.MetaData()
    feature_flags = sa.Table(
        "feature_flags",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("metadata_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("name", "project_id", name="uq_feature_flag_scope"),
    )
    model_metrics = sa.Table(
        "model_metrics",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=True),
        sa.Column("shot_id", sa.String(36), nullable=True),
        sa.Column("generation_job_id", sa.String(36), nullable=True),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("model_id", sa.String(120), nullable=False),
        sa.Column("model_version", sa.String(80), nullable=False),
        sa.Column("metric_name", sa.String(80), nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("metadata_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    production_traces = sa.Table(
        "production_traces",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("trace_id", sa.String(64), nullable=False, unique=True),
    )
    sa.Index("ix_production_traces_trace_id", production_traces.c.trace_id)
    alembic_version = sa.Table(
        "alembic_version",
        metadata,
        sa.Column("version_num", sa.String(32), primary_key=True),
    )
    metadata.create_all(engine)
    older = datetime.now(UTC) - timedelta(days=1)
    newer = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            feature_flags.insert(),
            [
                {
                    "id": "flag-old",
                    "name": "voyage_memory",
                    "project_id": None,
                    "enabled": False,
                    "metadata_json": {},
                    "created_at": older,
                    "updated_at": older,
                },
                {
                    "id": "flag-new",
                    "name": "voyage_memory",
                    "project_id": None,
                    "enabled": True,
                    "metadata_json": {},
                    "created_at": newer,
                    "updated_at": newer,
                },
            ],
        )
        connection.execute(
            model_metrics.insert(),
            [
                {
                    "id": "metric-first",
                    "project_id": None,
                    "shot_id": None,
                    "generation_job_id": "job-1",
                    "provider": "test",
                    "model_id": "test-video",
                    "model_version": "",
                    "metric_name": "generation_success",
                    "value": 1.0,
                    "metadata_json": {},
                    "created_at": older,
                    "updated_at": older,
                },
                {
                    "id": "metric-duplicate",
                    "project_id": None,
                    "shot_id": None,
                    "generation_job_id": "job-1",
                    "provider": "test",
                    "model_id": "test-video",
                    "model_version": "",
                    "metric_name": "generation_success",
                    "value": 1.0,
                    "metadata_json": {},
                    "created_at": newer,
                    "updated_at": newer,
                },
            ],
        )
        connection.execute(alembic_version.insert().values(version_num="0005_visual_runtime"))
    engine.dispose()

    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config("alembic.ini"), "head")

    migrated_engine = sa.create_engine(database_url)
    inspector = sa.inspect(migrated_engine)
    assert inspector.get_columns("feature_flags")[-1]["name"] == "scope_key"
    assert inspector.get_columns("feature_flags")[-1]["nullable"] is False
    assert {constraint["name"] for constraint in inspector.get_unique_constraints("feature_flags")} == {
        "uq_feature_flag_name_scope_key"
    }
    assert {constraint["name"] for constraint in inspector.get_unique_constraints("model_metrics")} == {
        "uq_model_metric_job_name"
    }
    trace_indexes = {index["name"]: index for index in inspector.get_indexes("production_traces")}
    assert trace_indexes["ix_production_traces_trace_id"]["unique"] == 1
    assert inspector.get_unique_constraints("production_traces") == []
    with migrated_engine.connect() as connection:
        flags = connection.execute(sa.text("SELECT id, scope_key FROM feature_flags ORDER BY id")).all()
        metrics = connection.execute(sa.text("SELECT id FROM model_metrics ORDER BY id")).scalars().all()
    migrated_engine.dispose()
    assert flags == [("flag-new", "global")]
    assert metrics == ["metric-first"]
