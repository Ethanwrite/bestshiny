"""Workflows: the saved canvases of a project, with optimistic concurrency.

A save names the version it edited. If someone else saved in between, the
save is refused with the current version instead of silently discarding their
edit; the editor then reloads. A workflow is soft-deleted, because its runs and
the generation jobs those runs created stay in the project's history.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from platform_database import Database
from production_domain.models import (
    GenerationEvent,
    GenerationJob,
    Project,
    Workflow,
    WorkflowNodeRun,
    WorkflowNodeRunStatus,
    WorkflowRun,
    WorkflowRunStatus,
    utcnow,
)
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .graph import empty_graph, parse_graph

ACTIVE_RUN_STATUSES = (WorkflowRunStatus.QUEUED.value, WorkflowRunStatus.RUNNING.value)
TERMINAL_NODE_STATUSES = frozenset(
    {
        WorkflowNodeRunStatus.SUCCEEDED.value,
        WorkflowNodeRunStatus.FAILED.value,
        WorkflowNodeRunStatus.SKIPPED.value,
        WorkflowNodeRunStatus.CANCELLED.value,
        WorkflowNodeRunStatus.CACHED.value,
    }
)
#: How far back the canvas looks for each node's latest result.
RESULT_HISTORY_RUNS = 25
MAX_NAME = 200


class WorkflowNotFound(LookupError):
    pass


class WorkflowConflict(ValueError):
    def __init__(self, message: str, *, reason_code: str, **detail: Any):
        super().__init__(message)
        self.reason_code = reason_code
        self.detail = detail

    def as_detail(self) -> dict[str, Any]:
        return {"message": str(self), "reason_code": self.reason_code, **self.detail}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def normalize_name(name: str | None, *, fallback: str = "Untitled canvas") -> str:
    value = " ".join(str(name or "").split())
    if len(value) > MAX_NAME:
        raise ValueError(f"a workflow name is at most {MAX_NAME} characters")
    return value or fallback


def job_progress(session: Session, job_ids: list[str]) -> dict[str, dict[str, Any]]:
    """The live facts about each generation job a node points at."""

    if not job_ids:
        return {}
    jobs = session.scalars(select(GenerationJob).where(GenerationJob.id.in_(job_ids))).all()
    progress: dict[str, float] = {}
    events = session.execute(
        select(GenerationEvent.generation_job_id, GenerationEvent.detail)
        .where(
            GenerationEvent.generation_job_id.in_(job_ids),
            GenerationEvent.event_type == "PROVIDER_JOB_POLL",
        )
        .order_by(GenerationEvent.created_at)
    ).all()
    for job_id, detail in events:
        value = (detail or {}).get("progress")
        if isinstance(value, int | float):
            progress[job_id] = float(value)
    return {
        job.id: {
            "id": job.id,
            "status": job.status,
            "provider": job.provider,
            "model": job.model,
            "progress": 1.0 if job.status == "COMPLETED" else progress.get(job.id),
            "output_asset_id": job.output_asset_id,
            "error_code": job.error_code,
            "error_message": (job.error_message or "")[:500] or None,
            "billing_owner": "USER_CONNECTION" if job.connection_id else "PLATFORM",
            "quoted_credits": job.quoted_credits,
            "deleted": job.deleted_at is not None,
        }
        for job in jobs
    }


def node_run_view(node_run: WorkflowNodeRun, jobs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": node_run.id,
        "run_id": node_run.run_id,
        "node_id": node_run.node_id,
        "node_type": node_run.node_type,
        "status": node_run.status,
        "fingerprint": node_run.fingerprint,
        "outputs": node_run.outputs_json or {},
        "usage": node_run.usage_json or {},
        "error_code": node_run.error_code,
        "error_message": node_run.error_message,
        "generation_job_id": node_run.generation_job_id,
        "job": jobs.get(node_run.generation_job_id) if node_run.generation_job_id else None,
        "cached_from_node_run_id": node_run.cached_from_node_run_id,
        "started_at": _iso(node_run.started_at),
        "finished_at": _iso(node_run.finished_at),
        "updated_at": _iso(node_run.updated_at),
    }


def run_scope(run: WorkflowRun) -> str:
    """``ALL`` when the run was asked for every executable node of its snapshot, else ``NODES``."""

    executable = {
        node.get("id")
        for node in (run.graph_json or {}).get("nodes") or []
        if isinstance(node, dict) and node.get("type") != "note"
    }
    return "ALL" if set(run.scope_node_ids_json or []) == executable else "NODES"


def run_view(session: Session, run: WorkflowRun) -> dict[str, Any]:
    node_runs = session.scalars(
        select(WorkflowNodeRun).where(WorkflowNodeRun.run_id == run.id).order_by(WorkflowNodeRun.created_at)
    ).all()
    jobs = job_progress(session, [item.generation_job_id for item in node_runs if item.generation_job_id])
    counts: dict[str, int] = {}
    for item in node_runs:
        counts[item.status] = counts.get(item.status, 0) + 1
    return {
        "id": run.id,
        "workflow_id": run.workflow_id,
        "project_id": run.project_id,
        "status": run.status,
        "workflow_version": run.workflow_version,
        "scope": run_scope(run),
        "scope_node_ids": list(run.scope_node_ids_json or []),
        "force_node_ids": list(run.force_node_ids_json or []),
        "error_message": run.error_message,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "cancel_requested_at": _iso(run.cancel_requested_at),
        "counts": counts,
        "node_runs": [node_run_view(item, jobs) for item in node_runs],
    }


def cancel_run_rows(session: Session, run_id: str, *, reason: str, now: datetime) -> bool:
    """Stop a run and every node in it that has not finished, in the caller's transaction.

    Returns whether the run was still active. A generation job a node already
    created is not touched here: it is the gateway's to cancel, and it stays
    visible in Productions either way.
    """

    stopped = session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id, WorkflowRun.status.in_(ACTIVE_RUN_STATUSES))
        .values(
            status=WorkflowRunStatus.CANCELLED.value,
            cancel_requested_at=now,
            finished_at=now,
            error_message=reason[:1000],
            claim_token=None,
            claim_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if int(getattr(stopped, "rowcount", 0)) != 1:
        return False
    session.execute(
        update(WorkflowNodeRun)
        .where(
            WorkflowNodeRun.run_id == run_id,
            WorkflowNodeRun.status.in_(
                (WorkflowNodeRunStatus.PENDING.value, WorkflowNodeRunStatus.RUNNING.value)
            ),
        )
        .values(
            status=WorkflowNodeRunStatus.CANCELLED.value,
            finished_at=now,
            error_code="RUN_CANCELLED",
            error_message=reason[:1000],
        )
        .execution_options(synchronize_session=False)
    )
    return True


class WorkflowService:
    def __init__(self, database: Database, *, clock: Callable[[], datetime] = utcnow):
        self.database = database
        self._clock = clock

    @staticmethod
    def _live(session: Session, workflow_id: str, *, for_update: bool = False) -> Workflow:
        statement = select(Workflow).where(Workflow.id == workflow_id, Workflow.deleted_at.is_(None))
        if for_update:
            statement = statement.with_for_update()
        workflow = session.scalar(statement)
        if workflow is None:
            raise WorkflowNotFound("workflow not found")
        return workflow

    def project_id_for(self, workflow_id: str) -> str:
        with self.database.session() as session:
            return self._live(session, workflow_id).project_id

    def list_for_project(self, project_id: str) -> list[dict[str, Any]]:
        with self.database.session() as session:
            workflows = session.scalars(
                select(Workflow)
                .where(Workflow.project_id == project_id, Workflow.deleted_at.is_(None))
                .order_by(Workflow.updated_at.desc(), Workflow.id)
            ).all()
            latest_runs: dict[str, WorkflowRun] = {}
            if workflows:
                ranked = (
                    select(
                        WorkflowRun.id,
                        func.row_number()
                        .over(partition_by=WorkflowRun.workflow_id, order_by=WorkflowRun.created_at.desc())
                        .label("rank"),
                    )
                    .where(WorkflowRun.workflow_id.in_([item.id for item in workflows]))
                    .subquery()
                )
                for run in session.scalars(
                    select(WorkflowRun).join(ranked, ranked.c.id == WorkflowRun.id).where(ranked.c.rank == 1)
                ):
                    latest_runs[run.workflow_id] = run
            return [
                {
                    "id": item.id,
                    "project_id": item.project_id,
                    "name": item.name,
                    "version": item.version,
                    "node_count": len((item.graph_json or {}).get("nodes") or []),
                    "created_at": _iso(item.created_at),
                    "updated_at": _iso(item.updated_at),
                    "last_run": (
                        {
                            "id": latest_runs[item.id].id,
                            "status": latest_runs[item.id].status,
                            "finished_at": _iso(latest_runs[item.id].finished_at),
                        }
                        if item.id in latest_runs
                        else None
                    ),
                }
                for item in workflows
            ]

    def create(
        self,
        project_id: str,
        *,
        user_id: str | None,
        name: str | None,
        graph: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        document = parse_graph(graph).to_json() if graph is not None else empty_graph()
        with self.database.session() as session:
            project = session.get(Project, project_id)
            if project is None:
                raise LookupError("project not found")
            workflow = Workflow(
                project_id=project_id,
                workspace_id=project.workspace_id,
                name=normalize_name(name),
                graph_json=document,
                version=1,
                created_by=user_id,
                updated_by=user_id,
            )
            session.add(workflow)
            session.flush()
            return self._view(session, workflow)

    def view(self, workflow_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            return self._view(session, self._live(session, workflow_id))

    def _view(self, session: Session, workflow: Workflow) -> dict[str, Any]:
        runs = session.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == workflow.id)
            .order_by(WorkflowRun.created_at.desc())
            .limit(RESULT_HISTORY_RUNS)
        ).all()
        node_results: dict[str, dict[str, Any]] = {}
        active_run = next((run for run in runs if run.status in ACTIVE_RUN_STATUSES), None)
        if runs:
            node_runs = session.scalars(
                select(WorkflowNodeRun)
                .where(WorkflowNodeRun.run_id.in_([run.id for run in runs]))
                .order_by(WorkflowNodeRun.created_at.desc(), WorkflowNodeRun.id)
            ).all()
            jobs = job_progress(
                session, [item.generation_job_id for item in node_runs if item.generation_job_id]
            )
            for item in node_runs:
                # The newest run that ran the node speaks for it - including a
                # failure, which the user needs to see until it is fixed.
                if item.node_id not in node_results:
                    node_results[item.node_id] = node_run_view(item, jobs)
        return {
            "id": workflow.id,
            "project_id": workflow.project_id,
            "workspace_id": workflow.workspace_id,
            "name": workflow.name,
            "version": workflow.version,
            "graph": workflow.graph_json,
            "created_at": _iso(workflow.created_at),
            "updated_at": _iso(workflow.updated_at),
            "node_results": node_results,
            "active_run": run_view(session, active_run) if active_run is not None else None,
        }

    def save(
        self,
        workflow_id: str,
        *,
        user_id: str | None,
        base_version: int,
        name: str | None = None,
        graph: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        document = parse_graph(graph).to_json() if graph is not None else None
        with self.database.session() as session:
            workflow = self._live(session, workflow_id)
            values: dict[str, Any] = {
                "version": Workflow.version + 1,
                "updated_by": user_id,
                "updated_at": self._clock(),
            }
            if document is not None:
                values["graph_json"] = document
            if name is not None:
                values["name"] = normalize_name(name)
            result = session.execute(
                update(Workflow)
                .where(
                    Workflow.id == workflow_id,
                    Workflow.version == base_version,
                    Workflow.deleted_at.is_(None),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if int(getattr(result, "rowcount", 0)) != 1:
                session.refresh(workflow)
                raise WorkflowConflict(
                    "this canvas was changed somewhere else; reload to get the latest version",
                    reason_code="WORKFLOW_VERSION_CONFLICT",
                    current_version=workflow.version,
                )
            session.refresh(workflow)
            return {
                "id": workflow.id,
                "name": workflow.name,
                "version": workflow.version,
                "updated_at": _iso(workflow.updated_at),
            }

    def delete(self, workflow_id: str, *, user_id: str | None) -> None:
        with self.database.session() as session:
            workflow = self._live(session, workflow_id, for_update=True)
            workflow.deleted_at = self._clock()
            workflow.deleted_by = user_id
            now = self._clock()
            # A deleted canvas stops scheduling work. Jobs it already created
            # keep running to their own terminal state in Productions.
            active = session.scalars(
                select(WorkflowRun.id).where(
                    WorkflowRun.workflow_id == workflow_id, WorkflowRun.status.in_(ACTIVE_RUN_STATUSES)
                )
            ).all()
            for run_id in active:
                cancel_run_rows(session, run_id, reason="the canvas was deleted", now=now)


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "cancel_run_rows",
    "TERMINAL_NODE_STATUSES",
    "WorkflowConflict",
    "WorkflowNotFound",
    "WorkflowService",
    "job_progress",
    "node_run_view",
    "normalize_name",
    "run_view",
]
