"""The provider transport a connection's adapters and chat clients send through.

It implements ``provider_sdk.ProviderTransport``, so the platform's reviewed
adapters (OpenRouter, Ark, Wan) run unchanged on a workspace's key: the key
lives here and is added to each request as a header, every connection passes
the egress fence, and response bodies are bounded while streaming.

It does not consult the platform's ``LiveProviderGate``. That gate exists so
the platform cannot spend its own provider money by accident; a connection
spends the workspace's money at the workspace's explicit request, and the
connection kill switch (``USER_CONNECTIONS_ENABLED``) is checked before any
connection-backed provider is built.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

import httpx
from provider_sdk import ProviderError, RetryCategory
from provider_sdk.transport import (
    ProviderHttpRequest,
    ProviderHttpResponse,
    ProviderMode,
    ProviderTransport,
)

from .egress import Resolver, fenced_client, read_bounded
from .errors import EgressDenied, ResponseTooLarge
from .protocols import AuthStyle

ClientFactory = Callable[[], httpx.AsyncClient]


class ConnectionTransport(ProviderTransport):
    # LIVE, because it is: `ProviderJsonClient.configured` treats a non-live
    # transport as configured without a key, and a connection always has one.
    mode = ProviderMode.LIVE

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        auth: AuthStyle,
        timeout_seconds: float = 120,
        max_response_bytes: int = 64 * 1024 * 1024,
        allow_private: bool = False,
        default_headers: Mapping[str, str] | None = None,
        resolver: Resolver | None = None,
        client_factory: ClientFactory | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.auth = auth
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.default_headers = dict(default_headers or {})
        self._client_factory = client_factory or (
            lambda: fenced_client(
                timeout_seconds=timeout_seconds,
                allow_private=allow_private,
                resolver=resolver,
            )
        )

    def __repr__(self) -> str:  # never the key
        return f"ConnectionTransport(base_url={self.base_url!r}, auth={self.auth.value!r})"

    def auth_headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        if self.auth is AuthStyle.X_API_KEY:
            return {"x-api-key": self._api_key}
        return {"Authorization": f"Bearer {self._api_key}"}

    async def send(self, request: ProviderHttpRequest) -> ProviderHttpResponse:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self.default_headers,
            **request.headers,
            **self.auth_headers(),
        }
        try:
            async with self._client_factory() as client:
                async with client.stream(
                    request.method,
                    f"{self.base_url}{request.path}",
                    json=request.json_body,
                    params=request.query or None,
                    headers=headers,
                ) as response:
                    body_bytes = await read_bounded(response, self.max_response_bytes)
                    status_code = response.status_code
                    response_headers = {key: value for key, value in response.headers.items()}
        except ResponseTooLarge as exc:
            # The request reached the provider; for a submission that means it
            # may have been accepted and billed, so it must not read as unsent.
            raise ProviderError(
                str(exc),
                RetryCategory.PERMANENT_ERROR,
                code="CONNECTION_RESPONSE_TOO_LARGE",
                submitted=request.method.upper() != "GET",
            ) from exc
        except EgressDenied as exc:
            raise ProviderError(
                str(exc),
                RetryCategory.PERMANENT_ERROR,
                code="CONNECTION_EGRESS_DENIED",
                submitted=False,
            ) from exc
        if 300 <= status_code < 400:
            # Redirects are never followed for an API call: a provider that
            # redirects its API elsewhere must be configured with that URL.
            return ProviderHttpResponse(
                status_code=502,
                json_body={
                    "error": {
                        "message": (
                            f"the endpoint answered with a redirect ({status_code}); "
                            "use the final URL as the connection's base URL"
                        )
                    }
                },
                headers=response_headers,
            )
        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except (UnicodeDecodeError, ValueError):
            body = {"error": {"message": f"the endpoint returned a non-JSON response (HTTP {status_code})"}}
        if not isinstance(body, dict):
            body = {"data": body}
        return ProviderHttpResponse(status_code=status_code, json_body=body, headers=response_headers)


__all__ = ["ClientFactory", "ConnectionTransport"]
