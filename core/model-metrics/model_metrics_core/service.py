from __future__ import annotations

from collections import defaultdict
from typing import Any

from platform_database import Database
from production_domain.models import (
    Episode,
    GenerationJob,
    ModelMetric,
    Project,
    Scene,
    Shot,
    new_id,
    utcnow,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

METRIC_NAMES = frozenset(
    {
        "generation_success",
        "user_accept",
        "user_regenerate",
        "auto_retry",
        "identity_failure",
        "scene_failure",
        "prop_failure",
        "dialogue_failure",
        "camera_failure",
        "gaze_failure",
        "physics_failure",
        "latency",
        "cost",
    }
)

FAILURE_TO_DIMENSION = {
    "identity_failure": "character_consistency",
    "scene_failure": "scene_consistency",
    "prop_failure": "product_fidelity",
    "dialogue_failure": "dialogue",
    "camera_failure": "camera_control",
    "gaze_failure": "camera_control",
    "physics_failure": "physical_plausibility",
}


class ModelMetricsService:
    """Append-only production evidence used by the adaptive router."""

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _linked_project_id(
        session: Session,
        *,
        project_id: str | None,
        shot_id: str | None,
        generation_job_id: str | None,
    ) -> str | None:
        project_ids: list[str] = []
        if project_id is not None:
            if session.get(Project, project_id) is None:
                raise LookupError("project not found")
            project_ids.append(project_id)
        if shot_id is not None:
            shot_project_id = session.scalar(
                select(Episode.project_id)
                .join(Scene, Scene.episode_id == Episode.id)
                .join(Shot, Shot.scene_id == Scene.id)
                .where(Shot.id == shot_id)
            )
            if shot_project_id is None:
                raise LookupError("shot not found")
            project_ids.append(shot_project_id)
        if generation_job_id is not None:
            job_project_id = session.scalar(
                select(GenerationJob.project_id).where(GenerationJob.id == generation_job_id)
            )
            if job_project_id is None:
                raise LookupError("generation job not found")
            project_ids.append(job_project_id)
        if len(set(project_ids)) > 1:
            raise ValueError("metric associations belong to different projects")
        return project_ids[0] if project_ids else None

    def record(
        self,
        *,
        provider: str,
        model_id: str,
        metric: str,
        value: float = 1.0,
        project_id: str | None = None,
        shot_id: str | None = None,
        generation_job_id: str | None = None,
        model_version: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ModelMetric:
        if metric not in METRIC_NAMES:
            raise ValueError(f"unsupported production metric: {metric}")
        with self.database.session() as session:
            linked_project_id = self._linked_project_id(
                session,
                project_id=project_id,
                shot_id=shot_id,
                generation_job_id=generation_job_id,
            )
            record = ModelMetric(
                project_id=linked_project_id,
                shot_id=shot_id,
                generation_job_id=generation_job_id,
                provider=provider,
                model_id=model_id,
                model_version=model_version,
                metric_name=metric,
                value=float(value),
                metadata_json=metadata or {},
            )
            session.add(record)
            session.flush()
            return record

    def record_once(
        self,
        *,
        provider: str,
        model_id: str,
        metric: str,
        generation_job_id: str,
        value: float = 1.0,
        project_id: str | None = None,
        shot_id: str | None = None,
        model_version: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ModelMetric:
        if metric not in METRIC_NAMES:
            raise ValueError(f"unsupported production metric: {metric}")
        with self.database.session() as session:
            linked_project_id = self._linked_project_id(
                session,
                project_id=project_id,
                shot_id=shot_id,
                generation_job_id=generation_job_id,
            )
            now = utcnow()
            values = {
                "id": new_id(),
                "project_id": linked_project_id,
                "shot_id": shot_id,
                "generation_job_id": generation_job_id,
                "provider": provider,
                "model_id": model_id,
                "model_version": model_version,
                "metric_name": metric,
                "value": float(value),
                "metadata_json": metadata or {},
                "created_at": now,
                "updated_at": now,
            }
            dialect = session.get_bind().dialect.name
            insert_statement: Any
            if dialect == "postgresql":
                insert_statement = postgresql_insert(ModelMetric).values(**values)
            elif dialect == "sqlite":
                insert_statement = sqlite_insert(ModelMetric).values(**values)
            else:  # pragma: no cover - the platform supports SQLite and PostgreSQL.
                raise RuntimeError(f"unsupported database dialect for metric upsert: {dialect}")
            statement = insert_statement.on_conflict_do_nothing(
                index_elements=["generation_job_id", "metric_name"]
            )
            session.execute(statement)
            record = session.scalar(
                select(ModelMetric).where(
                    ModelMetric.generation_job_id == generation_job_id,
                    ModelMetric.metric_name == metric,
                )
            )
            if record is None:  # pragma: no cover - defensive guard for a failed database insert.
                raise RuntimeError("metric upsert did not return a persisted value")
            session.flush()
            return record

    def production_adjustments(
        self,
    ) -> tuple[dict[str, dict[str, float]], dict[str, int]]:
        with self.database.session() as session:
            records = list(session.scalars(select(ModelMetric).order_by(ModelMetric.created_at)))
        grouped: dict[str, list[ModelMetric]] = defaultdict(list)
        for record in records:
            grouped[f"{record.provider}:{record.model_id}"].append(record)
        adjustments: dict[str, dict[str, float]] = {}
        counts: dict[str, int] = {}
        for key, values in grouped.items():
            generations = [item for item in values if item.metric_name == "generation_success"]
            sample_count = len(generations)
            counts[key] = sample_count
            accepts = sum(item.value for item in values if item.metric_name == "user_accept")
            regenerates = sum(item.value for item in values if item.metric_name == "user_regenerate")
            decisions = accepts + regenerates
            dimensions: dict[str, float] = {}
            if decisions > 0:
                base_success = max(0.0, min(1.0, accepts / decisions))
                dimensions.update(
                    {
                        "visual_quality": base_success,
                        "character_consistency": base_success,
                        "scene_consistency": base_success,
                        "physical_plausibility": base_success,
                        "camera_control": base_success,
                        "dialogue": base_success,
                        "product_fidelity": base_success,
                    }
                )
            for metric, dimension in FAILURE_TO_DIMENSION.items():
                failure_records = [item for item in values if item.metric_name == metric]
                if failure_records and sample_count:
                    failures = sum(item.value for item in failure_records)
                    dimensions[dimension] = max(
                        0.0,
                        min(1.0, 1.0 - failures / sample_count),
                    )
            adjustments[key] = dimensions
        return adjustments, counts
