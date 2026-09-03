from __future__ import annotations

from typing import Any

from production_domain.models import RetryCategory
from provider_sdk import ProviderError, ProviderHealth
from provider_sdk.capabilities import EmbeddingCapability
from provider_sdk.http import ProviderJsonClient, provider_health_metadata
from provider_sdk.transport import LiveProviderSettings, ProviderTransport, create_provider_transport


class VoyageProvider(EmbeddingCapability):
    """Official Voyage multimodal-embedding transport."""

    name = "voyage"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.voyageai.com",
        model_id: str = "voyage-multimodal-3.5",
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
        self.model_id = model_id.strip()
        self.configured = bool(self.model_id) and (bool(api_key.strip()) or injected_transport)

    async def create_embeddings(
        self,
        *,
        model: str,
        inputs: str | list[str] | list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = (model or self.model_id).strip()
        if not selected:
            raise ProviderError(
                "VOYAGE_MULTIMODAL_MODEL is not configured",
                RetryCategory.INVALID_REQUEST,
                code="INVALID_REQUEST",
            )
        options = dict(parameters or {})
        input_type = options.pop("input_type", None)
        # Voyage returns 1024 dimensions by default. Memory truncates and
        # normalizes the Matryoshka vector locally, so the platform's generic
        # dimensions planning hint is not sent as a provider parameter.
        options.pop("dimensions", None)
        truncation = options.pop("truncation", True)
        if options:
            names = ", ".join(sorted(options))
            raise ProviderError(
                f"unsupported Voyage multimodal embedding parameters: {names}",
                RetryCategory.INVALID_REQUEST,
                code="INVALID_REQUEST",
            )
        body: dict[str, Any] = {
            "model": selected,
            "inputs": _voyage_inputs(inputs),
            "truncation": bool(truncation),
        }
        if input_type is not None:
            normalized_type = str(input_type).strip().lower()
            if normalized_type not in {"query", "document"}:
                raise ProviderError(
                    "Voyage input_type must be query or document",
                    RetryCategory.INVALID_REQUEST,
                    code="INVALID_REQUEST",
                )
            body["input_type"] = normalized_type
        return await self.client.request(
            "POST",
            "/v1/multimodalembeddings",
            json_body=body,
            submitted=True,
        )

    async def health(self) -> ProviderHealth:
        ok, detail, metadata = provider_health_metadata(self.client)
        metadata.update(
            {
                "capabilities": ["multimodal_embeddings"],
                "model_configured": bool(self.model_id),
                "api_surface": "POST /v1/multimodalembeddings",
            }
        )
        if not self.configured:
            return ProviderHealth(False, "NOT_CONFIGURED", {**metadata, "status": "NOT_CONFIGURED"})
        return ProviderHealth(ok, detail, metadata)


def _voyage_inputs(
    inputs: str | list[str] | list[dict[str, Any]],
) -> list[dict[str, list[dict[str, str]]]]:
    if isinstance(inputs, str):
        inputs = [inputs]
    if not isinstance(inputs, list) or not inputs:
        raise ProviderError(
            "Voyage multimodal embeddings require at least one input",
            RetryCategory.INVALID_REQUEST,
            code="INVALID_REQUEST",
        )
    normalized: list[dict[str, list[dict[str, str]]]] = []
    for item in inputs:
        if isinstance(item, str):
            content: Any = [{"type": "text", "text": item}]
        elif isinstance(item, dict):
            content = item.get("content")
        else:
            content = None
        if not isinstance(content, list) or not content:
            raise ProviderError(
                "each Voyage input requires non-empty content",
                RetryCategory.INVALID_REQUEST,
                code="INVALID_REQUEST",
            )
        pieces: list[dict[str, str]] = []
        for raw in content:
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("type") or "").strip()
            if kind == "text" and str(raw.get("text") or "").strip():
                pieces.append({"type": "text", "text": str(raw["text"])})
                continue
            if kind == "image_url":
                value = raw.get("image_url")
                if isinstance(value, dict):
                    value = value.get("url")
                url = str(value or "").strip()
                if url.startswith("data:"):
                    pieces.append({"type": "image_base64", "image_base64": url})
                elif url:
                    pieces.append({"type": "image_url", "image_url": url})
                continue
            if kind == "video_url" and str(raw.get("video_url") or "").strip():
                pieces.append({"type": "video_url", "video_url": str(raw["video_url"])})
        if not pieces:
            raise ProviderError(
                "Voyage input contains no supported text, image, or video content",
                RetryCategory.INVALID_REQUEST,
                code="INVALID_REQUEST",
            )
        normalized.append({"content": pieces})
    return normalized


__all__ = ["VoyageProvider"]
