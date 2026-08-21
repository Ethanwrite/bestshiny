from __future__ import annotations

from typing import Any
from urllib.parse import quote

from production_domain.models import RetryCategory
from provider_sdk import (
    GenerationProvider,
    ProviderError,
    ProviderHealth,
    ProviderJob,
    ProviderPollIdentity,
    ProviderSubmission,
    ProviderTrustLevel,
)
from provider_sdk.capabilities import ChatCapability, EmbeddingCapability, ResponsesCapability
from provider_sdk.http import ProviderJsonClient, provider_health_metadata
from provider_sdk.transport import (
    LiveProviderSettings,
    ProviderTransport,
    create_provider_transport,
)


class OpenRouterProvider(
    GenerationProvider,
    ChatCapability,
    ResponsesCapability,
    EmbeddingCapability,
):
    """One OpenRouter client for chat, Responses, embeddings, and async video."""

    name = "openrouter"
    trust_level = ProviderTrustLevel.PRODUCTION

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 120,
        transport_settings: LiveProviderSettings | None = None,
        transport: ProviderTransport | None = None,
    ):
        settings = transport_settings or LiveProviderSettings()
        injected_transport = transport is not None
        transport = transport or create_provider_transport(
            settings=settings,
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        self.client = ProviderJsonClient(self.name, transport, api_key_configured=bool(api_key.strip()))
        self.configured = bool(api_key.strip()) or injected_transport

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.client.request(
            "POST",
            "/chat/completions",
            json_body={"model": _required(model, "model"), "messages": messages, **(parameters or {})},
            submitted=True,
        )

    async def create_response(
        self,
        *,
        model: str,
        input_value: str | list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.client.request(
            "POST",
            "/responses",
            json_body={"model": _required(model, "model"), "input": input_value, **(parameters or {})},
            submitted=True,
        )

    async def create_embeddings(
        self,
        *,
        model: str,
        inputs: str | list[str] | list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.client.request(
            "POST",
            "/embeddings",
            json_body={"model": _required(model, "model"), "input": inputs, **(parameters or {})},
            submitted=True,
        )

    async def list_models(self) -> dict[str, Any]:
        return await self.client.request("GET", "/models")

    async def list_video_models(self) -> dict[str, Any]:
        return await self.client.request("GET", "/videos/models")

    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del request, account_id, worker_id
        raise _unsupported("OpenRouter image generation is not exposed by this adapter")

    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del account_id, worker_id
        payload = {key: value for key, value in request.items() if not key.startswith("_")}
        payload["model"] = _required(str(payload.get("model") or ""), "model")
        payload["prompt"] = _required(str(payload.get("prompt") or ""), "prompt")
        data = await self.client.request("POST", "/videos", json_body=payload, submitted=True)
        job_id = data.get("id")
        if not job_id:
            raise ProviderError(
                "OpenRouter returned no video job ID",
                RetryCategory.PERMANENT_ERROR,
                code="MISSING_PROVIDER_JOB",
                submitted=True,
            )
        return ProviderSubmission(str(job_id), data)

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        del asset, account_id, worker_id
        raise _unsupported("OpenRouter expects URL/data references in the generation request")

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
        poll_identity: ProviderPollIdentity | None = None,
    ) -> ProviderJob:
        del account_id, worker_id, poll_identity
        if generation_type != "video":
            raise _unsupported("OpenRouter provider polling only supports video jobs")
        data = await self.client.request("GET", f"/videos/{quote(provider_job_id, safe='')}")
        return _video_job(provider_job_id, data)

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        del account_id, worker_id
        await self.client.request("DELETE", f"/videos/{quote(provider_job_id, safe='')}")
        return True

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        del account_id, worker_id
        return None

    async def health(self) -> ProviderHealth:
        ok, detail, metadata = provider_health_metadata(self.client)
        metadata["capabilities"] = ["chat", "responses", "embeddings", "video"]
        if not self.configured:
            return ProviderHealth(False, "NOT_CONFIGURED", {**metadata, "status": "NOT_CONFIGURED"})
        return ProviderHealth(ok, detail, metadata)


def _required(value: str, label: str) -> str:
    if not value.strip():
        raise ProviderError(
            f"{label} is required",
            RetryCategory.INVALID_REQUEST,
            code="INVALID_REQUEST",
        )
    return value.strip()


def _unsupported(message: str) -> ProviderError:
    return ProviderError(message, RetryCategory.INVALID_REQUEST, code="CAPABILITY_NOT_SUPPORTED")


def _video_job(provider_job_id: str, data: dict[str, Any]) -> ProviderJob:
    raw_status = str(data.get("status") or "pending").lower()
    if raw_status in {"completed", "succeeded", "success"}:
        status, progress = "COMPLETED", 1.0
    elif raw_status in {"failed", "error", "expired"}:
        status, progress = "FAILED", 1.0
    elif raw_status in {"cancelled", "canceled"}:
        status, progress = "CANCELLED", 1.0
    else:
        status = "RUNNING" if raw_status in {"running", "processing"} else "QUEUED"
        progress = float(data.get("progress") or 0)
        if progress > 1:
            progress /= 100
    output = data.get("output") if isinstance(data.get("output"), dict) else {}
    output_url = data.get("output_url") or data.get("video_url") or data.get("url") or output.get("url")
    error = data.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("code")
    return ProviderJob(
        provider_job_id,
        status,
        progress=max(0.0, min(1.0, progress)),
        output_url=str(output_url) if output_url else None,
        output_mime_type="video/mp4" if output_url else None,
        error=str(error) if error else None,
        raw=data,
    )
