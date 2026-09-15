"""Routes for workspace connections (a workspace's own API keys) and canvas workflows.

Connections are workspace secrets: any member may see that one exists and use
it, only an owner or admin may add, check, change or remove one, and no route
ever returns a key. Canvases belong to a project: readers may view them, editors may
save and run them. A run executes in the worker; these routes only start,
observe and stop it.
"""

from __future__ import annotations

from typing import Any

from connection_core import (
    ConnectionInvalid,
    ConnectionModelInput,
    ConnectionNotFound,
    ConnectionUnavailable,
    ProviderCallFailed,
)
from fastapi import Depends, FastAPI, HTTPException, Response
from production_domain.models import Workspace
from pydantic import BaseModel, Field
from workflow_core import (
    WorkflowConflict,
    WorkflowGraphInvalid,
    WorkflowNotFound,
    catalog_view,
)

from .auth import ROLE_RANK, AuthPrincipal, AuthService
from .container import Container


class ConnectionModelBody(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    capability: str = Field(min_length=1, max_length=20)


class ConnectionCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    protocol: str = Field(min_length=1, max_length=40)
    # Deliberately unconstrained here: a pydantic length error would echo the
    # submitted key back in the 422 body. The service validates it instead.
    api_key: str = ""
    base_url: str | None = Field(default=None, max_length=500)
    capabilities: list[str] | None = Field(default=None, max_length=3)
    models: list[ConnectionModelBody] | None = Field(default=None, max_length=100)


class ConnectionUpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    api_key: str | None = None
    base_url: str | None = Field(default=None, max_length=500)
    capabilities: list[str] | None = Field(default=None, max_length=3)
    models: list[ConnectionModelBody] | None = Field(default=None, max_length=100)


class WorkflowCreateBody(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    graph: dict[str, Any] | None = None


class WorkflowSaveBody(BaseModel):
    base_version: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=200)
    graph: dict[str, Any] | None = None


class WorkflowRunBody(BaseModel):
    base_version: int | None = Field(default=None, ge=1)
    node_ids: list[str] | None = Field(default=None, max_length=300)
    force: bool | None = None


def _models(body: list[ConnectionModelBody] | None) -> list[ConnectionModelInput] | None:
    if body is None:
        return None
    return [ConnectionModelInput(id=item.id, capability=item.capability) for item in body]


def register_canvas_routes(app: FastAPI, container: Container, auth: AuthService, verify_api_key) -> None:  # type: ignore[no-untyped-def]
    def _actor(principal: AuthPrincipal) -> str | None:
        return None if principal.development_bypass else principal.user_id

    def _workspace(
        principal: AuthPrincipal, workspace_id: str, *, write: bool = False, admin: bool = False
    ) -> str:
        role = auth.require_workspace(principal, workspace_id, write=write, admin=admin)
        if principal.development_bypass:
            # The bypass is granted every role, so existence is checked here.
            with container.database.session() as session:
                if session.get(Workspace, workspace_id) is None:
                    raise HTTPException(404, "workspace not found")
        return role

    def _connection_error(exc: Exception) -> HTTPException:
        if isinstance(exc, ConnectionNotFound):
            return HTTPException(404, "connection not found")
        if isinstance(exc, ConnectionInvalid):
            return HTTPException(422, exc.as_detail())
        if isinstance(exc, ConnectionUnavailable):
            return HTTPException(409, exc.as_detail())
        if isinstance(exc, ProviderCallFailed):
            return HTTPException(502, exc.as_detail())
        raise exc

    # ------------------------------------------------------------ connections

    @app.get("/v1/connections/protocols")
    def connection_protocols(_principal: AuthPrincipal = Depends(auth.current_user)) -> dict[str, Any]:
        return {"protocols": container.connections.protocols_view()}

    @app.get("/v1/workspaces/{workspace_id}/connections")
    def list_connections(
        workspace_id: str, principal: AuthPrincipal = Depends(auth.current_user)
    ) -> dict[str, Any]:
        role = _workspace(principal, workspace_id)
        return {
            "connections": [view.as_dict() for view in container.connections.list_connections(workspace_id)],
            "can_manage": ROLE_RANK.get(role, 0) >= ROLE_RANK["ADMIN"],
        }

    @app.post("/v1/workspaces/{workspace_id}/connections", status_code=201)
    def create_connection(
        workspace_id: str,
        body: ConnectionCreateBody,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        _workspace(principal, workspace_id, admin=True)
        try:
            view = container.connections.create_connection(
                workspace_id,
                user_id=_actor(principal),
                name=body.name,
                protocol=body.protocol,
                api_key=body.api_key,
                base_url=body.base_url,
                capabilities=body.capabilities,
                models=_models(body.models),
            )
        except (ConnectionInvalid, ConnectionUnavailable) as exc:
            raise _connection_error(exc) from exc
        return view.as_dict()

    @app.patch("/v1/workspaces/{workspace_id}/connections/{connection_id}")
    def update_connection(
        workspace_id: str,
        connection_id: str,
        body: ConnectionUpdateBody,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        _workspace(principal, workspace_id, admin=True)
        try:
            view = container.connections.update_connection(
                workspace_id,
                connection_id,
                name=body.name,
                base_url=body.base_url,
                api_key=body.api_key,
                capabilities=body.capabilities,
                models=_models(body.models),
            )
        except (ConnectionNotFound, ConnectionInvalid, ConnectionUnavailable) as exc:
            raise _connection_error(exc) from exc
        return view.as_dict()

    @app.delete("/v1/workspaces/{workspace_id}/connections/{connection_id}", status_code=204)
    def delete_connection(
        workspace_id: str,
        connection_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> Response:
        _workspace(principal, workspace_id, admin=True)
        try:
            container.connections.delete_connection(workspace_id, connection_id, user_id=_actor(principal))
        except ConnectionNotFound as exc:
            raise _connection_error(exc) from exc
        return Response(status_code=204)

    @app.post("/v1/workspaces/{workspace_id}/connections/{connection_id}/test")
    async def test_connection(
        workspace_id: str,
        connection_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        # Checking a key is managing the connection: the same owner-or-admin
        # rule as adding one, which is also what the editor offers it to.
        _workspace(principal, workspace_id, admin=True)
        try:
            return await container.connections.test_connection(workspace_id, connection_id)
        except (ConnectionNotFound, ConnectionInvalid, ConnectionUnavailable) as exc:
            raise _connection_error(exc) from exc

    @app.get("/v1/workspaces/{workspace_id}/connections/{connection_id}/remote-models")
    async def remote_models(
        workspace_id: str,
        connection_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        _workspace(principal, workspace_id, admin=True)
        try:
            models = await container.connections.list_remote_models(workspace_id, connection_id)
        except (ConnectionNotFound, ConnectionInvalid, ConnectionUnavailable, ProviderCallFailed) as exc:
            raise _connection_error(exc) from exc
        return {"models": [model.view() for model in models]}

    # ------------------------------------------------------------ workflows

    def _workflow_project(workflow_id: str, principal: AuthPrincipal, *, write: bool = False) -> str:
        try:
            project_id = container.workflows.project_id_for(workflow_id)
        except WorkflowNotFound as exc:
            raise HTTPException(404, "workflow not found") from exc
        auth.require_project(principal, project_id, write=write)
        return project_id

    def _run_project(run_id: str, principal: AuthPrincipal, *, write: bool = False) -> str:
        try:
            project_id = container.workflow_runs.project_id_for(run_id)
        except LookupError as exc:
            raise HTTPException(404, "workflow run not found") from exc
        auth.require_project(principal, project_id, write=write)
        return project_id

    @app.get("/v1/workflows/node-types")
    def workflow_node_types(_principal: AuthPrincipal = Depends(auth.current_user)) -> dict[str, Any]:
        return catalog_view()

    @app.get("/v1/projects/{project_id}/workflows")
    def list_workflows(
        project_id: str, principal: AuthPrincipal = Depends(auth.current_user)
    ) -> dict[str, Any]:
        auth.require_project(principal, project_id)
        return {"workflows": container.workflows.list_for_project(project_id)}

    @app.post("/v1/projects/{project_id}/workflows", status_code=201)
    def create_workflow(
        project_id: str,
        body: WorkflowCreateBody,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        auth.require_project(principal, project_id, write=True)
        try:
            return container.workflows.create(
                project_id, user_id=_actor(principal), name=body.name, graph=body.graph
            )
        except WorkflowGraphInvalid as exc:
            raise HTTPException(422, exc.as_detail()) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/workflows/{workflow_id}")
    def get_workflow(
        workflow_id: str, principal: AuthPrincipal = Depends(auth.current_user)
    ) -> dict[str, Any]:
        _workflow_project(workflow_id, principal)
        try:
            return container.workflows.view(workflow_id)
        except WorkflowNotFound as exc:
            raise HTTPException(404, "workflow not found") from exc

    @app.put("/v1/workflows/{workflow_id}")
    def save_workflow(
        workflow_id: str,
        body: WorkflowSaveBody,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        _workflow_project(workflow_id, principal, write=True)
        try:
            return container.workflows.save(
                workflow_id,
                user_id=_actor(principal),
                base_version=body.base_version,
                name=body.name,
                graph=body.graph,
            )
        except WorkflowNotFound as exc:
            raise HTTPException(404, "workflow not found") from exc
        except WorkflowConflict as exc:
            raise HTTPException(409, exc.as_detail()) from exc
        except WorkflowGraphInvalid as exc:
            raise HTTPException(422, exc.as_detail()) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/v1/workflows/{workflow_id}", status_code=204)
    def delete_workflow(workflow_id: str, principal: AuthPrincipal = Depends(auth.current_user)) -> Response:
        _workflow_project(workflow_id, principal, write=True)
        try:
            container.workflows.delete(workflow_id, user_id=_actor(principal))
        except WorkflowNotFound as exc:
            raise HTTPException(404, "workflow not found") from exc
        return Response(status_code=204)

    @app.post("/v1/workflows/{workflow_id}/runs", status_code=202)
    def start_workflow_run(
        workflow_id: str,
        body: WorkflowRunBody,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        _workflow_project(workflow_id, principal, write=True)
        try:
            return container.workflow_runs.start(
                workflow_id,
                user_id=_actor(principal),
                # A real user's plan gates the credit-paid nodes; the development
                # bypass keeps its latitude, as on every other generation route.
                enforce_plan=not principal.development_bypass,
                base_version=body.base_version,
                node_ids=body.node_ids,
                force=body.force,
            )
        except WorkflowNotFound as exc:
            raise HTTPException(404, "workflow not found") from exc
        except WorkflowConflict as exc:
            raise HTTPException(409, exc.as_detail()) from exc
        except WorkflowGraphInvalid as exc:
            raise HTTPException(422, exc.as_detail()) from exc

    @app.get("/v1/workflows/{workflow_id}/runs")
    def list_workflow_runs(
        workflow_id: str,
        limit: int = 20,
        principal: AuthPrincipal = Depends(auth.current_user),
    ) -> dict[str, Any]:
        _workflow_project(workflow_id, principal)
        return {"runs": container.workflow_runs.list_for_workflow(workflow_id, limit=limit)}

    @app.get("/v1/workflow-runs/{run_id}")
    def get_workflow_run(
        run_id: str, principal: AuthPrincipal = Depends(auth.current_user)
    ) -> dict[str, Any]:
        _run_project(run_id, principal)
        return container.workflow_runs.view(run_id)

    @app.post("/v1/workflow-runs/{run_id}/cancel")
    async def cancel_workflow_run(
        run_id: str, principal: AuthPrincipal = Depends(auth.current_user)
    ) -> dict[str, Any]:
        _run_project(run_id, principal, write=True)
        return await container.workflow_runs.cancel(run_id, user_id=_actor(principal))

    @app.post("/internal/maintenance/workflow-runs", dependencies=[Depends(verify_api_key)])
    async def advance_workflow_runs_endpoint(limit: int = 20) -> dict[str, int]:
        """Advance due canvas runs once - the operator face of the worker's loop."""

        return {"advanced": await container.workflow_runs.advance_due(limit=max(1, min(limit, 200)))}


__all__ = ["register_canvas_routes"]
