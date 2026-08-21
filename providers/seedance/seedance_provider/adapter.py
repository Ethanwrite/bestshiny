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
from provider_sdk.capabilities import ChatCapability
from provider_sdk.http import ProviderJsonClient, provider_health_metadata
from provider_sdk.transport import LiveProviderSettings, ProviderTransport, create_provider_transport


class ArkProvider(GenerationProvider, ChatCapability):
    """Volcengine Ark client shared by Doubao chat and Seedance video."""

    name = "seedance"
    trust_level = ProviderTrustLevel.PRODUCTION

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
        doubao_model_id: str = "",
        seedance_model_id: str = "",
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
        self.client = ProviderJsonClient("ark", transport, api_key_configured=bool(api_key.strip()))
        self.doubao_model_id = doubao_model_id.strip()
        self.seedance_model_id = seedance_model_id.strip()
        self.configured = bool(self.doubao_model_id or self.seedance_model_id) and (
            bool(api_key.strip()) or injected_transport
        )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = (model or self.doubao_model_id).strip()
        if not selected:
            raise _invalid("DOUBAO_MODEL_ID is not configured")
        return await self.client.request(
            "POST",
            "/chat/completions",
            json_body={"model": selected, "messages": messages, **(parameters or {})},
            submitted=True,
        )

    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del account_id, worker_id
        payload = {key: value for key, value in request.items() if not key.startswith("_")}
        if not str(payload.get("model") or "").strip():
            raise _invalid("image model is required")
        if not str(payload.get("prompt") or "").strip():
            raise _invalid("image prompt is required")
        data = await self.client.request("POST", "/images/generations", json_body=payload, submitted=True)
        entries = data.get("data") if isinstance(data.get("data"), list) else []
        first = entries[0] if entries and isinstance(entries[0], dict) else {}
        media_id = first.get("id") or first.get("url") or data.get("id")
        if not media_id:
            raise _missing_job("Ark returned no image result identifier")
        return ProviderSubmission(str(media_id), data)

    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del account_id, worker_id
        payload = _seedance_payload(request, self.seedance_model_id)
        data = await self.client.request(
            "POST",
            "/contents/generations/tasks",
            json_body=payload,
            submitted=True,
        )
        job_id = data.get("id")
        if not job_id:
            raise _missing_job("Ark returned no Seedance task ID")
        return ProviderSubmission(str(job_id), data)

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        del asset, account_id, worker_id
        raise ProviderError(
            "Ark adapter requires URL/data references in the request",
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
        poll_identity: ProviderPollIdentity | None = None,
    ) -> ProviderJob:
        del account_id, worker_id, poll_identity
        if generation_type != "video":
            raise _invalid("Ark polling currently supports Seedance video tasks only")
        data = await self.client.request(
            "GET", f"/contents/generations/tasks/{quote(provider_job_id, safe='')}"
        )
        return _ark_job(provider_job_id, data)

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        del account_id, worker_id
        await self.client.request("DELETE", f"/contents/generations/tasks/{quote(provider_job_id, safe='')}")
        return True

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        del account_id, worker_id
        return None

    async def health(self) -> ProviderHealth:
        ok, detail, metadata = provider_health_metadata(self.client)
        metadata.update(
            {
                "capabilities": ["doubao_chat", "seedance_video", "image"],
                "doubao_model_configured": bool(self.doubao_model_id),
                "seedance_model_configured": bool(self.seedance_model_id),
            }
        )
        if not self.configured:
            return ProviderHealth(False, "NOT_CONFIGURED", {**metadata, "status": "NOT_CONFIGURED"})
        return ProviderHealth(ok, detail, metadata)


class SeedanceProvider(ArkProvider):
    """Backward-compatible product-facing name for the Ark implementation."""


def _seedance_payload(request: dict[str, Any], configured_model: str) -> dict[str, Any]:
    model = str(request.get("model") or configured_model).strip()
    prompt = str(request.get("prompt") or "").strip()
    if not model:
        raise _invalid("SEEDANCE_MODEL_ID is not configured")
    if not prompt and not request.get("content"):
        raise _invalid("Seedance prompt or content is required")
    existing_content = request.get("content")
    if isinstance(existing_content, list):
        content = existing_content
    else:
        content = []
        if prompt:
            content.append({"type": "text", "text": prompt})
        references = [
            request.get("first_frame_image") or request.get("start_frame"),
            *(request.get("reference_images") or []),
        ]
        for reference in dict.fromkeys(str(item) for item in references if item):
            content.append({"type": "image_url", "image_url": {"url": reference}})
    payload: dict[str, Any] = {"model": model, "content": content}
    mappings = {
        "aspect_ratio": "ratio",
        "ratio": "ratio",
        "duration": "duration",
        "resolution": "resolution",
        "seed": "seed",
        "watermark": "watermark",
        "generate_audio": "generate_audio",
        "return_last_frame": "return_last_frame",
        "service_tier": "service_tier",
        "execution_expires_after": "execution_expires_after",
        "callback_url": "callback_url",
    }
    for source, target in mappings.items():
        if source in request and request[source] is not None:
            payload[target] = request[source]
    audio = request.get("audio")
    if "generate_audio" not in payload and isinstance(audio, dict):
        payload["generate_audio"] = bool(audio)
    payload.setdefault("watermark", False)
    payload.setdefault("return_last_frame", True)
    return payload


def _ark_job(provider_job_id: str, data: dict[str, Any]) -> ProviderJob:
    raw_status = str(data.get("status") or "queued").lower()
    if raw_status == "succeeded":
        status, progress = "COMPLETED", 1.0
    elif raw_status in {"failed", "expired"}:
        status, progress = "FAILED", 1.0
    elif raw_status in {"cancelled", "canceled"}:
        status, progress = "CANCELLED", 1.0
    else:
        status = "RUNNING" if raw_status == "running" else "QUEUED"
        progress = 0.5 if status == "RUNNING" else 0.0
    content = data.get("content") if isinstance(data.get("content"), dict) else {}
    output_url = data.get("video_url") or content.get("video_url")
    error = data.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("code")
    return ProviderJob(
        provider_job_id,
        status,
        progress=progress,
        output_url=str(output_url) if output_url else None,
        output_mime_type="video/mp4" if output_url else None,
        error=str(error) if error else None,
        raw=data,
    )


def _invalid(message: str) -> ProviderError:
    return ProviderError(message, RetryCategory.INVALID_REQUEST, code="INVALID_REQUEST")


def _missing_job(message: str) -> ProviderError:
    return ProviderError(
        message,
        RetryCategory.PERMANENT_ERROR,
        code="MISSING_PROVIDER_JOB",
        submitted=True,
    )
