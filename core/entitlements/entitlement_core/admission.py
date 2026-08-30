from __future__ import annotations

from dataclasses import dataclass

from cost_core import CreditEstimate, CreditPricingEngine, PricingUnverified
from model_registry_core import ModelRole
from platform_contracts import GenerationRequest
from production_domain.models import (
    GenerationIdempotency,
    GenerationJob,
    JobStatus,
    Project,
)
from provider_sdk import AssetCriticality
from sqlalchemy import func, select

from .runtime import ModelRoleRuntime
from .service import (
    PlanEntitlementDenied,
    WorkspaceModelResolver,
    WorkspacePlanContext,
    WorkspacePlanTier,
)

# The public image-quality levels. The browser sends a tier name, never a
# model ID; the server owns this mapping, so a quote and its execution can
# only ever describe the same target. Plans are gates, not redirects: a tier
# the plan does not include is refused, never quietly replaced.
IMAGE_MODEL_TIERS: dict[str, tuple[str, frozenset[str]]] = {
    "shiny": ("seedream-5.0-ark", frozenset({"FREE", "PRO", "ENTERPRISE", "ALL"})),
    "shinier": ("flow-narwhal-image-internal", frozenset({"PRO", "ENTERPRISE", "ALL"})),
    "shiniest": ("gpt-image-2-openrouter", frozenset({"PRO", "ENTERPRISE", "ALL"})),
}


class ExplicitModelUnavailable(ValueError):
    """A named model cannot run, and substituting a different one is not allowed.

    Raised only for a caller-named provider/model. Automatic selection never
    raises this: it has a catalogue to choose from, so it either resolves or the
    role itself is unsatisfiable.
    """


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
        *,
        free_plan_max_images: int = 3,
    ):
        self.workspace_models = workspace_models
        self.model_roles = model_roles
        self.pricing = pricing
        self.free_plan_max_images = max(0, int(free_plan_max_images))

    def admit_passenger(
        self,
        request: GenerationRequest,
        *,
        requested_role: str | None = None,
        resolution: str = "720p",
        enforce_plan: bool = True,
        image_task: str = "auto",
        image_tier: str | None = None,
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
            if admitted.type == "image":
                admitted.metadata["image_task"] = image_task
                self._enforce_free_image_quota(context, admitted)

            named_provider = (request.provider or "").strip()
            named_model = (request.model or "").strip()

            if admitted.type == "image" and (named_provider or named_model):
                # Image targets are router-owned. Accepting a named image model
                # would reintroduce the case this contract exists to prevent: a
                # quote for one model and an execution on another.
                raise ExplicitModelUnavailable(
                    "image generation does not accept a named model; send a creative "
                    "task and let the router resolve the target"
                )
            if bool(named_provider) != bool(named_model):
                raise ExplicitModelUnavailable(
                    "provider and model must be given together; leave both empty to let "
                    "the platform choose automatically"
                )

            if named_provider:
                # Manual selection. The named model is the one that gets priced,
                # recorded on the job, submitted to the provider and billed — or
                # the request is refused. It is never quietly replaced, because a
                # caller who names a model is making a decision, not a request for
                # a suggestion.
                self._assert_named_model_usable(named_provider, named_model, admitted.type)
                self._assert_plan_allows_named_model(context, named_provider, named_model, admitted.type)
                admitted.provider = named_provider
                admitted.model = named_model
                admitted.metadata["model_selection"] = "MANUAL"
                role_value = None
            elif admitted.type == "image" and image_tier:
                # Tier selection. The browser names a public quality level; the
                # server maps it to the one model that tier means, gates it on
                # the plan, and that exact model is quoted, billed and run.
                tier_state = self._resolve_image_tier(context, image_tier)
                admitted.provider = tier_state.provider
                admitted.model = tier_state.provider_model_id
                admitted.metadata["model_selection"] = "TIER"
                admitted.metadata["image_model_tier"] = image_tier.strip().lower()
                role_value = None
            else:
                # Router-owned selection. For images this is the default path;
                # for video it is what "Auto" means. Resolution runs through the
                # workspace's plan-scoped catalogue, so a FREE workspace can only
                # ever land on the model its FREE binding names.
                admitted.metadata["model_selection"] = "ROUTER" if admitted.type == "image" else "AUTO"
                if admitted.type == "video":
                    role = (
                        ModelRole(requested_role)
                        if requested_role
                        else self.workspace_models.default_video_role(admitted.project_id)
                    )
                    modality = "video"
                else:
                    role = ModelRole(requested_role) if requested_role else ModelRole.IMAGE_GENERATION
                    modality = "image"
                selected, _capability, _implementation = self.model_roles.resolve(
                    admitted.project_id,
                    role,
                    asset_criticality=AssetCriticality.STANDARD,
                    require_live=False,
                )
                if selected.modality != modality:
                    raise ValueError(f"model role {role.value} does not provide {modality} generation")
                admitted.provider = selected.provider
                admitted.model = selected.provider_model_id
                role_value = role.value

        try:
            estimate = self.pricing.estimate(
                provider=admitted.provider,
                model=admitted.model,
                media_type=admitted.type,
                duration=admitted.duration or 1,
                resolution=resolution,
                reference_count=len(admitted.reference_asset_ids),
                image_count=admitted.image_count,
                generation_policy=admitted.generation_policy,
            )
        except PricingUnverified:
            # Never a compatibility case. The fallback below exists for requests
            # that predate pricing entirely, and it prices them at zero; routing
            # an unverified price into it would turn "we do not know what this
            # costs" into "this is free", which is the one answer that is
            # certainly wrong. It subclasses ValueError so callers keep their
            # 400, so it has to be caught ahead of the clause below.
            raise
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

    def _assert_named_model_usable(self, provider: str, model: str, media_type: str) -> None:
        """Refuse a named model the platform cannot run, rather than swapping it."""

        registry = self.pricing.registry
        try:
            profile = registry.get(model, provider)
        except LookupError as exc:
            raise ExplicitModelUnavailable(str(exc)) from exc
        if profile is None:
            raise ExplicitModelUnavailable(f"{provider} / {model} is not a known model")
        if profile.status == "disabled":
            raise ExplicitModelUnavailable(f"{provider} / {model} is disabled")
        if not registry.provider_enabled(provider):
            raise ExplicitModelUnavailable(
                f"provider {provider} is currently disabled by platform operations"
            )
        if profile.modality != media_type:
            raise ExplicitModelUnavailable(
                f"{provider} / {model} is a {profile.modality} model and cannot serve a "
                f"{media_type} request"
            )
        operation = f"{media_type}_generation"
        if operation not in profile.supported_operations:
            raise ExplicitModelUnavailable(f"{provider} / {model} does not support {operation}")

    def _resolve_image_tier(self, context: WorkspacePlanContext, image_tier: str):  # type: ignore[no-untyped-def]
        """Map a public image-quality tier to its server-owned model, or refuse."""

        tier = image_tier.strip().lower()
        entry = IMAGE_MODEL_TIERS.get(tier)
        if entry is None:
            raise ValueError(f"unknown image quality level: {image_tier[:40]}")
        logical_name, allowed_plans = entry
        if context.plan_tier.value not in allowed_plans:
            raise PlanEntitlementDenied(
                "this image quality level is part of the Pro plan; upgrade to use it"
            )
        state = self.workspace_models.models.runtime_model(logical_name)
        if not state.enabled:
            raise ExplicitModelUnavailable(
                "this image quality level is temporarily unavailable"
            )
        self._assert_named_model_usable(state.provider, state.provider_model_id, "image")
        return state

    def _enforce_free_image_quota(
        self, context: WorkspacePlanContext, request: GenerationRequest
    ) -> None:
        """The FREE plan's hard image budget, counted from the jobs table.

        A retry of an already-admitted submission (its idempotency key has a
        job) is not new spend and passes; everything else counts every image
        job of the workspace that was not refunded outright. Enforced
        server-side — the browser payload cannot widen it.
        """

        if context.plan_tier is not WorkspacePlanTier.FREE or context.workspace_id is None:
            return
        if request.image_count > 1:
            raise PlanEntitlementDenied("the Free plan generates one image per request")
        with self.workspace_models.database.session() as session:
            if request.idempotency_key:
                replay = session.scalar(
                    select(GenerationIdempotency.id).where(
                        GenerationIdempotency.project_id == request.project_id,
                        GenerationIdempotency.key == request.idempotency_key,
                    )
                )
                if replay is not None:
                    return
            used = session.scalar(
                select(func.count())
                .select_from(GenerationJob)
                .join(Project, Project.id == GenerationJob.project_id)
                .where(
                    Project.workspace_id == context.workspace_id,
                    GenerationJob.generation_type == "image",
                    GenerationJob.status.not_in(
                        (JobStatus.FAILED.value, JobStatus.CANCELLED.value)
                    ),
                )
            )
        if int(used or 0) >= self.free_plan_max_images:
            raise PlanEntitlementDenied(
                f"the Free plan includes {self.free_plan_max_images} images; "
                "upgrade to Pro to keep creating"
            )

    def _assert_plan_allows_named_model(
        self,
        context: WorkspacePlanContext,
        provider: str,
        model: str,
        media_type: str,
    ) -> None:
        """Plan limits deny a named model; they never redirect it to another one."""

        if context.plan_tier is not WorkspacePlanTier.FREE:
            return
        allowed, _capability, _implementation = self.model_roles.resolve(
            context.project_id,
            ModelRole.VIDEO_SEEDANCE,
            asset_criticality=AssetCriticality.STANDARD,
            require_live=False,
        )
        if provider != allowed.provider:
            raise PlanEntitlementDenied(
                f"FREE workspaces may only generate video on {allowed.provider}; "
                f"{provider} / {model} requires a paid plan"
            )

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
            generation_policy=admitted.generation_policy,
        )
        admitted.cost_estimate = estimate.estimated_total_usd
        admitted.metadata = {
            **admitted.metadata,
            "admission_version": self.version,
            "model_role": role_value,
        }
        return AdmittedGeneration(admitted, estimate, role_value, context.plan_tier)


__all__ = ["AdmittedGeneration", "GenerationAdmissionService"]
