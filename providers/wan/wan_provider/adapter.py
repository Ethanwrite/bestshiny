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
    ProviderReferenceMode,
    ProviderSubmission,
    ProviderTrustLevel,
)
from provider_sdk.capabilities import ChatCapability
from provider_sdk.http import ProviderJsonClient, provider_health_metadata
from provider_sdk.transport import (
    LiveProviderSettings,
    ProviderTransport,
    create_provider_transport,
)

# Reviewed "<logical model>[:<mode>]=<dashscope model>" pairs, mirroring the
# Google Flow mapping so one mechanism covers every logical -> runtime model
# translation. Modes are t2v, i2v and r2v. An entry without a mode applies to
# every mode of that logical model, which is what single-model families such as
# Wan 3.0 need.
DEFAULT_VIDEO_MODEL_KEYS: dict[str, str] = {
    "wan-2.7:t2v": "wan2.7-t2v",
    "wan-2.7:i2v": "wan2.7-i2v",
    "wan-2.7:r2v": "wan2.7-r2v-2026-06-12",
    "wan-3.0": "wan3.0-video",
}


def parse_video_model_keys(configured: str) -> dict[str, str]:
    """Parse the operator-reviewed logical-to-DashScope model declaration.

    Only operator entries are returned. They must stay distinguishable from the
    built-in defaults so an explicit WAN2_7_*_MODEL_ID setting can outrank a
    default without outranking a deliberate declaration.
    """

    mapping: dict[str, str] = {}
    for entry in str(configured or "").split(","):
        item = entry.strip()
        if not item:
            continue
        logical, separator, dashscope_model = item.partition("=")
        if not separator or not logical.strip() or not dashscope_model.strip():
            raise ValueError(f"WAN_VIDEO_MODEL_KEYS entry must be model[:mode]=dashscope_model: {item}")
        mapping[logical.strip()] = dashscope_model.strip()
    return mapping


def resolve_video_model(
    requested: str,
    mode: str,
    model_keys: dict[str, str],
    mode_default: str = "",
) -> str:
    """Resolve one logical model plus its mode to a DashScope model ID.

    A logical registry name such as ``wan-2.7`` is not a DashScope model, and a
    mode-scoped setting alone cannot distinguish Wan versions. The mapping is
    consulted first for ``model:mode`` and then for ``model``; an operator's
    mode-specific setting remains an explicit override, and an unmapped model
    is rejected rather than posted to DashScope as an unknown model.
    """

    selected = str(requested or "").strip()
    keys = (f"{selected}:{mode}", selected)
    # 1. an explicit operator declaration always wins;
    for key in keys:
        if selected and key in model_keys:
            return model_keys[key]
    # 2. then the operator's mode-specific setting;
    if mode_default.strip():
        return mode_default.strip()
    # 3. then the reviewed built-in default for a known family.
    for key in keys:
        if selected and key in DEFAULT_VIDEO_MODEL_KEYS:
            return DEFAULT_VIDEO_MODEL_KEYS[key]
    if not selected:
        raise _invalid("a Wan video model is required for this mode")
    raise _invalid(
        f"Wan has no reviewed DashScope model for {selected!r} in {mode} mode; "
        "declare it in WAN_VIDEO_MODEL_KEYS"
    )


class WanProvider(GenerationProvider, ChatCapability):
    """Alibaba workspace adapter for OpenAI-compatible chat and Wan 2.7 async video."""

    name = "wan"
    # Wan requires fetchable URLs; DashScope never ingests an upload.
    reference_mode = ProviderReferenceMode.FETCHABLE_URL
    trust_level = ProviderTrustLevel.PRODUCTION

    def __init__(
        self,
        *,
        api_key: str = "",
        openai_base_url: str = "",
        dashscope_base_url: str = "",
        chat_model_id: str = "",
        t2v_model_id: str = "",
        i2v_model_id: str = "",
        r2v_model_id: str = "",
        video_model_keys: str = "",
        timeout_seconds: float = 120,
        transport_settings: LiveProviderSettings | None = None,
        chat_transport: ProviderTransport | None = None,
        video_transport: ProviderTransport | None = None,
    ):
        settings = transport_settings or LiveProviderSettings()
        chat_transport_injected = chat_transport is not None
        video_transport_injected = video_transport is not None
        chat_transport = chat_transport or create_provider_transport(
            settings=settings,
            base_url=openai_base_url or "https://wan-openai.invalid",
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        video_transport = video_transport or create_provider_transport(
            settings=settings,
            base_url=dashscope_base_url or "https://wan-dashscope.invalid",
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        configured = bool(api_key.strip())
        self.chat_client = ProviderJsonClient("wan_chat", chat_transport, api_key_configured=configured)
        self.video_client = ProviderJsonClient("wan", video_transport, api_key_configured=configured)
        self.openai_base_configured = bool(openai_base_url.strip())
        self.dashscope_base_configured = bool(dashscope_base_url.strip())
        self.chat_model_id = chat_model_id.strip()
        self.t2v_model_id = t2v_model_id.strip()
        self.i2v_model_id = i2v_model_id.strip()
        self.r2v_model_id = r2v_model_id.strip()
        self.video_model_keys = parse_video_model_keys(video_model_keys)
        self.configured = bool(self.t2v_model_id or self.i2v_model_id or self.r2v_model_id) and (
            (bool(api_key.strip()) and self.dashscope_base_configured) or video_transport_injected
        )
        self.chat_configured = bool(self.chat_model_id) and (
            (bool(api_key.strip()) and self.openai_base_configured) or chat_transport_injected
        )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = (model or self.chat_model_id).strip()
        if not selected:
            raise _invalid("WAN_CHAT_MODEL_ID is not configured")
        if not self.chat_configured:
            raise ProviderError(
                "Wan OpenAI-compatible chat transport is not configured",
                RetryCategory.PERMANENT_ERROR,
                code="PROVIDER_NOT_CONFIGURED",
            )
        return await self.chat_client.request(
            "POST",
            "/chat/completions",
            json_body={"model": selected, "messages": messages, **(parameters or {})},
            submitted=True,
        )

    async def generate_image(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del request, account_id, worker_id
        raise ProviderError(
            "Wan image generation is not exposed by this adapter",
            RetryCategory.INVALID_REQUEST,
            code="CAPABILITY_NOT_SUPPORTED",
        )

    async def generate_video(
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        del account_id, worker_id
        payload = self._video_payload(request)
        data = await self.video_client.request(
            "POST",
            "/services/aigc/video-generation/video-synthesis",
            json_body=payload,
            headers={"X-DashScope-Async": "enable"},
            submitted=True,
        )
        output = data.get("output") if isinstance(data.get("output"), dict) else {}
        job_id = data.get("task_id") or output.get("task_id")
        if not job_id:
            raise ProviderError(
                "Wan returned no async task ID",
                RetryCategory.PERMANENT_ERROR,
                code="MISSING_PROVIDER_JOB",
                submitted=True,
            )
        return ProviderSubmission(str(job_id), data)

    def _video_payload(self, request: dict[str, Any]) -> dict[str, Any]:
        reference_video = request.get("reference_video") or request.get("reference_video_url")
        first_frame = (
            request.get("first_frame")
            or request.get("first_frame_image")
            or request.get("start_frame")
            or request.get("start_frame_url")
        )
        mode = "r2v" if reference_video else "i2v" if first_frame else "t2v"
        mode_default = (
            self.r2v_model_id if reference_video else self.i2v_model_id if first_frame else self.t2v_model_id
        )
        model = resolve_video_model(
            str(request.get("model") or ""),
            mode,
            self.video_model_keys,
            mode_default,
        )
        existing_input = request.get("input")
        if isinstance(existing_input, dict):
            input_value = dict(existing_input)
        else:
            prompt = str(request.get("prompt") or "").strip()
            if not prompt:
                raise _invalid("Wan video prompt is required")
            input_value = {"prompt": prompt}
            if first_frame:
                input_value["img_url"] = first_frame
            last_frame = (
                request.get("last_frame") or request.get("end_frame") or request.get("end_frame_url")
            )
            if last_frame:
                input_value["last_frame_url"] = last_frame
            if reference_video:
                input_value["video_url"] = reference_video
        existing_parameters = request.get("parameters")
        parameters = dict(existing_parameters) if isinstance(existing_parameters, dict) else {}
        mappings = {
            "duration": "duration",
            "size": "size",
            "resolution": "size",
            "seed": "seed",
            "prompt_extend": "prompt_extend",
            "watermark": "watermark",
            "audio": "audio",
        }
        for source, target in mappings.items():
            if source in request and request[source] is not None:
                parameters[target] = request[source]
        parameters.setdefault("watermark", False)
        return {"model": model, "input": input_value, "parameters": parameters}

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:
        del asset, account_id, worker_id
        raise ProviderError(
            "Wan adapter requires URL references in the request",
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
            raise _invalid("Wan polling only supports video tasks")
        data = await self.video_client.request("GET", f"/tasks/{quote(provider_job_id, safe='')}")
        return _wan_job(provider_job_id, data)

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:
        del account_id, worker_id
        await self.video_client.request("DELETE", f"/tasks/{quote(provider_job_id, safe='')}")
        return True

    async def get_credits(self, *, account_id: str, worker_id: str) -> int | None:
        del account_id, worker_id
        return None

    async def health(self) -> ProviderHealth:
        ok, detail, metadata = provider_health_metadata(self.video_client)
        metadata.update(
            {
                "capabilities": ["openai_chat", "wan2.7_async_video"],
                "openai_base_configured": self.openai_base_configured,
                "dashscope_base_configured": self.dashscope_base_configured,
                "chat_model_configured": bool(self.chat_model_id),
                "chat_transport_configured": self.chat_configured,
                "video_models_configured": bool(self.t2v_model_id or self.i2v_model_id or self.r2v_model_id),
            }
        )
        if not self.configured:
            return ProviderHealth(False, "NOT_CONFIGURED", {**metadata, "status": "NOT_CONFIGURED"})
        return ProviderHealth(ok, detail, metadata)


def _wan_job(provider_job_id: str, data: dict[str, Any]) -> ProviderJob:
    output = data.get("output") if isinstance(data.get("output"), dict) else {}
    raw_status = str(output.get("task_status") or data.get("status") or "PENDING").upper()
    if raw_status == "SUCCEEDED":
        status, progress = "COMPLETED", 1.0
    elif raw_status in {"FAILED", "UNKNOWN"}:
        status, progress = "FAILED", 1.0
    elif raw_status in {"CANCELED", "CANCELLED"}:
        status, progress = "CANCELLED", 1.0
    else:
        status = "RUNNING" if raw_status == "RUNNING" else "QUEUED"
        progress = 0.5 if status == "RUNNING" else 0.0
    results = output.get("results") if isinstance(output.get("results"), list) else []
    first = results[0] if results and isinstance(results[0], dict) else {}
    output_url = output.get("video_url") or first.get("url") or first.get("video_url")
    error = output.get("message") or data.get("message")
    return ProviderJob(
        provider_job_id,
        status,
        progress=progress,
        output_url=str(output_url) if output_url else None,
        output_mime_type="video/mp4" if output_url else None,
        error=str(error) if error and status == "FAILED" else None,
        raw=data,
    )


def _invalid(message: str) -> ProviderError:
    return ProviderError(message, RetryCategory.INVALID_REQUEST, code="INVALID_REQUEST")
