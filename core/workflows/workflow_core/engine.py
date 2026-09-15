"""Running a canvas: snapshot, schedule, execute, and settle each node durably.

A run is created from the saved graph in one transaction: the graph is
snapshotted, every executable node in scope gets a ``workflow_node_runs`` row,
input nodes resolve immediately, and a node whose fingerprint matches an
earlier successful result is satisfied from it (unless the user forced it).

The worker then advances runs under a lease. A node becomes ready when every
node feeding it has succeeded; one with a failed, skipped or cancelled input
is skipped. Ready nodes execute:

- an LLM node calls the chosen connection's chat API;
- a generation node creates an ordinary generation job - through admission
  and credits for ``platform``, through the gateway's connection path for
  ``connection`` - with an idempotency key derived from the node run, so a
  crash after the job was created and before the node recorded it replays to
  the same job instead of paying twice. The node then follows the job.

Every node transition is a conditional update on the node's current status,
so a cancellation that lands while a model call is in flight wins, and the
late answer is dropped rather than written over it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from platform_contracts import GenerationRequest, PassengerGenerationCommand
from platform_database import Database
from production_domain.models import (
    GenerationIdempotency,
    GenerationJob,
    JobStatus,
    MediaAsset,
    Project,
    Workflow,
    WorkflowNodeRun,
    WorkflowNodeRunStatus,
    WorkflowRun,
    WorkflowRunStatus,
    utcnow,
)
from provider_sdk import USER_CONNECTION_PROVIDER
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .graph import (
    WorkflowGraph,
    WorkflowGraphInvalid,
    fingerprints,
    graph_hash,
    parse_graph,
    run_issues,
)
from .service import (
    ACTIVE_RUN_STATUSES,
    TERMINAL_NODE_STATUSES,
    WorkflowConflict,
    WorkflowNotFound,
    cancel_run_rows,
    run_scope,
    run_view,
)

#: Node statuses a downstream node may consume.
_SATISFIED = frozenset({WorkflowNodeRunStatus.SUCCEEDED.value, WorkflowNodeRunStatus.CACHED.value})
_BLOCKING = frozenset(
    {
        WorkflowNodeRunStatus.FAILED.value,
        WorkflowNodeRunStatus.SKIPPED.value,
        WorkflowNodeRunStatus.CANCELLED.value,
    }
)
_GENERATION_NODES = frozenset({"image_generation", "video_generation"})
_JOB_TERMINAL_FAILURES = frozenset(
    {JobStatus.FAILED.value, JobStatus.CANCELLED.value, JobStatus.WORKER_NEEDS_USER_ACTION.value}
)
#: How often a run waiting on generation jobs is looked at again.
WAITING_ADVANCE_SECONDS = 2.0
#: An LLM node's model call is retried once if the worker died during it.
MAX_LLM_ATTEMPTS = 2
#: Bound on rounds of "a node finished, so start what it unblocked" per claim.
MAX_ROUNDS_PER_ADVANCE = 50
MAX_PROMPT_CHARS = 30_000
#: `generation_jobs.model` is 120 characters.
MAX_MODEL_ID = 120

logger = logging.getLogger(__name__)


class ChatConnections(Protocol):
    async def chat(
        self,
        *,
        connection_id: str,
        workspace_id: str | None,
        model: str,
        messages: list[dict[str, str]],
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Any: ...


class GenerationSubmitter(Protocol):
    """The two ways a canvas node creates a generation job (see ``build_submitter``)."""

    def submit_platform(self, request: PlatformGeneration) -> GenerationJob: ...

    def submit_connection(self, request: ConnectionGeneration) -> GenerationJob: ...


@dataclass(frozen=True)
class PlatformGeneration:
    project_id: str
    media_type: str
    prompt: str
    idempotency_key: str
    enforce_plan: bool
    metadata: dict[str, Any]
    negative_prompt: str = ""
    provider: str = ""
    model: str = ""
    image_tier: str | None = None
    duration: float | None = None
    aspect_ratio: str = "16:9"
    resolution: str = "720p"
    start_frame_asset_id: str | None = None
    end_frame_asset_id: str | None = None
    reference_asset_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConnectionGeneration:
    project_id: str
    connection_id: str
    media_type: str
    model: str
    prompt: str
    idempotency_key: str
    metadata: dict[str, Any]
    negative_prompt: str = ""
    duration: float | None = None
    aspect_ratio: str = "16:9"
    resolution: str = "720p"
    start_frame_asset_id: str | None = None
    end_frame_asset_id: str | None = None
    reference_asset_ids: tuple[str, ...] = ()


class NodeFailure(Exception):
    """A node could not run, for a reason the user can act on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def build_submitter(
    *,
    admission: Any,
    visual_runtime: Any,
    pricing_version: Callable[[], str],
    error_codes: Callable[[Exception], str],
) -> GenerationSubmitter:
    """The production submitter: the same admission and runtime the Create page uses."""

    class _Submitter:
        def submit_platform(self, request: PlatformGeneration) -> GenerationJob:
            command = PassengerGenerationCommand(
                project_id=request.project_id,
                media_type=request.media_type,  # type: ignore[arg-type]
                provider=request.provider,
                model=request.model,
                image_tier=request.image_tier,
                prompt=request.prompt,
                negative_prompt=request.negative_prompt,
                duration=request.duration,
                aspect_ratio=request.aspect_ratio,
                resolution=request.resolution,
                reference_asset_ids=list(request.reference_asset_ids),
                start_frame_asset_id=request.start_frame_asset_id,
                end_frame_asset_id=request.end_frame_asset_id,
                idempotency_key=request.idempotency_key,
            )
            try:
                admitted = admission.admit_passenger(
                    GenerationRequest(
                        project_id=command.project_id,
                        type=command.media_type,
                        provider=command.provider,
                        model=command.model,
                        prompt=command.prompt,
                        negative_prompt=command.negative_prompt,
                        duration=command.duration,
                        aspect_ratio=command.aspect_ratio,
                        start_frame_asset_id=command.start_frame_asset_id,
                        end_frame_asset_id=command.end_frame_asset_id,
                        reference_asset_ids=command.reference_asset_ids,
                        idempotency_key=command.idempotency_key,
                        asset_criticality=command.asset_criticality,
                    ),
                    requested_role=None,
                    resolution=command.resolution,
                    enforce_plan=request.enforce_plan,
                    image_tier=command.image_tier,
                )
                estimate = admitted.estimate
                admitted_command = command.model_copy(
                    update={
                        "provider": admitted.request.provider,
                        "model": admitted.request.model,
                        "model_role": admitted.model_role,
                        "asset_criticality": admitted.request.asset_criticality,
                        "duration": admitted.request.duration,
                        "estimated_cost": estimate.estimated_total_usd,
                    }
                )
                job, _replayed = visual_runtime.submit_passenger(
                    admitted_command,
                    estimated_credits=estimate.credits,
                    pricing_version=pricing_version(),
                    quoted_cost_usd=estimate.estimated_total_usd,
                    mode="CANVAS",
                    extra_metadata=request.metadata,
                )
            except NodeFailure:
                raise
            except Exception as exc:
                raise NodeFailure(error_codes(exc), str(exc)[:1000] or type(exc).__name__) from exc
            return job

        def submit_connection(self, request: ConnectionGeneration) -> GenerationJob:
            generation = GenerationRequest(
                project_id=request.project_id,
                type=request.media_type,  # type: ignore[arg-type]
                provider=USER_CONNECTION_PROVIDER,
                model=request.model,
                prompt=request.prompt,
                negative_prompt=request.negative_prompt,
                duration=request.duration,
                aspect_ratio=request.aspect_ratio,
                start_frame_asset_id=request.start_frame_asset_id,
                end_frame_asset_id=request.end_frame_asset_id,
                reference_asset_ids=list(request.reference_asset_ids),
                idempotency_key=request.idempotency_key,
                metadata={**request.metadata, "mode": "CANVAS", "resolution": request.resolution},
            )
            try:
                job, _replayed = visual_runtime.submit(
                    generation,
                    mode="CANVAS",
                    prompt_version="canvas-user-authored-v1",
                    resolution=request.resolution,
                    connection_id=request.connection_id,
                )
            except Exception as exc:
                raise NodeFailure(error_codes(exc), str(exc)[:1000] or type(exc).__name__) from exc
            return job

    return _Submitter()


@dataclass
class _NodeState:
    row_id: str
    node_id: str
    node_type: str
    status: str
    outputs: dict[str, Any]
    generation_job_id: str | None
    attempt_count: int
    started_at: datetime | None


class WorkflowRunService:
    def __init__(
        self,
        database: Database,
        *,
        connections: ChatConnections,
        submitter: GenerationSubmitter,
        cancel_job: Callable[[str], Any] | None = None,
        llm_timeout_seconds: float = 300,
        lease_seconds: int = 300,
        clock: Callable[[], datetime] = utcnow,
    ):
        if lease_seconds < 30:
            raise ValueError("lease_seconds must be at least 30")
        self.database = database
        self.connections = connections
        self.submitter = submitter
        self.cancel_job = cancel_job
        self.llm_timeout_seconds = llm_timeout_seconds
        self.lease_seconds = lease_seconds
        self._clock = clock

    # ------------------------------------------------------------------ start / view / cancel

    def start(
        self,
        workflow_id: str,
        *,
        user_id: str | None,
        enforce_plan: bool,
        base_version: int | None = None,
        node_ids: list[str] | None = None,
        force: bool | None = None,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            workflow = session.scalar(
                select(Workflow)
                .where(Workflow.id == workflow_id, Workflow.deleted_at.is_(None))
                .with_for_update()
            )
            if workflow is None:
                raise WorkflowNotFound("workflow not found")
            if base_version is not None and workflow.version != base_version:
                raise WorkflowConflict(
                    "save the canvas before running it; the saved version is different",
                    reason_code="WORKFLOW_VERSION_CONFLICT",
                    current_version=workflow.version,
                )
            active = session.scalar(
                select(WorkflowRun.id).where(
                    WorkflowRun.workflow_id == workflow_id, WorkflowRun.status.in_(ACTIVE_RUN_STATUSES)
                )
            )
            if active is not None:
                raise WorkflowConflict(
                    "this canvas is already running; stop that run first",
                    reason_code="WORKFLOW_RUN_ACTIVE",
                    run_id=active,
                )
            graph = parse_graph(workflow.graph_json)
            executable = [node_id for node_id, node in graph.nodes.items() if node.spec.executable]
            if node_ids:
                unknown = [node_id for node_id in node_ids if node_id not in graph.nodes]
                if unknown:
                    raise WorkflowConflict(
                        "a node to run is not on the saved canvas",
                        reason_code="WORKFLOW_NODE_UNKNOWN",
                        node_ids=unknown,
                    )
                targets = [
                    node_id for node_id in dict.fromkeys(node_ids) if graph.nodes[node_id].spec.executable
                ]
                if not targets:
                    raise WorkflowConflict("notes do not run", reason_code="WORKFLOW_NOTHING_TO_RUN")
                upstream = graph.upstream(targets)
                scope = [node_id for node_id in graph.topological_order() if node_id in upstream]
                forced = set(targets) if force is not False else set()
            else:
                targets = executable
                scope = [node_id for node_id in graph.topological_order() if node_id in set(executable)]
                forced = set(executable) if force else set()
            scope = [node_id for node_id in scope if graph.nodes[node_id].spec.executable]
            if not scope:
                raise WorkflowConflict("add a node to run", reason_code="WORKFLOW_NOTHING_TO_RUN")
            issues = run_issues(graph, scope)
            if issues:
                raise WorkflowGraphInvalid(issues)
            prints = fingerprints(graph)
            now = self._clock()
            run = WorkflowRun(
                workflow_id=workflow.id,
                project_id=workflow.project_id,
                workspace_id=workflow.workspace_id,
                created_by=user_id,
                enforce_plan=enforce_plan,
                status=WorkflowRunStatus.QUEUED.value,
                graph_json=graph.to_json(),
                graph_hash=graph_hash(graph.to_json()),
                workflow_version=workflow.version,
                scope_node_ids_json=list(targets),
                force_node_ids_json=sorted(forced),
                next_advance_at=now,
            )
            session.add(run)
            session.flush()
            for node_id in scope:
                node = graph.nodes[node_id]
                node_run = WorkflowNodeRun(
                    run_id=run.id,
                    workflow_id=workflow.id,
                    node_id=node_id,
                    node_type=node.type,
                    status=WorkflowNodeRunStatus.PENDING.value,
                    fingerprint=prints[node_id],
                )
                session.add(node_run)
                session.flush([node_run])
                if node.type == "text":
                    self._finish(
                        node_run,
                        WorkflowNodeRunStatus.SUCCEEDED,
                        now,
                        outputs={"text": str(node.data.get("text") or "")},
                    )
                elif node.type == "image_input":
                    asset_id = str(node.data.get("asset_id") or "")
                    asset = session.get(MediaAsset, asset_id) if asset_id else None
                    if asset is None or asset.project_id != workflow.project_id:
                        self._finish(
                            node_run,
                            WorkflowNodeRunStatus.FAILED,
                            now,
                            error=("ASSET_NOT_FOUND", "the image is not in this project; upload it again"),
                        )
                    else:
                        self._finish(
                            node_run,
                            WorkflowNodeRunStatus.SUCCEEDED,
                            now,
                            outputs={"image": {"asset_id": asset.id}},
                        )
                elif node_id not in forced:
                    self._reuse_cached(session, node_run, now)
            session.flush()
            return run_view(session, run)

    @staticmethod
    def _finish(
        node_run: WorkflowNodeRun,
        status: WorkflowNodeRunStatus,
        now: datetime,
        *,
        outputs: dict[str, Any] | None = None,
        error: tuple[str, str] | None = None,
    ) -> None:
        node_run.status = status.value
        node_run.started_at = node_run.started_at or now
        node_run.finished_at = now
        if outputs is not None:
            node_run.outputs_json = outputs
        if error is not None:
            node_run.error_code, node_run.error_message = error[0][:100], error[1][:1000]

    def _reuse_cached(self, session: Session, node_run: WorkflowNodeRun, now: datetime) -> None:
        candidates = session.scalars(
            select(WorkflowNodeRun)
            .where(
                WorkflowNodeRun.workflow_id == node_run.workflow_id,
                WorkflowNodeRun.node_id == node_run.node_id,
                WorkflowNodeRun.fingerprint == node_run.fingerprint,
                WorkflowNodeRun.status.in_(tuple(_SATISFIED)),
                WorkflowNodeRun.id != node_run.id,
            )
            .order_by(WorkflowNodeRun.created_at.desc())
            .limit(5)
        ).all()
        for candidate in candidates:
            if not candidate.outputs_json:
                continue
            if candidate.generation_job_id:
                job = session.get(GenerationJob, candidate.generation_job_id)
                # A result the user removed from Productions may have had its
                # media reclaimed; it is not something to reuse.
                if job is None or job.deleted_at is not None or job.status != JobStatus.COMPLETED.value:
                    continue
            node_run.status = WorkflowNodeRunStatus.CACHED.value
            node_run.outputs_json = dict(candidate.outputs_json)
            node_run.usage_json = {"cached": True}
            node_run.generation_job_id = candidate.generation_job_id
            node_run.connection_id = candidate.connection_id
            node_run.cached_from_node_run_id = candidate.cached_from_node_run_id or candidate.id
            node_run.started_at = now
            node_run.finished_at = now
            return

    def view(self, run_id: str) -> dict[str, Any]:
        with self.database.session() as session:
            run = session.get(WorkflowRun, run_id)
            if run is None:
                raise LookupError("workflow run not found")
            return run_view(session, run)

    def project_id_for(self, run_id: str) -> str:
        with self.database.session() as session:
            run = session.get(WorkflowRun, run_id)
            if run is None:
                raise LookupError("workflow run not found")
            return run.project_id

    def list_for_workflow(self, workflow_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.database.session() as session:
            runs = session.scalars(
                select(WorkflowRun)
                .where(WorkflowRun.workflow_id == workflow_id)
                .order_by(WorkflowRun.created_at.desc())
                .limit(max(1, min(limit, 100)))
            ).all()
            return [
                {
                    "id": run.id,
                    "status": run.status,
                    "workflow_version": run.workflow_version,
                    "scope": run_scope(run),
                    "scope_node_ids": list(run.scope_node_ids_json or []),
                    "created_at": run.created_at.isoformat(),
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                    "error_message": run.error_message,
                }
                for run in runs
            ]

    async def cancel(self, run_id: str, *, user_id: str | None) -> dict[str, Any]:
        job_ids: list[str] = []
        with self.database.session() as session:
            run = session.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update())
            if run is None:
                raise LookupError("workflow run not found")
            running_jobs = session.scalars(
                select(WorkflowNodeRun.generation_job_id).where(
                    WorkflowNodeRun.run_id == run_id,
                    WorkflowNodeRun.status == WorkflowNodeRunStatus.RUNNING.value,
                    WorkflowNodeRun.generation_job_id.is_not(None),
                )
            ).all()
            if cancel_run_rows(session, run_id, reason="stopped by the user", now=self._clock()):
                job_ids = [job_id for job_id in running_jobs if job_id]
        if self.cancel_job is not None:
            for job_id in job_ids:
                try:
                    await self.cancel_job(job_id)
                except Exception:
                    # A job the provider cannot stop runs to its end and stays
                    # in Productions; the canvas run is stopped regardless.
                    pass
        return self.view(run_id)

    # ------------------------------------------------------------------ advance

    async def advance_due(self, *, limit: int = 20) -> int:
        now = self._clock()
        with self.database.session() as session:
            due = session.scalars(
                select(WorkflowRun.id)
                .where(
                    WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),
                    or_(WorkflowRun.next_advance_at.is_(None), WorkflowRun.next_advance_at <= now),
                    or_(WorkflowRun.claim_expires_at.is_(None), WorkflowRun.claim_expires_at <= now),
                )
                .order_by(WorkflowRun.next_advance_at, WorkflowRun.id)
                .limit(max(1, limit))
            ).all()
        claimed = [(run_id, token) for run_id in due if (token := self._claim(run_id)) is not None]
        # Runs advance side by side: one canvas waiting minutes on a model call
        # must not hold every other workspace's canvas behind it.
        outcomes = await asyncio.gather(
            *(self._advance_claimed(run_id, token) for run_id, token in claimed),
            return_exceptions=True,
        )
        for (run_id, _token), outcome in zip(claimed, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.error("workflow run %s could not advance", run_id, exc_info=outcome)
        return len(claimed)

    async def _advance_claimed(self, run_id: str, token: str) -> None:
        try:
            await self.advance(run_id, token)
        finally:
            self._release(run_id, token)

    @property
    def _lease(self) -> timedelta:
        # A claim must outlive the longest model call it may make, or a second
        # worker would take the run over while the first is still waiting.
        return timedelta(seconds=max(float(self.lease_seconds), self.llm_timeout_seconds + 60))

    def _claim(self, run_id: str) -> str | None:
        token = uuid.uuid4().hex
        now = self._clock()
        with self.database.session() as session:
            result = session.execute(
                update(WorkflowRun)
                .where(
                    WorkflowRun.id == run_id,
                    WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),
                    or_(WorkflowRun.claim_expires_at.is_(None), WorkflowRun.claim_expires_at <= now),
                )
                .values(claim_token=token, claim_expires_at=now + self._lease)
                .execution_options(synchronize_session=False)
            )
            return token if int(getattr(result, "rowcount", 0)) == 1 else None

    def _release(self, run_id: str, token: str) -> None:
        with self.database.session() as session:
            session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id, WorkflowRun.claim_token == token)
                .values(claim_token=None, claim_expires_at=None)
                .execution_options(synchronize_session=False)
            )

    def _renew(self, run_id: str, token: str) -> bool:
        with self.database.session() as session:
            result = session.execute(
                update(WorkflowRun)
                .where(
                    WorkflowRun.id == run_id,
                    WorkflowRun.claim_token == token,
                    WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),
                )
                .values(claim_expires_at=self._clock() + self._lease)
                .execution_options(synchronize_session=False)
            )
            return int(getattr(result, "rowcount", 0)) == 1

    def _load(
        self, run_id: str
    ) -> tuple[WorkflowRun, WorkflowGraph, dict[str, _NodeState], str | None] | None:
        with self.database.session() as session:
            run = session.get(WorkflowRun, run_id)
            if run is None or run.status not in ACTIVE_RUN_STATUSES:
                return None
            graph = parse_graph(run.graph_json)
            states = {
                row.node_id: _NodeState(
                    row_id=row.id,
                    node_id=row.node_id,
                    node_type=row.node_type,
                    status=row.status,
                    outputs=dict(row.outputs_json or {}),
                    generation_job_id=row.generation_job_id,
                    attempt_count=row.attempt_count,
                    started_at=row.started_at,
                )
                for row in session.scalars(select(WorkflowNodeRun).where(WorkflowNodeRun.run_id == run_id))
            }
            project = session.get(Project, run.project_id)
            session.expunge(run)
            return run, graph, states, project.workspace_id if project else None

    async def advance(self, run_id: str, token: str) -> None:
        for _round in range(MAX_ROUNDS_PER_ADVANCE):
            loaded = self._load(run_id)
            if loaded is None:
                return
            run, graph, states, workspace_id = loaded
            if run.claim_token != token:
                return
            if run.status == WorkflowRunStatus.QUEUED.value:
                self._mark_running(run_id, token)
            progressed = False
            waiting = False
            # 1. Follow the generation jobs nodes already created.
            for state in states.values():
                if (
                    state.status == WorkflowNodeRunStatus.RUNNING.value
                    and state.node_type in _GENERATION_NODES
                ):
                    outcome = self._follow_job(state)
                    progressed = progressed or outcome
                    waiting = waiting or not outcome
            if progressed:
                continue
            # 2. Skip what can no longer run; find what can.
            ready: list[_NodeState] = []
            for node_id in graph.topological_order():
                state = states.get(node_id)
                if state is None or state.status != WorkflowNodeRunStatus.PENDING.value:
                    continue
                sources = [edge.source for edge in graph.incoming(node_id)]
                source_states = [states.get(source) for source in sources]
                if any(item is None or item.status in _BLOCKING for item in source_states):
                    self._skip(state)
                    progressed = True
                    continue
                if all(item is not None and item.status in _SATISFIED for item in source_states):
                    ready.append(state)
            if progressed:
                continue
            # 3. A model call that outlived a dead worker is retried once.
            for state in states.values():
                if state.status == WorkflowNodeRunStatus.RUNNING.value and state.node_type == "llm":
                    if self._llm_abandoned(state):
                        self._reset_or_fail_llm(state)
                        progressed = True
                    else:
                        waiting = True
            if progressed:
                continue
            if not ready:
                self._settle(run_id, token, states, waiting=waiting)
                return
            if not self._renew(run_id, token):
                return
            # Creating a generation job is quick and in order; model calls wait
            # on the network, so independent ones run side by side.
            for state in [item for item in ready if item.node_type in _GENERATION_NODES]:
                await self._execute(run, graph, states, state, workspace_id)
            calls = [item for item in ready if item.node_type not in _GENERATION_NODES]
            outcomes = await asyncio.gather(
                *(self._execute(run, graph, states, state, workspace_id) for state in calls),
                return_exceptions=True,
            )
            failure = next((item for item in outcomes if isinstance(item, BaseException)), None)
            if failure is not None:
                raise failure
            if not self._renew(run_id, token):
                return
        self._schedule(run_id, token, delay_seconds=0)

    def _mark_running(self, run_id: str, token: str) -> None:
        now = self._clock()
        with self.database.session() as session:
            session.execute(
                update(WorkflowRun)
                .where(
                    WorkflowRun.id == run_id,
                    WorkflowRun.claim_token == token,
                    WorkflowRun.status == WorkflowRunStatus.QUEUED.value,
                )
                .values(status=WorkflowRunStatus.RUNNING.value, started_at=now)
                .execution_options(synchronize_session=False)
            )

    def _transition(
        self,
        state: _NodeState,
        *,
        from_status: str,
        to_status: WorkflowNodeRunStatus,
        attempt: int | None = None,
        **values: Any,
    ) -> bool:
        conditions = [WorkflowNodeRun.id == state.row_id, WorkflowNodeRun.status == from_status]
        if attempt is not None:
            conditions.append(WorkflowNodeRun.attempt_count == attempt)
        with self.database.session() as session:
            result = session.execute(
                update(WorkflowNodeRun)
                .where(*conditions)
                .values(status=to_status.value, **values)
                .execution_options(synchronize_session=False)
            )
            return int(getattr(result, "rowcount", 0)) == 1

    def _skip(self, state: _NodeState) -> None:
        self._transition(
            state,
            from_status=WorkflowNodeRunStatus.PENDING.value,
            to_status=WorkflowNodeRunStatus.SKIPPED,
            finished_at=self._clock(),
            error_code="INPUT_UNAVAILABLE",
            error_message="an input this node needs did not finish",
        )

    def _follow_job(self, state: _NodeState) -> bool:
        """Update a generation node from its job; return whether it reached a terminal state."""

        if not state.generation_job_id:
            # The job may exist: the process can die after the gateway created
            # it and before the node recorded its id. The node run's own
            # idempotency key finds it.
            with self.database.session() as session:
                recovered = session.scalar(
                    select(GenerationIdempotency.generation_job_id).where(
                        GenerationIdempotency.key == f"canvas:{state.row_id}"
                    )
                )
                if recovered:
                    session.execute(
                        update(WorkflowNodeRun)
                        .where(
                            WorkflowNodeRun.id == state.row_id,
                            WorkflowNodeRun.status == WorkflowNodeRunStatus.RUNNING.value,
                        )
                        .values(generation_job_id=recovered)
                        .execution_options(synchronize_session=False)
                    )
            if not recovered:
                if not self._llm_abandoned(state):
                    return False
                return self._transition(
                    state,
                    from_status=WorkflowNodeRunStatus.RUNNING.value,
                    to_status=WorkflowNodeRunStatus.FAILED,
                    finished_at=self._clock(),
                    error_code="JOB_MISSING",
                    error_message="the generation could not be created",
                )
            state.generation_job_id = recovered
        with self.database.session() as session:
            job = session.get(GenerationJob, state.generation_job_id)
            if job is None:
                status, asset_id, error_code, error_message = "MISSING", None, "JOB_MISSING", "job not found"
            else:
                status, asset_id = job.status, job.output_asset_id
                error_code, error_message = job.error_code, job.error_message
        if status == JobStatus.COMPLETED.value and not asset_id:
            return self._transition(
                state,
                from_status=WorkflowNodeRunStatus.RUNNING.value,
                to_status=WorkflowNodeRunStatus.FAILED,
                finished_at=self._clock(),
                error_code="JOB_NO_OUTPUT",
                error_message="the generation completed without an output",
            )
        if status == JobStatus.COMPLETED.value and asset_id:
            port = "video" if state.node_type == "video_generation" else "image"
            return self._transition(
                state,
                from_status=WorkflowNodeRunStatus.RUNNING.value,
                to_status=WorkflowNodeRunStatus.SUCCEEDED,
                finished_at=self._clock(),
                outputs_json={port: {"asset_id": asset_id, "generation_job_id": state.generation_job_id}},
            )
        if status in _JOB_TERMINAL_FAILURES or status == "MISSING":
            message = (error_message or "").strip() or f"the generation ended as {status.lower()}"
            if status == JobStatus.WORKER_NEEDS_USER_ACTION.value:
                message = f"{message} - open Productions to resolve it"
            return self._transition(
                state,
                from_status=WorkflowNodeRunStatus.RUNNING.value,
                to_status=WorkflowNodeRunStatus.FAILED,
                finished_at=self._clock(),
                error_code=(error_code or f"JOB_{status}")[:100],
                error_message=message[:1000],
            )
        return False

    def _llm_abandoned(self, state: _NodeState) -> bool:
        if state.started_at is None:
            return True
        started = (
            state.started_at if state.started_at.tzinfo else state.started_at.replace(tzinfo=utcnow().tzinfo)
        )
        return self._clock() - started > timedelta(seconds=self.llm_timeout_seconds + 60)

    def _reset_or_fail_llm(self, state: _NodeState) -> None:
        if state.attempt_count < MAX_LLM_ATTEMPTS:
            self._transition(
                state,
                from_status=WorkflowNodeRunStatus.RUNNING.value,
                to_status=WorkflowNodeRunStatus.PENDING,
                attempt=state.attempt_count,
            )
        else:
            self._transition(
                state,
                from_status=WorkflowNodeRunStatus.RUNNING.value,
                to_status=WorkflowNodeRunStatus.FAILED,
                attempt=state.attempt_count,
                finished_at=self._clock(),
                error_code="LLM_TIMEOUT",
                error_message="the model call did not finish",
            )

    @staticmethod
    def _input_values(
        graph: WorkflowGraph, states: dict[str, _NodeState], node_id: str, port: str
    ) -> list[Any]:
        values = []
        for edge in graph.inputs_on(node_id, port):
            source = states.get(edge.source)
            if source is not None:
                values.append(source.outputs.get(edge.source_port))
        return [value for value in values if value not in (None, "")]

    @staticmethod
    def _asset_ids(values: list[Any]) -> list[str]:
        return [
            str(value["asset_id"])
            for value in values
            if isinstance(value, dict) and isinstance(value.get("asset_id"), str)
        ]

    async def _execute(
        self,
        run: WorkflowRun,
        graph: WorkflowGraph,
        states: dict[str, _NodeState],
        state: _NodeState,
        workspace_id: str | None,
    ) -> None:
        node = graph.nodes[state.node_id]
        now = self._clock()
        attempt = state.attempt_count + 1
        if not self._transition(
            state,
            from_status=WorkflowNodeRunStatus.PENDING.value,
            to_status=WorkflowNodeRunStatus.RUNNING,
            started_at=now,
            attempt_count=attempt,
        ):
            return
        state.status = WorkflowNodeRunStatus.RUNNING.value
        state.attempt_count = attempt
        try:
            if node.type == "llm":
                await self._execute_llm(run, graph, states, state, workspace_id, attempt)
            elif node.type in _GENERATION_NODES:
                self._execute_generation(run, graph, states, state, attempt)
            else:
                raise NodeFailure("NODE_NOT_EXECUTABLE", f"{node.spec.title} nodes do not run")
        except NodeFailure as failure:
            self._transition(
                state,
                from_status=WorkflowNodeRunStatus.RUNNING.value,
                to_status=WorkflowNodeRunStatus.FAILED,
                attempt=attempt,
                finished_at=self._clock(),
                error_code=failure.code[:100],
                error_message=str(failure)[:1000],
            )

    async def _execute_llm(
        self,
        run: WorkflowRun,
        graph: WorkflowGraph,
        states: dict[str, _NodeState],
        state: _NodeState,
        workspace_id: str | None,
        attempt: int,
    ) -> None:
        node = graph.nodes[state.node_id]
        data = node.data
        prompts = [str(value) for value in self._input_values(graph, states, node.id, "prompt")]
        contexts = [str(value) for value in self._input_values(graph, states, node.id, "context")]
        instruction = str(data.get("instruction") or "").strip()
        sections = [part for part in (instruction, *prompts) if part.strip()]
        if contexts:
            sections.append("Context:\n" + "\n\n---\n\n".join(contexts))
        message = "\n\n".join(sections).strip()[: MAX_PROMPT_CHARS * 4]
        if not message:
            raise NodeFailure("EMPTY_PROMPT", "there is nothing to send to the model")
        connection_id = str(data.get("connection_id") or "")
        model = str(data.get("model") or "")
        self._record_inputs(
            state,
            attempt,
            {"connection_id": connection_id, "model": model, "prompt_preview": message[:2000]},
            connection_id=connection_id,
        )
        try:
            result = await asyncio.wait_for(
                self.connections.chat(
                    connection_id=connection_id,
                    workspace_id=workspace_id,
                    model=model,
                    messages=[{"role": "user", "content": message}],
                    system=str(data.get("system_prompt") or "").strip() or None,
                    max_tokens=data.get("max_tokens"),
                    temperature=data.get("temperature"),
                ),
                timeout=self.llm_timeout_seconds,
            )
        except TimeoutError as exc:
            raise NodeFailure(
                "LLM_TIMEOUT", f"the model did not answer within {self.llm_timeout_seconds:g}s"
            ) from exc
        except NodeFailure:
            raise
        except Exception as exc:
            raise NodeFailure(str(getattr(exc, "code", "") or "LLM_FAILED"), str(exc)[:1000]) from exc
        usage = result.usage() if hasattr(result, "usage") else {}
        self._transition(
            state,
            from_status=WorkflowNodeRunStatus.RUNNING.value,
            to_status=WorkflowNodeRunStatus.SUCCEEDED,
            attempt=attempt,
            finished_at=self._clock(),
            outputs_json={"text": str(result.text)},
            usage_json=usage,
        )

    def _record_inputs(
        self, state: _NodeState, attempt: int, inputs: dict[str, Any], *, connection_id: str | None = None
    ) -> None:
        with self.database.session() as session:
            session.execute(
                update(WorkflowNodeRun)
                .where(
                    WorkflowNodeRun.id == state.row_id,
                    WorkflowNodeRun.status == WorkflowNodeRunStatus.RUNNING.value,
                    WorkflowNodeRun.attempt_count == attempt,
                )
                .values(inputs_json=inputs, connection_id=connection_id or None)
                .execution_options(synchronize_session=False)
            )

    def _execute_generation(
        self,
        run: WorkflowRun,
        graph: WorkflowGraph,
        states: dict[str, _NodeState],
        state: _NodeState,
        attempt: int,
    ) -> None:
        node = graph.nodes[state.node_id]
        data = node.data
        media_type = "video" if node.type == "video_generation" else "image"
        connected = [str(value) for value in self._input_values(graph, states, node.id, "prompt")]
        prompt = (
            "\n\n".join(part for part in connected if part.strip()) or str(data.get("prompt") or "")
        ).strip()
        if not prompt:
            raise NodeFailure("EMPTY_PROMPT", "the generation has no prompt")
        prompt = prompt[:MAX_PROMPT_CHARS]
        references = self._asset_ids(self._input_values(graph, states, node.id, "reference"))
        first = self._asset_ids(self._input_values(graph, states, node.id, "first_frame"))
        last = self._asset_ids(self._input_values(graph, states, node.id, "last_frame"))
        metadata = {
            "workflow_id": run.workflow_id,
            "workflow_run_id": run.id,
            "workflow_node_id": node.id,
        }
        # One key per node run: a replay after a crash finds the job it made.
        idempotency_key = f"canvas:{state.row_id}"
        duration = float(data.get("duration") or 5) if media_type == "video" else None
        aspect_ratio = str(data.get("aspect_ratio") or ("16:9" if media_type == "video" else "1:1"))
        # An image job carries the command's default resolution, as the Create page's does (it sends
        # none for images), so the canvas is quoted exactly what that page is quoted.
        resolution = str(data.get("resolution") or "720p") if media_type == "video" else "720p"
        source = str(data.get("source") or "platform")
        inputs: dict[str, Any] = {
            "source": source,
            "prompt_preview": prompt[:2000],
            "reference_asset_ids": references,
            "first_frame_asset_id": first[0] if first else None,
            "last_frame_asset_id": last[0] if last else None,
        }
        if source == "connection":
            connection_id = str(data.get("connection_id") or "")
            model = str(data.get("model") or "")
            if len(model) > MAX_MODEL_ID:
                raise NodeFailure("MODEL_ID_TOO_LONG", f"a model id is at most {MAX_MODEL_ID} characters")
            inputs.update({"connection_id": connection_id, "model": model})
            self._record_inputs(state, attempt, inputs, connection_id=connection_id)
            options: dict[str, Any] = {}
            if media_type == "video" and isinstance(data.get("generate_audio"), bool):
                options["generate_audio"] = data["generate_audio"]
            job = self.submitter.submit_connection(
                ConnectionGeneration(
                    project_id=run.project_id,
                    connection_id=connection_id,
                    media_type=media_type,
                    model=model,
                    prompt=prompt,
                    negative_prompt=str(data.get("negative_prompt") or ""),
                    idempotency_key=idempotency_key,
                    metadata={**metadata, **({"connection_options": options} if options else {})},
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    start_frame_asset_id=first[0] if first else None,
                    end_frame_asset_id=last[0] if last and first else None,
                    reference_asset_ids=tuple(references),
                )
            )
        else:
            provider, _, model = str(data.get("platform_model") or "").partition(":")
            inputs.update(
                {"platform_model": data.get("platform_model") or "", "image_tier": data.get("image_tier")}
            )
            self._record_inputs(state, attempt, inputs)
            job = self.submitter.submit_platform(
                PlatformGeneration(
                    project_id=run.project_id,
                    media_type=media_type,
                    prompt=prompt,
                    negative_prompt=str(data.get("negative_prompt") or ""),
                    idempotency_key=idempotency_key,
                    enforce_plan=run.enforce_plan,
                    metadata=metadata,
                    provider=provider if media_type == "video" and model else "",
                    model=model if media_type == "video" and provider else "",
                    image_tier=str(data.get("image_tier") or "shiny") if media_type == "image" else None,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    start_frame_asset_id=first[0] if first else None,
                    end_frame_asset_id=last[0] if last and first else None,
                    reference_asset_ids=tuple(references),
                )
            )
        with self.database.session() as session:
            session.execute(
                update(WorkflowNodeRun)
                .where(
                    WorkflowNodeRun.id == state.row_id,
                    WorkflowNodeRun.status == WorkflowNodeRunStatus.RUNNING.value,
                    WorkflowNodeRun.attempt_count == attempt,
                )
                .values(generation_job_id=job.id)
                .execution_options(synchronize_session=False)
            )
        state.generation_job_id = job.id

    def _settle(self, run_id: str, token: str, states: dict[str, _NodeState], *, waiting: bool) -> None:
        latest = self._load(run_id)
        if latest is None:
            return
        _run, _graph, current, _workspace = latest
        unfinished = [state for state in current.values() if state.status not in TERMINAL_NODE_STATUSES]
        if unfinished or waiting:
            self._schedule(run_id, token, delay_seconds=WAITING_ADVANCE_SECONDS)
            return
        failed = [
            state
            for state in current.values()
            if state.status in {WorkflowNodeRunStatus.FAILED.value, WorkflowNodeRunStatus.SKIPPED.value}
        ]
        now = self._clock()
        with self.database.session() as session:
            session.execute(
                update(WorkflowRun)
                .where(
                    WorkflowRun.id == run_id,
                    WorkflowRun.claim_token == token,
                    WorkflowRun.status.in_(ACTIVE_RUN_STATUSES),
                )
                .values(
                    status=(WorkflowRunStatus.FAILED if failed else WorkflowRunStatus.SUCCEEDED).value,
                    finished_at=now,
                    started_at=_run.started_at or now,
                    next_advance_at=None,
                    error_message=(
                        f"{len(failed)} node{'s' if len(failed) != 1 else ''} did not finish"
                        if failed
                        else None
                    ),
                )
                .execution_options(synchronize_session=False)
            )

    def _schedule(self, run_id: str, token: str, *, delay_seconds: float) -> None:
        with self.database.session() as session:
            session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id, WorkflowRun.claim_token == token)
                .values(next_advance_at=self._clock() + timedelta(seconds=delay_seconds))
                .execution_options(synchronize_session=False)
            )


def node_failure_code(exc: Exception) -> str:
    """A stable node error code for an exception from admission, the gateway or a connection."""

    explicit = getattr(exc, "code", None) or getattr(exc, "reason_code", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    names = {cls.__name__ for cls in type(exc).__mro__}
    for name, code in (
        ("InsufficientWorkspaceCredits", "INSUFFICIENT_CREDITS"),
        ("PlanEntitlementDenied", "PLAN_DENIED"),
        ("ProductionBudgetExceeded", "PLATFORM_BUSY"),
        ("SpendAuthorizationDenied", "SPEND_NOT_AUTHORIZED"),
        ("PricingUnverified", "PRICING_UNVERIFIED"),
        ("ExplicitModelUnavailable", "MODEL_UNAVAILABLE"),
        ("IdempotencyConflict", "IDEMPOTENCY_CONFLICT"),
        ("WorkspaceCreditConflict", "CREDIT_CONFLICT"),
        ("CapabilityProviderNotFound", "MODEL_UNAVAILABLE"),
        ("LookupError", "NOT_FOUND"),
        ("ValueError", "GENERATION_REJECTED"),
    ):
        if name in names:
            return code
    return "GENERATION_FAILED"


__all__ = [
    "ConnectionGeneration",
    "GenerationSubmitter",
    "NodeFailure",
    "PlatformGeneration",
    "WorkflowRunService",
    "build_submitter",
    "node_failure_code",
]
