from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from production_domain.models import RetryCategory
from provider_sdk import (
    AssetCriticality,
    GenerationProvider,
    ProviderError,
    ProviderHealth,
    ProviderJob,
    ProviderSubmission,
    ProviderTrustLevel,
)
from provider_sdk.budget import (
    InMemoryProviderBudgetRepository,
    ProviderBudgetExceeded,
    ProviderBudgetRepository,
)
from provider_sdk.capabilities import ChatCapability
from provider_sdk.edge import EdgePolicyViolation, EdgeTask, EdgeTaskPolicy
from provider_sdk.http import ProviderJsonClient, provider_health_metadata
from provider_sdk.transport import (
    LiveProviderSettings,
    ProviderMode,
    ProviderTransport,
    create_provider_transport,
)


class RunAPIEdgeProvider(GenerationProvider, ChatCapability):
    """Low-trust adapter that cannot execute without edge policy and budget approval."""

    name = "runapi"
    trust_level = ProviderTrustLevel.EDGE

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        model_id: str = "",
        chat_path: str = "/v1/chat/completions",
        image_path: str = "/v1/images/generations",
        video_path: str = "/v1/videos",
        timeout_seconds: float = 120,
        budget_repository: ProviderBudgetRepository | None = None,
        budget_usd: Decimal | str = Decimal("10"),
        allow_edge_calls: bool = False,
        transport_settings: LiveProviderSettings | None = None,
        transport: ProviderTransport | None = None,
    ):
        settings = transport_settings or LiveProviderSettings()
        injected_transport = transport is not None
        # Mock/recorded transports do not need a host. Live construction rejects
        # the empty placeholder before any network call can occur.
        transport_base = base_url or "https://runapi.invalid"
        transport = transport or create_provider_transport(
            settings=settings,
            base_url=transport_base,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        self.client = ProviderJsonClient(self.name, transport, api_key_configured=bool(api_key.strip()))
        self.base_url_configured = bool(base_url.strip())
        self.model_id = model_id.strip()
        self.chat_path = chat_path
        self.image_path = image_path
        self.video_path = video_path
        self.policy = EdgeTaskPolicy()
        self.persistent_budget = budget_repository is not None
        self.budget = budget_repository or InMemoryProviderBudgetRepository(
            {self.name: Decimal(str(budget_usd))}
        )
        self.allow_edge_calls = allow_edge_calls
        self.configured = bool(self.model_id) and (
            (bool(api_key.strip()) and self.base_url_configured) or injected_transport
        )

    def _task(self, request: dict[str, Any], *, generation: bool = False) -> EdgeTask:
        # Only an internal envelope is accepted. Public GenerationRequest
        # metadata is untrusted and must never define task identity or price.
        declaration = request.get("_edge_task")
        if not isinstance(declaration, EdgeTask):
            raise _policy_error("RunAPI requires a server-issued EdgeTask")
        request_criticality = request.get("asset_criticality")
        if request_criticality is not None:
            try:
                criticality_matches = AssetCriticality(str(request_criticality)) == AssetCriticality(
                    declaration.asset_criticality
                )
            except ValueError as exc:
                raise _policy_error("invalid RunAPI asset criticality") from exc
            if not criticality_matches:
                raise _policy_error("edge task criticality cannot override GenerationRequest")
        try:
            task = declaration
            if generation:
                self.policy.authorize_generation(task)
            else:
                self.policy.authorize(task)
        except EdgePolicyViolation as exc:
            raise _policy_error(str(exc)) from exc
        if self.client.transport.mode is ProviderMode.LIVE and not self.allow_edge_calls:
            raise ProviderError(
                "live RunAPI call denied; ALLOW_RUNAPI_EDGE_CALLS=true is required",
                RetryCategory.PERMANENT_ERROR,
                code="RUNAPI_EDGE_CALL_DENIED",
            )
        if self.client.transport.mode is ProviderMode.LIVE and not self.persistent_budget:
            raise ProviderError(
                "live RunAPI routing requires a persistent ProviderBudgetRepository",
                RetryCategory.PERMANENT_ERROR,
                code="PERSISTENT_BUDGET_REQUIRED",
            )
        return task

    async def _execute(
        self,
        task: EdgeTask,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        # Configuration and the global live-call gate are local facts. Resolve
        # them before reserving budget or recording a remote paid boundary.
        self.client.assert_ready()
        paid_boundary_crossed = self.client.transport.mode is ProviderMode.LIVE
        initial_status = "UNCERTAIN" if paid_boundary_crossed else "RESERVED"
        try:
            reservation = self.budget.reserve(
                provider=self.name,
                task_id=task.task_id,
                task_role=task.role.value,
                estimated_cost_usd=task.estimated_cost_usd,
                initial_status=initial_status,
            )
        except ProviderBudgetExceeded as exc:
            raise ProviderError(
                str(exc),
                RetryCategory.PERMANENT_ERROR,
                code="PROVIDER_BUDGET_EXHAUSTED",
            ) from exc
        if not reservation.acquired or reservation.status != initial_status:
            raise ProviderError(
                "RunAPI task ID has already been executed",
                RetryCategory.PERMANENT_ERROR,
                code="EDGE_TASK_ALREADY_EXECUTED",
            )
        try:
            data = await operation()
        except ProviderError as exc:
            if not paid_boundary_crossed and exc.submitted:
                self.budget.settle(
                    reservation.reservation_id,
                    actual_cost_usd=None,
                    status="UNCERTAIN",
                )
            elif not paid_boundary_crossed:
                self.budget.release(reservation.reservation_id)
            raise
        except Exception:
            # Mock/recorded fixture misses cannot have spent money. For a live
            # unknown boundary, reserve the estimate conservatively.
            if not paid_boundary_crossed:
                self.budget.release(reservation.reservation_id)
            raise
        actual_cost = _actual_cost(data)
        self.budget.settle(
            reservation.reservation_id,
            actual_cost_usd=actual_cost,
            status="SETTLED" if actual_cost is not None else "UNCERTAIN",
        )
        return data

    async def execute_chat(
        self,
        *,
        task: EdgeTask,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            self.policy.authorize(task)
        except EdgePolicyViolation as exc:
            raise _policy_error(str(exc)) from exc
        if self.client.transport.mode is ProviderMode.LIVE and not self.allow_edge_calls:
            raise ProviderError(
                "live RunAPI call denied; ALLOW_RUNAPI_EDGE_CALLS=true is required",
                RetryCategory.PERMANENT_ERROR,
                code="RUNAPI_EDGE_CALL_DENIED",
            )
        if self.client.transport.mode is ProviderMode.LIVE and not self.persistent_budget:
            raise ProviderError(
                "live RunAPI routing requires a persistent ProviderBudgetRepository",
                RetryCategory.PERMANENT_ERROR,
                code="PERSISTENT_BUDGET_REQUIRED",
            )
        selected_model = (model or self.model_id).strip()
        if not selected_model:
            raise _invalid("RUNAPI_MODEL_ID is not configured")
        payload = {"model": selected_model, "messages": messages, **(parameters or {})}
        return await self._execute(
            task,
            lambda: self.client.request("POST", self.chat_path, json_body=payload, submitted=True),
        )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        values = dict(parameters or {})
        declaration = values.pop("_edge_task", None)
        if not isinstance(declaration, EdgeTask):
            raise _policy_error("RunAPI chat requires a server-issued EdgeTask")
        return await self.execute_chat(
            task=declaration,
            model=model,
            messages=messages,
            parameters=values,
        )

    async def refine_prompt(self, request: dict[str, Any]) -> dict[str, Any]:
        task = self._task(request)
        model = str(request.get("model") or self.model_id)
        body = {
            key: value for key, value in request.items() if not key.startswith("_") and key not in {"model"}
        }
        data = await self.execute_chat(
            task=task,
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Return one JSON object. Preserve immutable_facts exactly and only refine "
                        "professional wording. Never overwrite the source prompt."
                    ),
                },
                {"role": "user", "content": json.dumps(body, ensure_ascii=False, sort_keys=True)},
            ],
            parameters={"response_format": {"type": "json_object"}},
        )
        content = _chat_content(data)
        try:
            parsed = json.loads(content) if isinstance(content, str) else content
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "RunAPI prompt refiner returned invalid JSON",
                RetryCategory.PERMANENT_ERROR,
                code="INVALID_REFINEMENT_RESPONSE",
            ) from exc
        if not isinstance(parsed, dict):
            raise ProviderError(
                "RunAPI prompt refiner returned an invalid object",
                RetryCategory.PERMANENT_ERROR,
                code="INVALID_REFINEMENT_RESPONSE",
            )
        return parsed

    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del account_id, worker_id
        task = self._task(request, generation=True)
        payload = {key: value for key, value in request.items() if not key.startswith("_")}
        payload["model"] = str(payload.get("model") or self.model_id).strip()
        if not payload["model"]:
            raise _invalid("RUNAPI_MODEL_ID is not configured")
        data = await self._execute(
            task,
            lambda: self.client.request("POST", self.image_path, json_body=payload, submitted=True),
        )
        job_id = _job_id(data)
        if not job_id:
            raise _missing_job("RunAPI returned no image task ID")
        return ProviderSubmission(job_id, data)

    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del account_id, worker_id
        task = self._task(request, generation=True)
        payload = {key: value for key, value in request.items() if not key.startswith("_")}
        payload["model"] = str(payload.get("model") or self.model_id).strip()
        if not payload["model"]:
            raise _invalid("RUNAPI_MODEL_ID is not configured")
        data = await self._execute(
            task,
            lambda: self.client.request("POST", self.video_path, json_body=payload, submitted=True),
        )
        job_id = _job_id(data)
        if not job_id:
            raise _missing_job("RunAPI returned no video task ID")
        return ProviderSubmission(job_id, data)

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        del asset, account_id, worker_id
        raise ProviderError(
            "RunAPI uploads are disabled; edge assets must remain temporary",
            RetryCategory.INVALID_REQUEST,
            code="CAPABILITY_NOT_SUPPORTED",
        )

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool:
        del provider_media_id, account_id, worker_id
        return False

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
    ) -> ProviderJob:
        del account_id, worker_id
        base = self.image_path if generation_type == "image" else self.video_path
        data = await self.client.request("GET", f"{base.rstrip('/')}/{quote(provider_job_id, safe='')}")
        return _provider_job(provider_job_id, data, generation_type)

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        del provider_job_id, account_id, worker_id
        return False

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        del account_id, worker_id
        return None

    async def health(self) -> ProviderHealth:
        ok, detail, metadata = provider_health_metadata(self.client)
        snapshot = self.budget.get(self.name)
        metadata.update(
            {
                "trust_level": "EDGE",
                "routing_enabled": snapshot.routing_enabled,
                "credit_budget_usd": str(snapshot.credit_budget_usd),
                "actual_cost_usd": str(snapshot.actual_cost_usd),
                "reserved_cost_usd": str(snapshot.reserved_cost_usd),
                "remaining_budget_usd": str(snapshot.remaining_budget_usd),
                "base_url_configured": self.base_url_configured,
                "persistent_budget": self.persistent_budget,
            }
        )
        if not self.configured:
            return ProviderHealth(False, "NOT_CONFIGURED", {**metadata, "status": "NOT_CONFIGURED"})
        if self.client.transport.mode is ProviderMode.LIVE and not self.persistent_budget:
            return ProviderHealth(
                False,
                "PERSISTENT_BUDGET_REQUIRED",
                {**metadata, "status": "PERSISTENT_BUDGET_REQUIRED"},
            )
        if not snapshot.routing_enabled:
            return ProviderHealth(False, "BUDGET_EXHAUSTED", metadata)
        return ProviderHealth(ok, detail, metadata)


def _actual_cost(data: dict[str, Any]) -> Decimal | None:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    value = usage.get("cost") or usage.get("total_cost") or data.get("cost")
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed >= 0 else None


def _chat_content(data: dict[str, Any]) -> Any:
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    first = choices[0] if choices and isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    return message.get("content") or data.get("output") or data


def _job_id(data: dict[str, Any]) -> str:
    output = data.get("output") if isinstance(data.get("output"), dict) else {}
    return str(data.get("id") or data.get("task_id") or output.get("task_id") or "")


def _provider_job(provider_job_id: str, data: dict[str, Any], generation_type: str) -> ProviderJob:
    output = data.get("output") if isinstance(data.get("output"), dict) else {}
    raw_status = str(data.get("status") or output.get("task_status") or "pending").lower()
    if raw_status in {"completed", "succeeded", "success"}:
        status, progress = "COMPLETED", 1.0
    elif raw_status in {"failed", "error", "expired"}:
        status, progress = "FAILED", 1.0
    else:
        status = "RUNNING" if raw_status in {"running", "processing"} else "QUEUED"
        progress = 0.5 if status == "RUNNING" else 0.0
    url = data.get("url") or data.get("output_url") or output.get("video_url") or output.get("url")
    return ProviderJob(
        provider_job_id,
        status,
        progress=progress,
        output_url=str(url) if url else None,
        output_mime_type=("video/mp4" if generation_type == "video" else "image/png") if url else None,
        error=str(data.get("error")) if data.get("error") else None,
        raw=data,
    )


def _policy_error(message: str) -> ProviderError:
    return ProviderError(message, RetryCategory.PERMANENT_ERROR, code="EDGE_POLICY_DENIED")


def _invalid(message: str) -> ProviderError:
    return ProviderError(message, RetryCategory.INVALID_REQUEST, code="INVALID_REQUEST")


def _missing_job(message: str) -> ProviderError:
    return ProviderError(
        message,
        RetryCategory.PERMANENT_ERROR,
        code="MISSING_PROVIDER_JOB",
        submitted=True,
    )
