from __future__ import annotations

import base64
import binascii
from typing import Any
from urllib.parse import quote

from production_domain.models import RetryCategory
from provider_sdk import (
    GenerationProvider,
    ProviderError,
    ProviderHealth,
    ProviderInlineOutput,
    ProviderJob,
    ProviderPollIdentity,
    ProviderReferenceMode,
    ProviderSubmission,
    ProviderTrustLevel,
)
from provider_sdk.capabilities import ChatCapability
from provider_sdk.http import ProviderJsonClient, provider_health_metadata
from provider_sdk.transport import LiveProviderSettings, ProviderTransport, create_provider_transport

# Only these fields may reach the Ark image API. Tenancy, routing, accounting,
# idempotency and internal audit metadata never leave the platform.
IMAGE_REQUEST_FIELDS = frozenset(
    {
        "model",
        "prompt",
        "negative_prompt",
        "image",
        "image_url",
        "size",
        "resolution",
        "aspect_ratio",
        "seed",
        "guidance_scale",
        "watermark",
        "response_format",
        "sequential_image_generation",
    }
)


class ArkProvider(GenerationProvider, ChatCapability):
    """Volcengine Ark client shared by Doubao chat and Seedance video."""

    name = "seedance"
    trust_level = ProviderTrustLevel.PRODUCTION
    # Ark never ingests uploads; every reference must already be fetchable.
    reference_mode = ProviderReferenceMode.FETCHABLE_URL

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
        payload = {
            key: value
            for key, value in request.items()
            if key in IMAGE_REQUEST_FIELDS and value is not None
        }
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
        # Ark's images API is synchronous: the artefact is in this response and
        # there is no task to poll (`get_job` rightly refuses non-video IDs —
        # observed live on 2026-08-30 as `INVALID_REQUEST` after a confirmed,
        # billed submission). Carrying the result on the submission lets the
        # Gateway finish through its ordinary held-result completion path.
        outputs = _ark_image_outputs(entries)
        url = first.get("url") if isinstance(first.get("url"), str) and first.get("url") else None
        if not outputs and not url:
            raise _missing_job("Ark returned neither an image URL nor inline image bytes")
        return ProviderSubmission(
            str(media_id),
            data,
            result=ProviderJob(
                str(media_id),
                "COMPLETED",
                progress=1.0,
                output_url=None if outputs else url,
                output_mime_type=outputs[0].mime_type if outputs else None,
                outputs=outputs,
                raw=data,
            ),
        )

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
        first_frame = (
            request.get("first_frame_image")
            or request.get("start_frame")
            or request.get("start_frame_url")
        )
        last_frame = (
            request.get("last_frame_image")
            or request.get("end_frame")
            or request.get("end_frame_url")
        )
        references = list(request.get("reference_images") or request.get("reference_urls") or [])
        # `role` is a sibling of `image_url`, and Ark requires it: an omni-reference
        # image without `role: reference_image` is not a reference, and a frame
        # without `role: first_frame` is not a frame. Sending role-less images —
        # which is what this adapter did — leaves the model to guess, and mixing a
        # first frame into the same flat list as reference images asks for two
        # mutually exclusive modes at once.
        if references and (first_frame or last_frame):
            raise _invalid(
                "Seedance takes either first/last frames or omni-reference images, not both"
            )
        if last_frame and not first_frame:
            raise _invalid("Seedance last_frame requires a first_frame in the same request")
        if first_frame:
            content.append(
                {"type": "image_url", "image_url": {"url": str(first_frame)}, "role": "first_frame"}
            )
        if last_frame:
            content.append(
                {"type": "image_url", "image_url": {"url": str(last_frame)}, "role": "last_frame"}
            )
        for reference in dict.fromkeys(str(item) for item in references if item):
            content.append(
                {"type": "image_url", "image_url": {"url": reference}, "role": "reference_image"}
            )
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
    # Seedance 2.5 accepts `ratio` only as "adaptive" once a frame fixes the
    # geometry (first-frame, first-and-last-frame, edit and extend). A supplied
    # frame has already decided the aspect, so this is not overriding a caller's
    # choice — it is declining to ask the same question twice with a second
    # answer. Scoped to the 2.5 family on purpose: the rule is documented for
    # 2.5, and quietly applying it to 2.0 would be guessing at a contract again.
    if _is_seedance_2_5(model) and _has_frame_role(payload.get("content")):
        payload["ratio"] = "adaptive"
    return payload


def _is_seedance_2_5(model: str) -> bool:
    return "seedance-2-5" in model or "seedance-2.5" in model


def _has_frame_role(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(part, dict) and part.get("role") in {"first_frame", "last_frame"}
        for part in content
    )


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


#: Bound on an inline (b64_json) image accepted from Ark, matching the
#: OpenRouter image path's ceiling.
MAX_IMAGE_OUTPUT_BYTES = 32 * 1024 * 1024


def _ark_image_outputs(entries: list[Any]) -> list[ProviderInlineOutput]:
    """Decode inline b64_json image entries; URL-form responses return []."""

    outputs: list[ProviderInlineOutput] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        encoded = entry.get("b64_json")
        if not isinstance(encoded, str) or not encoded:
            continue
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ProviderError(
                "Ark returned an image that is not valid base64",
                RetryCategory.PERMANENT_ERROR,
                code="INVALID_PROVIDER_IMAGE",
                submitted=True,
            ) from exc
        if len(content) > MAX_IMAGE_OUTPUT_BYTES:
            raise ProviderError(
                "Ark returned an image larger than the accepted bound",
                RetryCategory.PERMANENT_ERROR,
                code="PROVIDER_IMAGE_TOO_LARGE",
                submitted=True,
            )
        outputs.append(ProviderInlineOutput(mime_type="image/png", content=content))
    return outputs


def _invalid(message: str) -> ProviderError:
    return ProviderError(message, RetryCategory.INVALID_REQUEST, code="INVALID_REQUEST")


def _missing_job(message: str) -> ProviderError:
    return ProviderError(
        message,
        RetryCategory.PERMANENT_ERROR,
        code="MISSING_PROVIDER_JOB",
        submitted=True,
    )
