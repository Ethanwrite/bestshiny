from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass, replace
from decimal import Decimal
from enum import Enum
from typing import Any

from cost_core import TokenCostEngine, TokenSettlement
from model_registry_core import (
    ModelRole,
    ResolvedModel,
    production_serviceable,
    record_role_canary_outcome,
)
from platform_database import Database
from production_domain.models import (
    DecisionRecord,
    ModelExecutionRecord,
    RetryCategory,
    RunAPIBenchmark,
)
from provider_sdk import (
    AssetCriticality,
    ChatCapability,
    EdgeTask,
    EdgeTaskRole,
    EmbeddingCapability,
    FactLockPromptRefiner,
    FactLockSet,
    PromptRefinementResult,
    ProviderCapability,
    ProviderCapabilityCatalog,
    ProviderError,
    ProviderMode,
    ProviderTrustViolation,
    assert_provider_can_handle,
)
from sqlalchemy import select

from .canary import (
    CanaryReservation,
    LiveCanaryConflict,
    LiveCanaryDenied,
    LiveCanaryPermitService,
    LiveSpendDenied,
)
from .production_budget import (
    FENCE_CANARY,
    FENCE_PRODUCTION,
    MODEL_ROLE_KIND,
    SOURCE_ESTIMATED_QUOTE,
    SOURCE_TOKENS_LIST,
    SOURCE_VERIFIED_PROVIDER,
    ProductionBudgetService,
    SpendAuthorizationDenied,
    SpendAuthorizationView,
)
from .service import WorkspaceModelResolver


@dataclass(frozen=True)
class LiveRoleFence:
    """What fenced one live role call: an operator's permit, the budget, or both.

    ``canary`` is set while the model has not earned ``VERIFIED_LIVE`` — the
    operator's permit is what authorizes the call. ``authorization`` is the
    quote-bound spend authorization under the platform breaker; it is set for
    every priced call once the budget is enabled, on both sides of the
    verification line, because the breaker bounds all live spend and not only
    the automatic part of it.
    """

    operation_key: str
    canary: CanaryReservation | None
    authorization: SpendAuthorizationView | None


@dataclass(frozen=True)
class ModelRoleExecution:
    resolved_model: ResolvedModel
    capability: ProviderCapability
    response: dict[str, Any]
    decision_record_id: str
    execution_record_id: str


def capability_for_model_role(role: ModelRole | str) -> ProviderCapability:
    requested = ModelRole(role)
    if requested in {ModelRole.MULTIMODAL_EMBEDDING, ModelRole.STYLE_SEMANTIC_EMBEDDING}:
        return ProviderCapability.EMBEDDINGS
    if requested.value.startswith("VIDEO_"):
        return ProviderCapability.VIDEO
    return ProviderCapability.CHAT


class ModelRoleRuntime:
    """Execute server-owned business roles through configured provider clients."""

    version = "model-role-runtime-v1"
    edge_pricing_version = "runapi-edge-pricing-v1"
    edge_prompt_refinement_cost_usd = Decimal("0.01")
    # The output-side token budget a live hold assumes when the caller sets no
    # explicit cap. It bounds a *reservation*, not the response: settlement
    # replaces it with the counted tokens. Generous on purpose — the observed
    # director turn ran a 719-token reasoning chain, and an undershooting hold
    # is the one direction a spending fence must not err in.
    chat_output_token_budget = 4096
    _edge_task_namespace = uuid.UUID("dc01347d-b4a2-5ac9-bc5d-f709bb4ef5fa")

    def __init__(
        self,
        database: Database,
        resolver: WorkspaceModelResolver,
        providers: ProviderCapabilityCatalog,
        *,
        provider_mode: ProviderMode | str = ProviderMode.MOCK,
        live_canary: LiveCanaryPermitService | None = None,
        token_costs: TokenCostEngine | None = None,
        production_budget: ProductionBudgetService | None = None,
    ):
        self.database = database
        self.resolver = resolver
        self.providers = providers
        try:
            self.provider_mode = ProviderMode(provider_mode)
        except ValueError as exc:
            raise ValueError("provider_mode must be mock, recorded, or live") from exc
        self.live_canary = live_canary
        self.token_costs = token_costs
        self.production_budget = production_budget

    def resolve(
        self,
        project_id: str,
        role: ModelRole | str,
        *,
        asset_criticality: AssetCriticality | str = AssetCriticality.STANDARD,
        require_live: bool = False,
    ) -> tuple[ResolvedModel, ProviderCapability, object]:
        requested_role = ModelRole(role)
        criticality = AssetCriticality(asset_criticality)
        server_requires_live = self.provider_mode is ProviderMode.LIVE
        selected = self.resolver.resolve(
            project_id,
            requested_role,
            asset_criticality=criticality,
            require_live=server_requires_live or require_live,
        )
        capability = capability_for_model_role(requested_role)
        implementation = self.providers.resolve(selected.provider, capability)
        assert_provider_can_handle(
            getattr(implementation, "trust_level", selected.provider_trust_level),
            criticality,
        )
        return selected, capability, implementation

    def _revalidate_execution_boundary(
        self,
        project_id: str,
        selected: ResolvedModel,
        *,
        criticality: AssetCriticality,
        require_live: bool,
    ) -> None:
        """Linearize the persisted model switch immediately before transport.

        Role resolution and transport execution deliberately remain separate,
        but an administrator may change a binding, runtime model ID, or live
        switch between those two operations.  Re-resolving here makes that
        change win before any provider coroutine is entered.  A change after
        this synchronous boundary is ordered after an already-authorized
        in-flight call and applies to subsequent calls.
        """

        current = self.resolver.resolve(
            project_id,
            selected.role,
            asset_criticality=criticality,
            require_live=self.provider_mode is ProviderMode.LIVE or require_live,
        )
        if current != selected:
            raise LookupError(
                "model role execution target changed before the provider boundary; resolve again"
            )

    async def execute_chat(
        self,
        project_id: str,
        role: ModelRole | str,
        *,
        messages: list[dict[str, Any]],
        asset_criticality: AssetCriticality | str = AssetCriticality.STANDARD,
        parameters: dict[str, Any] | None = None,
        require_live: bool = False,
    ) -> ModelRoleExecution:
        requested_role = ModelRole(role)
        criticality = AssetCriticality(asset_criticality)
        selected, capability, implementation = self.resolve(
            project_id,
            requested_role,
            asset_criticality=criticality,
            require_live=require_live,
        )
        if capability is not ProviderCapability.CHAT or not isinstance(implementation, ChatCapability):
            raise TypeError(f"model role {requested_role.value} is not executable as chat")
        request_hash = _execution_request_hash(
            requested_role,
            {"messages": messages, "parameters": parameters or {}},
        )
        started = time.perf_counter()
        fence: LiveRoleFence | None = None
        boundary_crossed = False
        try:
            self._revalidate_execution_boundary(
                project_id,
                selected,
                criticality=criticality,
                require_live=require_live,
            )
            fence = self._reserve_live_fence(
                selected,
                project_id=project_id,
                estimated_cost=self._planning_estimate(
                    selected,
                    parameters,
                    input_characters=len(
                        json.dumps(_json_safe(messages), ensure_ascii=False)
                    ),
                    max_output_tokens=_output_token_cap(
                        parameters, default=self.chat_output_token_budget
                    ),
                ),
            )
            if fence is not None:
                self._prepare_live_boundary(fence, request_hash=request_hash)
                boundary_crossed = True
            response = await implementation.chat(
                model=selected.provider_model_id,
                messages=messages,
                parameters=parameters,
            )
            self._settle_live_fence(fence, response=response, request_hash=request_hash)
        except Exception as exc:
            self._release_pre_boundary_fence(fence, crossed=boundary_crossed)
            self._record(
                project_id=project_id,
                selected=selected,
                capability=capability,
                criticality=criticality,
                input_count=len(messages),
                outcome="FAILED",
                reason_codes=["ROLE_RESOLVED", "CAPABILITY_CONFIGURED", type(exc).__name__],
                request_hash=request_hash,
                latency_ms=(time.perf_counter() - started) * 1000,
                parameters=parameters,
                error_code=getattr(exc, "code", type(exc).__name__),
                fence=fence,
            )
            raise
        decision_id, execution_id = self._record(
            project_id=project_id,
            selected=selected,
            capability=capability,
            criticality=criticality,
            input_count=len(messages),
            outcome="SUCCEEDED",
            reason_codes=["ROLE_RESOLVED", "CAPABILITY_CONFIGURED", "CALL_SUCCEEDED"],
            request_hash=request_hash,
            latency_ms=(time.perf_counter() - started) * 1000,
            parameters=parameters,
            response=response,
            fence=fence,
        )
        return ModelRoleExecution(selected, capability, response, decision_id, execution_id)

    async def execute_embeddings(
        self,
        project_id: str,
        *,
        inputs: str | list[str] | list[dict[str, Any]],
        role: ModelRole | str = ModelRole.MULTIMODAL_EMBEDDING,
        asset_criticality: AssetCriticality | str = AssetCriticality.STANDARD,
        parameters: dict[str, Any] | None = None,
        require_live: bool = False,
    ) -> ModelRoleExecution:
        requested_role = ModelRole(role)
        criticality = AssetCriticality(asset_criticality)
        selected, capability, implementation = self.resolve(
            project_id,
            requested_role,
            asset_criticality=criticality,
            require_live=require_live,
        )
        if capability is not ProviderCapability.EMBEDDINGS or not isinstance(
            implementation, EmbeddingCapability
        ):
            raise TypeError(f"model role {requested_role.value} is not executable as embeddings")
        input_count = len(inputs) if isinstance(inputs, list) else 1
        request_hash = _execution_request_hash(
            requested_role,
            {"inputs": inputs, "parameters": parameters or {}},
        )
        started = time.perf_counter()
        fence: LiveRoleFence | None = None
        boundary_crossed = False
        try:
            self._revalidate_execution_boundary(
                project_id,
                selected,
                criticality=criticality,
                require_live=require_live,
            )
            fence = self._reserve_live_fence(
                selected,
                project_id=project_id,
                estimated_cost=self._planning_estimate(
                    selected,
                    parameters,
                    input_characters=len(json.dumps(_json_safe(inputs), ensure_ascii=False)),
                    # Embedding output is not billed per token anywhere this
                    # platform prices; the hold covers the input side only.
                    max_output_tokens=0,
                ),
            )
            if fence is not None:
                self._prepare_live_boundary(fence, request_hash=request_hash)
                boundary_crossed = True
            response = await implementation.create_embeddings(
                model=selected.provider_model_id,
                inputs=inputs,
                parameters=parameters,
            )
            self._settle_live_fence(fence, response=response, request_hash=request_hash)
        except Exception as exc:
            self._release_pre_boundary_fence(fence, crossed=boundary_crossed)
            self._record(
                project_id=project_id,
                selected=selected,
                capability=capability,
                criticality=criticality,
                input_count=input_count,
                outcome="FAILED",
                reason_codes=["ROLE_RESOLVED", "CAPABILITY_CONFIGURED", type(exc).__name__],
                request_hash=request_hash,
                latency_ms=(time.perf_counter() - started) * 1000,
                parameters=parameters,
                error_code=getattr(exc, "code", type(exc).__name__),
                fence=fence,
            )
            raise
        decision_id, execution_id = self._record(
            project_id=project_id,
            selected=selected,
            capability=capability,
            criticality=criticality,
            input_count=input_count,
            outcome="SUCCEEDED",
            reason_codes=["ROLE_RESOLVED", "CAPABILITY_CONFIGURED", "CALL_SUCCEEDED"],
            request_hash=request_hash,
            latency_ms=(time.perf_counter() - started) * 1000,
            parameters=parameters,
            response=response,
            fence=fence,
        )
        return ModelRoleExecution(selected, capability, response, decision_id, execution_id)

    async def refine_prompt(
        self,
        project_id: str,
        *,
        original_prompt: str,
        fact_locks: FactLockSet,
        task_id: str | None = None,
        estimated_cost_usd: Decimal | str | None = None,
        require_live: bool = False,
    ) -> PromptRefinementResult:
        """Try the budgeted edge refiner, then validate/fallback to OpenRouter."""

        # The legacy arguments remain source-compatible but are intentionally
        # ignored. Identity and price are derived from server-owned inputs so a
        # caller cannot understate RunAPI budget usage or choose a reusable ID.
        _ = (task_id, estimated_cost_usd)
        scope = self.resolver.context_for_project(project_id)
        task_digest = hashlib.sha256(
            json.dumps(
                {
                    "workspace_id": scope.workspace_id,
                    "project_id": project_id,
                    "role": EdgeTaskRole.PROMPT_DRAFT_REFINEMENT.value,
                    "original_prompt_sha256": hashlib.sha256(original_prompt.encode("utf-8")).hexdigest(),
                    "fact_locks": fact_locks.immutable_facts,
                    "pricing_version": self.edge_pricing_version,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        server_task_id = str(uuid.uuid5(self._edge_task_namespace, task_digest))
        server_estimate = self.edge_prompt_refinement_cost_usd
        server_task = EdgeTask(
            task_id=server_task_id,
            role=EdgeTaskRole.PROMPT_DRAFT_REFINEMENT,
            asset_criticality=AssetCriticality.EDGE,
            estimated_cost_usd=server_estimate,
        )
        primary_execution: ModelRoleExecution | None = None

        async def generate_draft(payload: dict[str, Any]) -> dict[str, Any]:
            nonlocal primary_execution
            primary_execution = await self.execute_chat(
                project_id,
                ModelRole.PROMPT_REFINER_LOW_COST,
                messages=_refinement_messages(payload),
                asset_criticality=AssetCriticality.EDGE,
                parameters={
                    "response_format": {"type": "json_object"},
                    "_edge_task": server_task,
                },
                require_live=require_live,
            )
            return _chat_json(primary_execution.response)

        async def generate_fallback(prompt: str, locks: FactLockSet) -> dict[str, Any]:
            payload = {
                "instruction": (
                    "Improve professional cinematic wording only. Return JSON with refined_prompt "
                    "and immutable_facts. Preserve immutable facts exactly."
                ),
                "original_prompt": prompt,
                "immutable_facts": locks.immutable_facts,
            }
            execution = await self.execute_chat(
                project_id,
                ModelRole.PROMPT_REFINER_FALLBACK,
                messages=_refinement_messages(payload),
                asset_criticality=AssetCriticality.EDGE,
                parameters={"response_format": {"type": "json_object"}},
                require_live=require_live,
            )
            return _chat_json(execution.response)

        refiner = FactLockPromptRefiner(generate_draft, fallback_generator=generate_fallback)
        primary_unavailable = False
        try:
            result = await refiner.refine(original_prompt=original_prompt, fact_locks=fact_locks)
        except (
            # A refused live-spend reservation — a missing permit or a tripped
            # production breaker — is a budget refusal, not a platform fault.
            # The director's turn got this degradation in #25; refine kept
            # 500ing on the same denial on production — each model call here
            # must degrade the same way.
            LiveCanaryConflict,
            LiveSpendDenied,
            LookupError,
            ProviderError,
            ProviderTrustViolation,
        ):
            primary_unavailable = True

            async def fallback_as_primary(_payload: dict[str, Any]) -> dict[str, Any]:
                return await generate_fallback(original_prompt, fact_locks)

            try:
                result = await FactLockPromptRefiner(fallback_as_primary).refine(
                    original_prompt=original_prompt,
                    fact_locks=fact_locks,
                )
                result = replace(
                    result,
                    source="fallback",
                    reason_codes=("PRIMARY_UNAVAILABLE", *result.reason_codes),
                )
            except (
                LiveCanaryConflict,
                LiveSpendDenied,
                LookupError,
                ProviderError,
                ProviderTrustViolation,
            ):
                # A model outage must never make the deterministic product
                # prompt path unavailable.  Keeping the already-approved input
                # is the only safe fallback; it is explicitly recorded as an
                # unoptimized candidate, not as a successful model execution.
                result = PromptRefinementResult(
                    original_prompt=original_prompt,
                    optimized_candidate=original_prompt,
                    accepted=False,
                    source="local_safe_fallback",
                    reason_codes=("PRIMARY_UNAVAILABLE", "FALLBACK_UNAVAILABLE"),
                    diff="",
                )

        self._record_refinement(
            project_id=project_id,
            result=result,
            primary_unavailable=primary_unavailable,
            task_id=server_task_id,
            original_prompt=original_prompt,
            primary_execution=primary_execution,
        )
        return result

    def _record(
        self,
        *,
        project_id: str,
        selected: ResolvedModel,
        capability: ProviderCapability,
        criticality: AssetCriticality,
        input_count: int,
        outcome: str,
        reason_codes: list[str],
        request_hash: str,
        latency_ms: float,
        parameters: dict[str, Any] | None,
        response: dict[str, Any] | None = None,
        error_code: str | None = None,
        fence: LiveRoleFence | None = None,
    ) -> tuple[str, str]:
        canary_usage_id = fence.canary.usage_id if fence is not None and fence.canary is not None else None
        spend_authorization_id = (
            fence.authorization.id if fence is not None and fence.authorization is not None else None
        )
        usage = response.get("usage") if response and isinstance(response.get("usage"), dict) else {}
        reported_actual_cost = _actual_cost(response or {})
        actual_cost = reported_actual_cost if self.provider_mode is ProviderMode.LIVE else None
        estimated_cost = _estimated_cost(parameters or {})
        token_priced: TokenSettlement | None = None
        if actual_cost is None and self.provider_mode is ProviderMode.LIVE and response:
            token_priced = self._token_settlement(
                selected.provider, selected.provider_model_id, response
            )
        if actual_cost is not None:
            cost_source = "VERIFIED_PROVIDER"
        elif token_priced is not None:
            # The provider reported usage counts — tokens, pixels, or both —
            # and no cost. Priced here from the dated canonical list rates: a
            # real figure with a weaker provenance than a provider invoice,
            # and it says which it is.
            #
            # "TOKENS_LIST" is deliberately not a `BillingEvidenceSource`
            # member. That enum describes evidence about a *provider invoice*;
            # this is the vendor's official list price applied to the
            # provider's own reported usage, which is a different claim, and
            # collapsing it into VERIFIED_PROVIDER would overstate it while
            # ESTIMATED would understate it. The string is the shared vocabulary
            # of `production_budget.SOURCE_TOKENS_LIST` and
            # `live_canary._ROLE_SETTLEMENT_SOURCES`, both of which reject any
            # source they do not know, so it stays exactly as written here.
            actual_cost = token_priced.cost_usd
            cost_source = "TOKENS_LIST"
        elif estimated_cost is not None:
            cost_source = "ESTIMATED"
        else:
            cost_source = "UNKNOWN"
        with self.database.session() as session:
            execution = ModelExecutionRecord(
                project_id=project_id,
                role=selected.role.value,
                model_definition_id=selected.definition_id,
                provider=selected.provider,
                provider_model_id=selected.provider_model_id,
                request_hash=request_hash,
                latency_ms=max(0.0, latency_ms),
                token_usage_json=_json_safe(usage),
                estimated_cost_usd=estimated_cost,
                actual_cost_usd=actual_cost,
                cost_source=cost_source,
                status=outcome,
                error_code=error_code,
                metadata_json={
                    "capability": capability.value,
                    "asset_criticality": criticality.value,
                    "input_count": input_count,
                    "live_canary_usage_id": canary_usage_id,
                    "spend_authorization_id": spend_authorization_id,
                    "live_fence": (
                        None
                        if fence is None
                        else (FENCE_CANARY if fence.canary is not None else FENCE_PRODUCTION)
                    ),
                    "provider_mode": self.provider_mode.value,
                    "reported_actual_cost_ignored": (
                        reported_actual_cost is not None and self.provider_mode is not ProviderMode.LIVE
                    ),
                    **(
                        {
                            "token_pricing_detail": token_priced.detail,
                            # A multimodal call is billed on pixels as well as
                            # tokens, so the pixel counts travel beside the
                            # line an operator reads.
                            "token_pricing_image_pixels": token_priced.image_pixels,
                            "token_pricing_video_pixels": token_priced.video_pixels,
                        }
                        if token_priced is not None
                        else {}
                    ),
                    **(
                        {
                            "evidence_purpose": (parameters or {}).get("evidence_purpose"),
                            "evidence_asset_id": (parameters or {}).get("evidence_asset_id"),
                        }
                        if selected.role is ModelRole.VLM_REVIEWER
                        else {}
                    ),
                },
            )
            record = DecisionRecord(
                project_id=project_id,
                decision_type="MODEL_ROLE_EXECUTION",
                input_features={
                    "role": selected.role.value,
                    "plan_tier": selected.plan_tier,
                    "asset_criticality": criticality.value,
                    "capability": capability.value,
                    "input_count": input_count,
                    "outcome": outcome,
                    "live_canary_usage_id": canary_usage_id,
                    "spend_authorization_id": spend_authorization_id,
                },
                selected_action=(f"{selected.provider}:{selected.provider_model_id}:{capability.value}"),
                reason_codes=reason_codes,
                model_version=selected.logical_name,
                policy_version=self.version,
            )
            session.add_all([execution, record])
            session.flush()
            return record.id, execution.id

    def _reserve_live_fence(
        self,
        selected: ResolvedModel,
        *,
        project_id: str,
        estimated_cost: Decimal | None,
    ) -> LiveRoleFence | None:
        """Choose the fence for one live role call.

        A serviceable model — enabled, live-enabled, not blocked — runs on its
        own token-priced authorization under the platform breaker, with no
        operator permit. The permit is the fence only where the budget does
        not reach: the budget disabled, or a call the platform cannot price
        (no token rates), which holds the permit's whole remaining budget —
        the conservative shape it always had. When both apply, the breaker is
        reserved for the permit-fenced call as well, so the platform ceiling
        bounds canary spend too.
        """

        if self.provider_mode is not ProviderMode.LIVE:
            return None
        operation_key = f"model-role:{uuid.uuid4().hex}"
        budget = self.production_budget
        authorization: SpendAuthorizationView | None = None
        if budget is not None and budget.enabled and estimated_cost is not None:
            # Resolution in live mode already required `enabled` and
            # `live_enabled`; the lifecycle travels on the resolved model.
            serviceable = production_serviceable(
                enabled=True,
                live_enabled=True,
                lifecycle_status=selected.lifecycle_status,
            )
            try:
                authorization = budget.authorize_operation(
                    operation_key=operation_key,
                    provider=selected.provider,
                    model=selected.provider_model_id,
                    max_cost_usd=estimated_cost,
                    kind=MODEL_ROLE_KIND,
                    model_role=selected.role.value,
                    project_id=project_id,
                )
            except SpendAuthorizationDenied:
                # A zero estimate: nothing to authorize automatically.
                authorization = None
            if authorization is not None and serviceable:
                return LiveRoleFence(operation_key=operation_key, canary=None, authorization=authorization)
        if self.live_canary is None:
            self._release_authorization(authorization, evidence="no-permit-service")
            raise LiveCanaryDenied("live model execution requires a durable LiveCanaryPermit")
        try:
            canary = self.live_canary.reserve_matching(
                provider=selected.provider,
                model=selected.provider_model_id,
                estimated_cost_usd=estimated_cost,
                idempotency_key=operation_key,
            )
        except BaseException:
            self._release_authorization(authorization, evidence="permit-refused")
            raise
        return LiveRoleFence(operation_key=operation_key, canary=canary, authorization=authorization)

    def _release_authorization(self, authorization: SpendAuthorizationView | None, *, evidence: str) -> None:
        if authorization is None or self.production_budget is None:
            return
        try:
            self.production_budget.release_pre_boundary(authorization.id, evidence_reference=evidence)
        except Exception:
            pass

    def _prepare_live_boundary(self, fence: LiveRoleFence, *, request_hash: str) -> None:
        evidence = f"model-execution-boundary:{request_hash}"
        if fence.canary is not None:
            if self.live_canary is None:  # pragma: no cover - constructor invariant.
                raise RuntimeError("live canary service disappeared")
            self.live_canary.mark_uncertain(fence.canary.usage_id, evidence_reference=evidence)
        if fence.authorization is not None:
            if self.production_budget is None:  # pragma: no cover - constructor invariant.
                raise RuntimeError("production budget service disappeared")
            self.production_budget.prepare_boundary(
                fence.authorization.id,
                provider=fence.authorization.provider,
                model=fence.authorization.model,
                fence=FENCE_CANARY if fence.canary is not None else FENCE_PRODUCTION,
                evidence_reference=evidence,
            )

    def _settle_live_fence(
        self,
        fence: LiveRoleFence | None,
        *,
        response: dict[str, Any],
        request_hash: str,
    ) -> None:
        if fence is None:
            return
        provider = fence.canary.provider if fence.canary is not None else fence.authorization.provider  # type: ignore[union-attr]
        model = fence.canary.model if fence.canary is not None else fence.authorization.model  # type: ignore[union-attr]
        actual = _actual_cost(response)
        source = SOURCE_VERIFIED_PROVIDER
        evidence = f"model-execution:{request_hash}"
        if actual is None:
            # Token-billing providers report counts, never a cost figure. A
            # usage that cannot settle keeps its whole hold and the permit
            # stays EXHAUSTED, so the counted tokens are priced at the same
            # canonical list rates every quote uses. No rates, no counts — the
            # usage stays UNCERTAIN for an operator, exactly as before.
            settlement = self._token_settlement(provider, model, response)
            if settlement is not None:
                actual = settlement.cost_usd
                source = SOURCE_TOKENS_LIST
                evidence = f"model-execution-tokens:{request_hash}:{settlement.detail}"
        if fence.canary is not None and actual is not None:
            if self.live_canary is None:  # pragma: no cover - constructor invariant.
                raise RuntimeError("live canary service disappeared")
            self.live_canary.settle(
                fence.canary.usage_id,
                actual_cost_usd=actual,
                evidence_reference=evidence,
            )
        if actual is not None:
            # A live call that closed its loop at a checkable figure is
            # evidence about the model under either fence: lifecycle promotion
            # and routing read it. It gates nothing.
            record_role_canary_outcome(
                self.database,
                provider=provider,
                model=model,
                cost_usd=actual,
                cost_source=source,
                evidence_reference=evidence,
            )
        if fence.authorization is not None:
            if self.production_budget is None:  # pragma: no cover - constructor invariant.
                raise RuntimeError("production budget service disappeared")
            self.production_budget.settle(
                fence.authorization.id,
                actual_cost_usd=actual,
                evidence_reference=evidence,
                source=source if actual is not None else SOURCE_ESTIMATED_QUOTE,
            )

    def _planning_estimate(
        self,
        selected: ResolvedModel,
        parameters: dict[str, Any] | None,
        *,
        input_characters: int,
        max_output_tokens: int,
    ) -> Decimal | None:
        explicit = _estimated_cost(parameters or {})
        if explicit is not None:
            return explicit
        if self.provider_mode is not ProviderMode.LIVE or self.token_costs is None:
            return None
        return self.token_costs.estimate_call(
            selected.provider,
            selected.provider_model_id,
            input_characters=input_characters,
            max_output_tokens=max_output_tokens,
        )

    def _token_settlement(
        self,
        provider: str,
        model: str,
        response: dict[str, Any],
    ) -> TokenSettlement | None:
        if self.token_costs is None:
            return None
        usage = response.get("usage")
        if not isinstance(usage, dict) or not usage:
            return None
        return self.token_costs.settle_from_usage(provider, model, usage)

    def _release_pre_boundary_fence(
        self,
        fence: LiveRoleFence | None,
        *,
        crossed: bool,
    ) -> None:
        if fence is None or crossed:
            return
        if fence.canary is not None and self.live_canary is not None:
            self.live_canary.release(fence.canary.usage_id)
        self._release_authorization(fence.authorization, evidence="model-execution-pre-boundary-release")

    def _record_refinement(
        self,
        *,
        project_id: str,
        result: PromptRefinementResult,
        primary_unavailable: bool,
        task_id: str,
        original_prompt: str,
        primary_execution: ModelRoleExecution | None,
    ) -> None:
        with self.database.session() as session:
            session.add(
                DecisionRecord(
                    project_id=project_id,
                    decision_type="FACT_LOCK_PROMPT_REFINEMENT",
                    input_features={
                        "accepted": result.accepted,
                        "source": result.source,
                        "primary_unavailable": primary_unavailable,
                    },
                    selected_action=result.source,
                    reason_codes=list(result.reason_codes) or ["FACT_LOCKS_PRESERVED", "CANDIDATE_ACCEPTED"],
                    model_version=self.version,
                    policy_version="fact-lock-v1",
                )
            )
            if primary_execution is not None and primary_execution.resolved_model.provider == "runapi":
                execution = session.get(
                    ModelExecutionRecord,
                    primary_execution.execution_record_id,
                )
                existing = session.scalar(select(RunAPIBenchmark).where(RunAPIBenchmark.task_id == task_id))
                if execution is not None and existing is None:
                    session.add(
                        RunAPIBenchmark(
                            task_id=task_id,
                            task_type=EdgeTaskRole.PROMPT_DRAFT_REFINEMENT.value,
                            input_hash=hashlib.sha256(original_prompt.encode("utf-8")).hexdigest(),
                            output_quality=None,
                            fact_lock_pass=result.source == "primary" and result.accepted,
                            fallback_required=result.source != "primary",
                            latency_ms=execution.latency_ms,
                            actual_cost_usd=execution.actual_cost_usd,
                            user_acceptance=None,
                            metadata_json={
                                "model_execution_record_id": execution.id,
                                "provider": execution.provider,
                                "model": execution.provider_model_id,
                                "result_source": result.source,
                                "reason_codes": list(result.reason_codes),
                                "pricing_version": self.edge_pricing_version,
                            },
                        )
                    )


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _execution_request_hash(role: ModelRole, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"role": role.value, "payload": _json_safe(payload)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return parsed.quantize(Decimal("0.000001")) if parsed >= 0 else None


def _actual_cost(response: dict[str, Any]) -> Decimal | None:
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    for value in (usage.get("cost"), usage.get("total_cost"), response.get("cost")):
        parsed = _money(value)
        if parsed is not None:
            return parsed
    return None


def _estimated_cost(parameters: dict[str, Any]) -> Decimal | None:
    task = parameters.get("_edge_task")
    if isinstance(task, EdgeTask):
        return _money(task.estimated_cost_usd)
    return _money(parameters.get("estimated_cost_usd"))


def _output_token_cap(parameters: dict[str, Any] | None, *, default: int) -> int:
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = (parameters or {}).get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return default


def _refinement_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                "Return one strict JSON object. Preserve immutable_facts exactly and only improve "
                "cinematic wording. Never replace the approved source prompt."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def _chat_json(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderError(
            "chat response has no choices",
            RetryCategory.PERMANENT_ERROR,
            code="INVALID_REFINEMENT_RESPONSE",
        )
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ProviderError(
            "chat response has no message",
            RetryCategory.PERMANENT_ERROR,
            code="INVALID_REFINEMENT_RESPONSE",
        )
    content = message.get("content")
    if isinstance(content, dict):
        payload = content
    elif isinstance(content, str):
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "chat response is not strict JSON",
                RetryCategory.PERMANENT_ERROR,
                code="INVALID_REFINEMENT_RESPONSE",
            ) from exc
    else:
        raise ProviderError(
            "chat response has no JSON content",
            RetryCategory.PERMANENT_ERROR,
            code="INVALID_REFINEMENT_RESPONSE",
        )
    if not isinstance(payload, dict):
        raise ProviderError(
            "chat response JSON must be an object",
            RetryCategory.PERMANENT_ERROR,
            code="INVALID_REFINEMENT_RESPONSE",
        )
    return payload


__all__ = [
    "LiveRoleFence",
    "ModelRoleExecution",
    "ModelRoleRuntime",
    "capability_for_model_role",
]
