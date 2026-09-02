from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from model_registry_core import ModelInfrastructureService, ModelRole, ResolvedModel
from platform_database import Database
from production_domain.models import Project, Workspace
from provider_sdk import AssetCriticality


class WorkspacePlanTier(StrEnum):
    FREE = "FREE"
    PRO = "PRO"
    ENTERPRISE = "ENTERPRISE"
    ALL = "ALL"


class PlanEntitlementDenied(PermissionError):
    pass


@dataclass(frozen=True)
class WorkspacePlanContext:
    workspace_id: str | None
    project_id: str
    plan_tier: WorkspacePlanTier


class WorkspaceModelResolver:
    """Resolve business roles through the workspace's server-owned plan tier."""

    def __init__(self, database: Database, models: ModelInfrastructureService):
        self.database = database
        self.models = models

    def context_for_project(self, project_id: str) -> WorkspacePlanContext:
        with self.database.session() as session:
            project = session.get(Project, project_id)
            if not project:
                raise LookupError("project not found")
            if not project.workspace_id:
                # Legacy/development projects retain the unscoped catalogue.
                return WorkspacePlanContext(None, project.id, WorkspacePlanTier.ALL)
            workspace = session.get(Workspace, project.workspace_id)
            if not workspace:
                raise LookupError("project workspace not found")
            if workspace.status != "ACTIVE" or project.status != "ACTIVE":
                raise PlanEntitlementDenied("workspace or project is not active")
            try:
                tier = WorkspacePlanTier(workspace.plan_tier)
            except ValueError as exc:
                raise PlanEntitlementDenied(
                    f"workspace has an unsupported plan tier: {workspace.plan_tier}"
                ) from exc
            return WorkspacePlanContext(workspace.id, project.id, tier)

    def resolve(
        self,
        project_id: str,
        role: ModelRole | str,
        *,
        asset_criticality: AssetCriticality | str = AssetCriticality.STANDARD,
        require_live: bool = False,
    ) -> ResolvedModel:
        context = self.context_for_project(project_id)
        requested_role = ModelRole(role)
        self._assert_role_allowed(context.plan_tier, requested_role)
        return self.models.resolve_role(
            requested_role,
            plan_tier=context.plan_tier.value,
            asset_criticality=asset_criticality,
            require_live=require_live,
        )

    def default_video_role(self, project_id: str) -> ModelRole:
        # What "Auto" means, for every plan. Deliberately the priced,
        # live-verified route: the previous paid default (VIDEO_FLOW's
        # flow-veo-3.1) carries no verified provider price, so every paid Auto
        # video died at the quote with a 400 — the server's own default failing
        # the server's own pricing gate. The context lookup stays so a missing
        # or inactive project keeps failing here exactly as it always has.
        self.context_for_project(project_id)
        return ModelRole.VIDEO_SEEDANCE

    @staticmethod
    def _assert_role_allowed(plan_tier: WorkspacePlanTier, role: ModelRole) -> None:
        # FREE may use every reasoning role that has a FREE-scoped binding, but
        # video execution is intentionally limited to Seedance. This check is
        # server-side; changing a browser payload cannot unlock another model.
        if (
            plan_tier is WorkspacePlanTier.FREE
            and role.value.startswith("VIDEO_")
            and role is not ModelRole.VIDEO_SEEDANCE
        ):
            raise PlanEntitlementDenied("FREE workspaces may use only the VIDEO_SEEDANCE generation role")
