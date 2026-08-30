from __future__ import annotations

from typing import Any

import httpx
from production_domain.models import RetryCategory

from .errors import ProviderError
from .transport import (
    LiveProviderCallDenied,
    ProviderFixtureMiss,
    ProviderHttpRequest,
    ProviderMode,
    ProviderTransport,
)


class ProviderJsonClient:
    """Small JSON client shared by compatible provider adapters.

    Authentication stays inside the transport, so request objects, fixtures,
    error messages, and adapter logs never need to contain API keys.
    """

    def __init__(self, provider: str, transport: ProviderTransport, *, api_key_configured: bool):
        self.provider = provider
        self.transport = transport
        self.api_key_configured = api_key_configured

    @property
    def configured(self) -> bool:
        return self.transport.mode is not ProviderMode.LIVE or self.api_key_configured

    def assert_ready(self) -> None:
        """Fail local configuration/gate checks before a paid-call marker."""

        if not self.configured:
            raise ProviderError(
                f"{self.provider} provider API key is not configured",
                RetryCategory.PERMANENT_ERROR,
                code="PROVIDER_NOT_CONFIGURED",
            )
        try:
            self.transport.assert_ready()
        except LiveProviderCallDenied as exc:
            raise ProviderError(
                str(exc),
                RetryCategory.PERMANENT_ERROR,
                code="LIVE_PROVIDER_CALL_DENIED",
            ) from exc

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        submitted: bool = False,
    ) -> dict[str, Any]:
        self.assert_ready()
        try:
            response = await self.transport.send(
                ProviderHttpRequest(
                    method=method,
                    path=path,
                    json_body=json_body,
                    query=query or {},
                    headers=headers or {},
                )
            )
        except LiveProviderCallDenied as exc:
            raise ProviderError(
                str(exc),
                RetryCategory.PERMANENT_ERROR,
                code="LIVE_PROVIDER_CALL_DENIED",
            ) from exc
        except ProviderFixtureMiss as exc:
            raise ProviderError(
                str(exc),
                RetryCategory.PERMANENT_ERROR,
                code="PROVIDER_FIXTURE_MISSING",
                submitted=False,
            ) from exc
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(
                f"{self.provider} transport failed",
                RetryCategory.TRANSIENT_NETWORK,
                code="PROVIDER_NETWORK_ERROR",
                submitted=submitted,
            ) from exc

        if 200 <= response.status_code < 300:
            return response.json_body
        message = _safe_error_message(response.json_body)
        if response.status_code == 401:
            category, code = RetryCategory.CREDENTIAL_EXPIRED, "CREDENTIAL_EXPIRED"
        elif response.status_code == 429:
            category, code = RetryCategory.RATE_LIMIT, "RATE_LIMIT"
        elif response.status_code in {408, 502, 503, 504}:
            category, code = RetryCategory.PROVIDER_BUSY, "PROVIDER_BUSY"
        elif response.status_code in {400, 404, 409, 422}:
            category, code = RetryCategory.INVALID_REQUEST, "INVALID_REQUEST"
        elif response.status_code in {403, 451}:
            category, code = RetryCategory.CONTENT_REJECTED, "CONTENT_REJECTED"
        else:
            category, code = RetryCategory.PERMANENT_ERROR, "PROVIDER_ERROR"
        raise ProviderError(
            f"{self.provider} request failed: {message}",
            category,
            code=code,
            submitted=submitted and response.status_code in {408, 502, 503, 504},
        )


def wire_parameters(parameters: dict[str, Any] | None) -> dict[str, Any]:
    """Drop platform control fields before parameters reach a provider wire.

    Underscore-prefixed keys (the RunAPI edge-task handle rides chat
    parameters so that adapter can enforce its server-issued-task policy) are
    platform-internal. An adapter that splats parameters into a JSON body must
    filter them: a live refine died on `EdgeTask is not JSON serializable`
    inside httpx when the low-cost refiner role was bound to a non-edge
    provider (production, 2026-08-30). Mock transports never serialize, which
    is why no offline test ever saw it.
    """

    return {key: value for key, value in (parameters or {}).items() if not key.startswith("_")}


def _safe_error_message(body: dict[str, Any]) -> str:
    error = body.get("error")
    if isinstance(error, dict):
        value = error.get("message") or error.get("code") or "provider error"
    elif error:
        value = error
    else:
        value = body.get("message") or body.get("code") or "provider error"
    # Bound untrusted provider text before it can reach internal logs/UI.
    return str(value).replace("\n", " ")[:500]


def provider_health_metadata(client: ProviderJsonClient) -> tuple[bool, str, dict[str, Any]]:
    if not client.configured:
        return False, "NOT_CONFIGURED", {"status": "NOT_CONFIGURED", "mode": client.transport.mode.value}
    status = "MOCK" if client.transport.mode is ProviderMode.MOCK else client.transport.mode.value.upper()
    return True, status, {"status": status, "mode": client.transport.mode.value}


__all__ = ["ProviderJsonClient", "provider_health_metadata", "wire_parameters"]
