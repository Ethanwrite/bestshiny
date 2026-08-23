from __future__ import annotations

from dataclasses import dataclass

from cost_core import CreditEstimate, CreditPricingEngine
from model_registry_core import ModelRole
from platform_contracts import GenerationRequest
from provider_sdk import AssetCriticality

from .runtime import ModelRoleRuntime
from .service import PlanEntitlementDenied, WorkspaceModelResolver, WorkspacePlanTier


@dataclass(frozen=True)
class AdmittedGeneration:
    request: GenerationRequest
    estimate: CreditEstimate
    model_role: str | None
    plan_tier: WorkspacePlanTier


class GenerationAdmissionService:
    """Turn a public generation intent into a server-owned executable request."""

    version = "generation-admission-v1"

    def __init__(
        self,
        workspace_models: WorkspaceModelResolver,
        model_roles: ModelRoleRuntime,
        pricing: CreditPricingEngine,
    ):
        self.workspace_models = workspace_models
        self.model_roles = model_roles
        self.pricing = pricing

    def admit_passenger(
        self,
        request: GenerationRequest,
        *,
        requested_role: str | None = None,
        resolution: str = "720p",
        enforce_plan: bool = True,
    ) -> AdmittedGeneration:
        context = self.workspace_models.context_for_project(request.project_id)
        admitted = request.model_copy(deep=True)
        role_value: str | None = None

        if context.workspace_id is not None and enforce_plan:
            if admitted.shot_id or admitted.candidate_id:
                raise PlanEntitlementDenied(
                    "shot and candidate generation must use the authenticated shot endpoint"
                )
            admitted.asset_criticality = AssetCriticality.STANDARD
            admitted.cost_estimate = 0.0
            admitted.metadata = {"mode": "PASSENGER_SEAT", "admission_version": self.version}
            if admitted.type == "video":
                role = (
                    ModelRole(requested_role)
                    if requested_role
                    else self.workspace_models.default_video_role(admitted.project_id)
                )
                selected, _capability, _implementation = self.model_roles.resolve(
                    admitted.project_id,
                    role,
                    asset_criticality=AssetCriticality.STANDARD,
                    require_live=False,
                )
                if selected.modality != "video":
                    raise ValueError(f"model role {role.value} does not provide video generation")
                admitted.provider = selected.provider
                admitted.model = selected.provider_model_id
                role_value = role.value
            elif context.plan_tier is WorkspacePlanTier.FREE:
                raise PlanEntitlementDenied(
                    "FREE image generation is unavailable until a server-configured image role is enabled"
                )
            else:
                # The image target is server-owned, resolved through the same
                # registry role as video rather than named here.
                image_role = ModelRole(requested_role) if requested_role else ModelRole.IMAGE_GENERATION
                selected, _capability, _implementation = self.model_roles.resolve(
                    admitted.project_id,
                    image_role,
                    asset_criticality=AssetCriticality.STANDARD,
                    require_live=False,
                )
                if selected.modality != "image":
                    raise ValueError(f"model role {image_role.value} does not provide image generation")
                admitted.provider = selected.provider
                admitted.model = selected.provider_model_id
                role_value = image_role.value

        try:
            estimate = self.pricing.estimate(
                provider=admitted.provider,
                model=admitted.model,
                media_type=admitted.type,
                duration=admitted.duration or 1,
                resolution=resolution,
                reference_count=len(admitted.reference_asset_ids),
                image_count=admitted.image_count,
            )
        except ValueError:
            if context.workspace_id is not None and enforce_plan:
                raise
            # Legacy development projects predate the commercial model and
            # pricing registries. Preserve their target-validation behavior;
            # scoped workspaces never receive this compatibility fallback.
            estimate = CreditEstimate(
                provider_cost_usd=0.0,
                resolution_multiplier=1.0,
                reference_multiplier=1.0,
                service_multiplier=1.0,
                estimated_total_usd=0.0,
                credits=max(1, admitted.image_count),
                usd_per_credit=self.pricing.usd_per_credit,
                image_count=max(1, admitted.image_count),
            )
        admitted.cost_estimate = estimate.estimated_total_usd
        return AdmittedGeneration(admitted, estimate, role_value, context.plan_tier)

    def admit_autopilot(
        self,
        request: GenerationRequest,
        *,
        resolution: str = "720p",
        enforce_plan: bool = True,
    ) -> AdmittedGeneration:
        """Price an internal shot request and enforce the workspace video plan."""

        if request.type != "video" or not request.shot_id or not request.candidate_id:
            raise ValueError("autopilot admission requires a candidate-bound video request")
        context = self.workspace_models.context_for_project(request.project_id)
        admitted = request.model_copy(deep=True)
        role_value: str | None = None
        if enforce_plan and context.workspace_id is not None and context.plan_tier is WorkspacePlanTier.FREE:
            role = ModelRole.VIDEO_SEEDANCE
            selected, _capability, _implementation = self.model_roles.resolve(
                admitted.project_id,
                role,
                asset_criticality=AssetCriticality.STANDARD,
                require_live=False,
            )
            if (admitted.provider, admitted.model) != (
                selected.provider,
                selected.provider_model_id,
            ):
                # The Adapter payload was compiled for the target the router
                # picked. Re-routing the plan invalidates it, so it is dropped
                # rather than carried onto a different model's transport.
                admitted.provider_payload = {}
            admitted.provider = selected.provider
            admitted.model = selected.provider_model_id
            admitted.asset_criticality = AssetCriticality.STANDARD
            role_value = role.value
        estimate = self.pricing.estimate(
            provider=admitted.provider,
            model=admitted.model,
            media_type="video",
            duration=admitted.duration or 1,
            resolution=resolution,
            reference_count=len(admitted.reference_asset_ids),
        )
        admitted.cost_estimate = estimate.estimated_total_usd
        admitted.metadata = {
            **admitted.metadata,
            "admission_version": self.version,
            "model_role": role_value,
        }
        return AdmittedGeneration(admitted, estimate, role_value, context.plan_tier)


__all__ = ["AdmittedGeneration", "GenerationAdmissionService"]
