"""Canvas workflows through the API and the run engine.

What these tests pin:

1. saving - versions are optimistic, an invalid document is refused with every
   problem named, and a canvas belongs to its project's workspace;
2. running - a run executes its nodes in dependency order on the connections
   and models they name, hands each node's output to the nodes it feeds, and
   charges no credits for connection-paid nodes;
3. reuse - an unchanged node reuses its earlier result instead of paying
   again, a forced node regenerates, and an edit invalidates everything
   downstream of it;
4. failure - a node that cannot run fails with its reason, what depends on it
   is skipped, and the run says so; a stopped run stops its nodes and jobs;
5. platform nodes - a credit-paid node goes through the same admission and
   runtime as the Create page, labelled as coming from the canvas.
"""

from __future__ import annotations

import asyncio
import shutil
from types import SimpleNamespace

import pytest
from connection_core import generation as connection_generation
from entitlement_core import InsufficientWorkspaceCredits
from fastapi.testclient import TestClient
from generation_gateway.worker import advance_workflow_runs_once, process_next_job
from production_domain.models import (
    GenerationJob,
    WorkflowNodeRun,
    WorkflowRun,
    Workspace,
)
from sqlalchemy import func, select
from video_platform_api.main import create_app
from workflow_core import GRAPH_SCHEMA

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def _register(client: TestClient, email: str) -> tuple[dict[str, str], str, str]:
    registered = client.post(
        "/api/auth/register", json={"email": email, "password": "correct horse battery staple"}
    )
    assert registered.status_code in {200, 201}, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    created = client.post("/v1/projects", headers=headers, json={"title": "Canvas"}).json()
    project = client.get(f"/v1/projects/{created['id']}", headers=headers).json()
    return headers, project["id"], project["workspace_id"]


def node(node_id: str, node_type: str, x: float = 0, **data):  # type: ignore[no-untyped-def]
    return {"id": node_id, "type": node_type, "position": {"x": x, "y": 0}, "data": data}


def edge(edge_id: str, source: str, source_port: str, target: str, target_port: str) -> dict[str, str]:
    return {
        "id": edge_id,
        "source": source,
        "source_port": source_port,
        "target": target,
        "target_port": target_port,
    }


def _mock_connection(container, client: TestClient, headers, workspace_id: str) -> str:  # type: ignore[no-untyped-def]
    container.connections.mock_protocol_enabled = True
    response = client.post(
        f"/v1/workspaces/{workspace_id}/connections",
        headers=headers,
        json={
            "name": "Mock",
            "protocol": "mock",
            "api_key": "",
            "models": [
                {"id": "mock-chat", "capability": "chat"},
                {"id": "mock-image", "capability": "image"},
                {"id": "mock-video", "capability": "video"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _story_graph(connection_id: str, *, with_video: bool) -> dict:
    nodes = [
        node("text", "text", text="A lighthouse keeper at dusk"),
        node(
            "llm", "llm", 300, connection_id=connection_id, model="mock-chat", instruction="Make it cinematic"
        ),
        node(
            "image",
            "image_generation",
            600,
            source="connection",
            connection_id=connection_id,
            model="mock-image",
            aspect_ratio="16:9",
        ),
        node("note", "note", 0, text="notes never run"),
    ]
    edges = [edge("e1", "text", "text", "llm", "prompt"), edge("e2", "llm", "text", "image", "prompt")]
    if with_video:
        nodes.append(
            node(
                "video",
                "video_generation",
                900,
                source="connection",
                connection_id=connection_id,
                model="mock-video",
                duration=2,
                generate_audio=False,
            )
        )
        edges += [
            edge("e3", "llm", "text", "video", "prompt"),
            edge("e4", "image", "image", "video", "first_frame"),
        ]
    return {"schema": GRAPH_SCHEMA, "nodes": nodes, "edges": edges}


def _canvas(client: TestClient, headers, project_id: str, graph: dict) -> dict:  # type: ignore[no-untyped-def]
    created = client.post(
        f"/v1/projects/{project_id}/workflows", headers=headers, json={"name": "Story", "graph": graph}
    )
    assert created.status_code == 201, created.text
    return created.json()


async def _drive(container, run_id: str, rounds: int = 80) -> dict:  # type: ignore[no-untyped-def]
    container.gateway.poll_interval_seconds = 0.01
    for _ in range(rounds):
        await advance_workflow_runs_once(container)
        for _job in range(10):
            if not await process_next_job(container):
                break
        view = container.workflow_runs.view(run_id)
        if view["status"] in TERMINAL:
            return view
        await asyncio.sleep(0.03)
    return container.workflow_runs.view(run_id)


def _nodes(view: dict) -> dict[str, dict]:
    return {item["node_id"]: item for item in view["node_runs"]}


def _job_count(container, project_id: str) -> int:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        return int(
            session.scalar(
                select(func.count()).select_from(GenerationJob).where(GenerationJob.project_id == project_id)
            )
        )


# ---------------------------------------------------------------------------
# 1. Saving
# ---------------------------------------------------------------------------


def test_canvas_saves_are_versioned_validated_and_scoped_to_the_project(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, project_id, _workspace_id = _register(client, "canvas-owner@example.com")
        catalog = client.get("/v1/workflows/node-types", headers=headers)
        assert catalog.status_code == 200 and catalog.json()["graph_schema"] == GRAPH_SCHEMA

        created = client.post(f"/v1/projects/{project_id}/workflows", headers=headers, json={"name": "Draft"})
        assert created.status_code == 201, created.text
        workflow = created.json()
        assert workflow["version"] == 1 and workflow["graph"]["nodes"] == []

        graph = {"schema": GRAPH_SCHEMA, "nodes": [node("t", "text", text="hello")], "edges": []}
        saved = client.put(
            f"/v1/workflows/{workflow['id']}", headers=headers, json={"base_version": 1, "graph": graph}
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == 2

        stale = client.put(
            f"/v1/workflows/{workflow['id']}", headers=headers, json={"base_version": 1, "name": "Stale"}
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["reason_code"] == "WORKFLOW_VERSION_CONFLICT"
        assert stale.json()["detail"]["current_version"] == 2

        invalid = client.put(
            f"/v1/workflows/{workflow['id']}",
            headers=headers,
            json={
                "base_version": 2,
                "graph": {
                    "schema": GRAPH_SCHEMA,
                    "nodes": [node("t", "text"), node("i", "image_generation")],
                    "edges": [edge("e", "t", "text", "i", "reference")],
                },
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["reason_code"] == "WORKFLOW_INVALID"
        assert invalid.json()["detail"]["errors"][0]["code"] == "TYPE_MISMATCH"

        listing = client.get(f"/v1/projects/{project_id}/workflows", headers=headers).json()["workflows"]
        assert [(item["id"], item["node_count"]) for item in listing] == [(workflow["id"], 1)]

        stranger_headers, _p, _w = _register(client, "canvas-stranger@example.com")
        assert client.get(f"/v1/workflows/{workflow['id']}", headers=stranger_headers).status_code == 403
        assert (
            client.put(
                f"/v1/workflows/{workflow['id']}",
                headers=stranger_headers,
                json={"base_version": 2, "name": "x"},
            ).status_code
            == 403
        )

        assert client.delete(f"/v1/workflows/{workflow['id']}", headers=headers).status_code == 204
        assert client.get(f"/v1/workflows/{workflow['id']}", headers=headers).status_code == 404


# ---------------------------------------------------------------------------
# 2. Running
# ---------------------------------------------------------------------------


async def test_a_run_executes_llm_then_image_on_a_connection_and_charges_nothing(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, project_id, workspace_id = _register(client, "runner@example.com")
        connection_id = _mock_connection(container, client, headers, workspace_id)
        workflow = _canvas(client, headers, project_id, _story_graph(connection_id, with_video=False))
        with container.database.session() as session:
            balance_before = session.get(Workspace, workspace_id).credit_balance

        started = client.post(
            f"/v1/workflows/{workflow['id']}/runs",
            headers=headers,
            json={"base_version": workflow["version"]},
        )
        assert started.status_code == 202, started.text
        run = started.json()
        assert run["status"] == "QUEUED"
        assert _nodes(run)["text"]["status"] == "SUCCEEDED", "an input node resolves when the run starts"
        assert "note" not in _nodes(run), "notes never run"

        view = await _drive(container, run["id"])
        assert view["status"] == "SUCCEEDED", view
        nodes = _nodes(view)
        assert nodes["llm"]["outputs"]["text"].startswith("[mock mock-chat]")
        assert "Make it cinematic" in nodes["llm"]["outputs"]["text"]
        assert "A lighthouse keeper at dusk" in nodes["llm"]["outputs"]["text"]
        image = nodes["image"]["outputs"]["image"]
        assert image["asset_id"] and image["generation_job_id"]
        assert nodes["image"]["job"]["billing_owner"] == "USER_CONNECTION"

        with container.database.session() as session:
            job = session.get(GenerationJob, image["generation_job_id"])
            assert job.connection_id == connection_id
            assert job.request_json["prompt"] == nodes["llm"]["outputs"]["text"]
            assert job.request_json["metadata"]["mode"] == "CANVAS"
            assert job.request_json["metadata"]["workflow_node_id"] == "image"
            assert session.get(Workspace, workspace_id).credit_balance == balance_before

        reloaded = client.get(f"/v1/workflows/{workflow['id']}", headers=headers).json()
        assert set(reloaded["node_results"]) == {"text", "llm", "image"}
        assert reloaded["active_run"] is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the mock video protocol renders with FFmpeg")
async def test_a_generated_image_becomes_the_videos_first_frame(container, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(connection_generation, "MOCK_VIDEO_RENDER_SECONDS", 0.0)
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, project_id, workspace_id = _register(client, "video-runner@example.com")
        connection_id = _mock_connection(container, client, headers, workspace_id)
        workflow = _canvas(client, headers, project_id, _story_graph(connection_id, with_video=True))
        run = client.post(f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={}).json()
        view = await _drive(container, run["id"], rounds=150)
        assert view["status"] == "SUCCEEDED", view
        nodes = _nodes(view)
        video = nodes["video"]["outputs"]["video"]
        with container.database.session() as session:
            job = session.get(GenerationJob, video["generation_job_id"])
            assert job.generation_type == "video"
            assert job.request_json["start_frame_asset_id"] == nodes["image"]["outputs"]["image"]["asset_id"]
            assert job.request_json["metadata"]["connection_options"] == {"generate_audio": False}


# ---------------------------------------------------------------------------
# 3. Reuse
# ---------------------------------------------------------------------------


async def test_unchanged_nodes_are_reused_forced_nodes_regenerate_and_edits_flow_downstream(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, project_id, workspace_id = _register(client, "reuse@example.com")
        connection_id = _mock_connection(container, client, headers, workspace_id)
        graph = _story_graph(connection_id, with_video=False)
        workflow = _canvas(client, headers, project_id, graph)

        first = client.post(f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={}).json()
        assert first["scope"] == "ALL"
        first_view = await _drive(container, first["id"])
        assert first_view["status"] == "SUCCEEDED"
        assert _job_count(container, project_id) == 1

        again = client.post(f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={}).json()
        assert {key: item["status"] for key, item in _nodes(again).items()} == {
            "text": "SUCCEEDED",
            "llm": "CACHED",
            "image": "CACHED",
        }
        again_view = await _drive(container, again["id"])
        assert again_view["status"] == "SUCCEEDED"
        assert _job_count(container, project_id) == 1, "an unchanged canvas is not paid for twice"
        assert _nodes(again_view)["image"]["outputs"] == _nodes(first_view)["image"]["outputs"]

        forced = client.post(
            f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={"node_ids": ["image"]}
        ).json()
        assert forced["scope"] == "NODES"
        assert _nodes(forced)["llm"]["status"] == "CACHED"
        assert _nodes(forced)["image"]["status"] == "PENDING"
        forced_view = await _drive(container, forced["id"])
        assert forced_view["status"] == "SUCCEEDED"
        assert _job_count(container, project_id) == 2, "running one node regenerates it"

        graph["nodes"][1]["data"]["instruction"] = "Make it noir"
        current = client.get(f"/v1/workflows/{workflow['id']}", headers=headers).json()
        saved = client.put(
            f"/v1/workflows/{workflow['id']}",
            headers=headers,
            json={"base_version": current["version"], "graph": graph},
        )
        assert saved.status_code == 200
        edited = client.post(f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={}).json()
        assert _nodes(edited)["llm"]["status"] == "PENDING"
        assert _nodes(edited)["image"]["status"] == "PENDING", "an edit invalidates what depends on it"
        edited_view = await _drive(container, edited["id"])
        assert edited_view["status"] == "SUCCEEDED"
        assert "Make it noir" in _nodes(edited_view)["llm"]["outputs"]["text"]


# ---------------------------------------------------------------------------
# 4. Failure, validation, conflicts, cancellation
# ---------------------------------------------------------------------------


async def test_a_failed_node_skips_what_depends_on_it(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, project_id, workspace_id = _register(client, "failure@example.com")
        connection_id = _mock_connection(container, client, headers, workspace_id)
        workflow = _canvas(client, headers, project_id, _story_graph(connection_id, with_video=False))
        assert (
            client.delete(
                f"/v1/workspaces/{workspace_id}/connections/{connection_id}", headers=headers
            ).status_code
            == 204
        )
        run = client.post(f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={}).json()
        view = await _drive(container, run["id"])
        assert view["status"] == "FAILED"
        nodes = _nodes(view)
        assert nodes["llm"]["status"] == "FAILED"
        assert nodes["llm"]["error_code"] == "CONNECTION_UNAVAILABLE"
        assert nodes["image"]["status"] == "SKIPPED"
        assert _job_count(container, project_id) == 0
        assert view["error_message"] == "2 nodes did not finish"


def test_runs_refuse_invalid_stale_duplicate_and_unknown_requests(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, project_id, workspace_id = _register(client, "refusals@example.com")
        half_built = _canvas(
            client,
            headers,
            project_id,
            {
                "schema": GRAPH_SCHEMA,
                "nodes": [node("v", "video_generation", source="connection")],
                "edges": [],
            },
        )
        refused = client.post(f"/v1/workflows/{half_built['id']}/runs", headers=headers, json={})
        assert refused.status_code == 422
        errors = refused.json()["detail"]["errors"]
        assert {(item["node_id"], item["param"]) for item in errors} >= {("v", "prompt"), ("v", "model")}

        connection_id = _mock_connection(container, client, headers, workspace_id)
        workflow = _canvas(client, headers, project_id, _story_graph(connection_id, with_video=False))
        stale = client.post(
            f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={"base_version": 99}
        )
        assert (
            stale.status_code == 409 and stale.json()["detail"]["reason_code"] == "WORKFLOW_VERSION_CONFLICT"
        )
        unknown = client.post(
            f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={"node_ids": ["ghost"]}
        )
        assert (
            unknown.status_code == 409 and unknown.json()["detail"]["reason_code"] == "WORKFLOW_NODE_UNKNOWN"
        )
        notes_only = client.post(
            f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={"node_ids": ["note"]}
        )
        assert notes_only.status_code == 409

        first = client.post(f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={})
        assert first.status_code == 202
        duplicate = client.post(f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={})
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["reason_code"] == "WORKFLOW_RUN_ACTIVE"
        assert duplicate.json()["detail"]["run_id"] == first.json()["id"]


async def test_stopping_a_run_stops_its_nodes_and_its_jobs(container, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(connection_generation, "MOCK_VIDEO_RENDER_SECONDS", 3600.0)
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, project_id, workspace_id = _register(client, "stopper@example.com")
        connection_id = _mock_connection(container, client, headers, workspace_id)
        graph = {
            "schema": GRAPH_SCHEMA,
            "nodes": [
                node("text", "text", text="waves"),
                node(
                    "video",
                    "video_generation",
                    300,
                    source="connection",
                    connection_id=connection_id,
                    model="mock-video",
                ),
                node(
                    "after",
                    "llm",
                    600,
                    connection_id=connection_id,
                    model="mock-chat",
                    instruction="describe",
                ),
            ],
            "edges": [edge("e1", "text", "text", "video", "prompt")],
        }
        workflow = _canvas(client, headers, project_id, graph)
        run = client.post(f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={}).json()
        container.gateway.poll_interval_seconds = 0.01
        for _ in range(5):
            await advance_workflow_runs_once(container)
            for _job in range(5):
                await process_next_job(container)
            await asyncio.sleep(0.03)
        running = _nodes(container.workflow_runs.view(run["id"]))
        assert running["video"]["status"] == "RUNNING"
        job_id = running["video"]["generation_job_id"]

        stopped = client.post(f"/v1/workflow-runs/{run['id']}/cancel", headers=headers)
        assert stopped.status_code == 200, stopped.text
        view = stopped.json()
        assert view["status"] == "CANCELLED"
        assert _nodes(view)["video"]["status"] == "CANCELLED"
        with container.database.session() as session:
            assert session.get(GenerationJob, job_id).status == "CANCELLED"

        await advance_workflow_runs_once(container)
        assert container.workflow_runs.view(run["id"])["status"] == "CANCELLED", "a stopped run stays stopped"


async def test_a_run_of_a_deleted_canvas_is_stopped(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, project_id, workspace_id = _register(client, "deleted-canvas@example.com")
        connection_id = _mock_connection(container, client, headers, workspace_id)
        workflow = _canvas(client, headers, project_id, _story_graph(connection_id, with_video=False))
        run = client.post(f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={}).json()
        assert client.delete(f"/v1/workflows/{workflow['id']}", headers=headers).status_code == 204
        assert await advance_workflow_runs_once(container) == 0
        with container.database.session() as session:
            assert session.get(WorkflowRun, run["id"]).status == "CANCELLED"
            statuses = {
                row.node_id: row.status
                for row in session.scalars(select(WorkflowNodeRun).where(WorkflowNodeRun.run_id == run["id"]))
            }
        assert statuses["llm"] == "CANCELLED" and statuses["image"] == "CANCELLED"


# ---------------------------------------------------------------------------
# 5. Platform nodes
# ---------------------------------------------------------------------------


async def test_a_credit_paid_node_goes_through_admission_labelled_as_the_canvas(container, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(container.providers.get("openrouter"), "configured", True, raising=False)
    captured: dict[str, object] = {}

    def submit(command, **kwargs):  # type: ignore[no-untyped-def]
        captured["command"] = command
        captured["kwargs"] = kwargs
        with container.database.session() as session:
            session.add(
                GenerationJob(
                    id="platform-job-1",
                    project_id=command.project_id,
                    generation_type="video",
                    provider=command.provider,
                    model=command.model,
                    status="QUEUED",
                    request_json={},
                    request_hash="0" * 64,
                )
            )
        return SimpleNamespace(id="platform-job-1"), False

    monkeypatch.setattr(container.visual_runtime, "submit_passenger", submit)
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, project_id, workspace_id = _register(client, "platform-node@example.com")
        with container.database.session() as session:
            session.get(Workspace, workspace_id).plan_tier = "PRO"
        graph = {
            "schema": GRAPH_SCHEMA,
            "nodes": [
                node("text", "text", text="rain on a tin roof"),
                node(
                    "video",
                    "video_generation",
                    300,
                    source="platform",
                    platform_model="openrouter:google/veo-3.1",
                    duration=5,
                    aspect_ratio="16:9",
                    negative_prompt="text overlays",
                ),
            ],
            "edges": [edge("e1", "text", "text", "video", "prompt")],
        }
        workflow = _canvas(client, headers, project_id, graph)
        run = client.post(f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={}).json()
        await advance_workflow_runs_once(container)

    command = captured["command"]
    kwargs = captured["kwargs"]
    assert command.provider == "openrouter" and command.model == "google/veo-3.1"  # type: ignore[attr-defined]
    assert command.prompt == "rain on a tin roof"  # type: ignore[attr-defined]
    assert command.negative_prompt == "text overlays"  # type: ignore[attr-defined]
    assert command.duration == 6.0, "admission's execution length, as on the Create page"  # type: ignore[attr-defined]
    assert command.idempotency_key.startswith("canvas:")  # type: ignore[attr-defined]
    assert kwargs["mode"] == "CANVAS"  # type: ignore[index]
    assert kwargs["extra_metadata"]["workflow_node_id"] == "video"  # type: ignore[index]
    assert kwargs["estimated_credits"] >= 1, "the platform node is priced by admission"  # type: ignore[index]
    with container.database.session() as session:
        row = session.scalar(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.run_id == run["id"], WorkflowNodeRun.node_id == "video"
            )
        )
        assert row.status == "RUNNING" and row.generation_job_id == "platform-job-1"
        assert session.get(WorkflowRun, run["id"]).enforce_plan is True


async def test_a_platform_node_out_of_credits_fails_with_a_top_up_reason(container, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(container.providers.get("openrouter"), "configured", True, raising=False)

    def submit(command, **kwargs):  # type: ignore[no-untyped-def]
        raise InsufficientWorkspaceCredits("workspace has 0 credits; 44 required")

    monkeypatch.setattr(container.visual_runtime, "submit_passenger", submit)
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, project_id, workspace_id = _register(client, "no-credits@example.com")
        with container.database.session() as session:
            session.get(Workspace, workspace_id).plan_tier = "PRO"
        graph = {
            "schema": GRAPH_SCHEMA,
            "nodes": [
                node(
                    "video",
                    "video_generation",
                    source="platform",
                    platform_model="openrouter:google/veo-3.1",
                    prompt="rain",
                )
            ],
            "edges": [],
        }
        workflow = _canvas(client, headers, project_id, graph)
        run = client.post(f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={}).json()
        view = await _drive(container, run["id"], rounds=5)
    assert view["status"] == "FAILED"
    video = _nodes(view)["video"]
    assert video["status"] == "FAILED"
    assert video["error_code"] == "INSUFFICIENT_CREDITS"
    assert "0 credits" in video["error_message"]


def test_internal_advance_endpoint_requires_the_platform_key(container):  # type: ignore[no-untyped-def]
    with TestClient(create_app(container)) as client:
        assert client.post("/internal/maintenance/workflow-runs").status_code in {401, 403}
        allowed = client.post(
            "/internal/maintenance/workflow-runs", headers={"Authorization": "Bearer test-platform-key"}
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json() == {"advanced": 0}


async def test_independent_model_calls_run_side_by_side(container):  # type: ignore[no-untyped-def]
    from connection_core import ChatResult

    class SideBySide:
        def __init__(self) -> None:
            self.started = 0
            self.both = asyncio.Event()

        async def chat(self, **kwargs):  # type: ignore[no-untyped-def]
            self.started += 1
            if self.started == 2:
                self.both.set()
            # Sequential execution never starts the second call, so this waits out.
            await asyncio.wait_for(self.both.wait(), timeout=2)
            return ChatResult(
                text=f"answer from {kwargs['model']}", model=kwargs["model"], finish_reason="stop"
            )

    container.workflow_runs.connections = SideBySide()
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, project_id, _workspace_id = _register(client, "parallel@example.com")
        graph = {
            "schema": GRAPH_SCHEMA,
            "nodes": [
                node("text", "text", text="a harbour"),
                node("a", "llm", 300, connection_id="any", model="model-a", instruction="one"),
                node("b", "llm", 300, connection_id="any", model="model-b", instruction="two"),
            ],
            "edges": [edge("e1", "text", "text", "a", "prompt"), edge("e2", "text", "text", "b", "prompt")],
        }
        workflow = _canvas(client, headers, project_id, graph)
        run = client.post(f"/v1/workflows/{workflow['id']}/runs", headers=headers, json={}).json()
    view = await _drive(container, run["id"], rounds=10)
    assert view["status"] == "SUCCEEDED", view
    nodes = _nodes(view)
    assert nodes["a"]["outputs"]["text"] == "answer from model-a"
    assert nodes["b"]["outputs"]["text"] == "answer from model-b"
