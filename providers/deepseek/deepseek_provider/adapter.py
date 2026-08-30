from __future__ import annotations

from typing import Any

from production_domain.models import RetryCategory
from provider_sdk import ProviderError, ProviderHealth
from provider_sdk.capabilities import ChatCapability
from provider_sdk.http import ProviderJsonClient, provider_health_metadata, wire_parameters
from provider_sdk.transport import LiveProviderSettings, ProviderTransport, create_provider_transport


class DeepSeekProvider(ChatCapability):
    """OpenAI-compatible low-cost chat capability; no generation surface."""

    name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.deepseek.com",
        model_id: str = "",
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

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = (model or self.model_id).strip()
        if not selected:
            raise ProviderError(
                "DEEPSEEK_MODEL_ID is not configured",
                RetryCategory.INVALID_REQUEST,
                code="INVALID_REQUEST",
            )
        return await self.client.request(
            "POST",
            "/chat/completions",
            json_body={"model": selected, "messages": messages, **wire_parameters(parameters)},
            submitted=True,
        )

    async def health(self) -> ProviderHealth:
        ok, detail, metadata = provider_health_metadata(self.client)
        metadata.update({"capabilities": ["chat"], "model_configured": bool(self.model_id)})
        if not self.configured:
            return ProviderHealth(False, "NOT_CONFIGURED", {**metadata, "status": "NOT_CONFIGURED"})
        return ProviderHealth(ok, detail, metadata)
