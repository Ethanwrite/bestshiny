from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

import httpx

LIVE_PROVIDER_CONFIRMATION = "I_UNDERSTAND_THIS_COSTS_MONEY"


class ProviderMode(StrEnum):
    MOCK = "mock"
    RECORDED = "recorded"
    LIVE = "live"


class LiveProviderCallDenied(RuntimeError):
    """Raised before a transport can make a paid or otherwise live provider call."""


class ProviderFixtureMiss(LookupError):
    """A mock/recorded transport received a request with no approved fixture."""


@dataclass(frozen=True)
class LiveProviderSettings:
    provider_mode: ProviderMode | str = ProviderMode.MOCK
    allow_live_provider_calls: bool = False
    live_provider_confirmation: str = ""


class LiveProviderGate:
    """The single three-part gate for every outbound provider transport.

    Constructing a live transport is deliberately impossible unless all three
    controls are present. Provider-specific switches may add restrictions but
    may never bypass this gate.
    """

    def __init__(self, settings: LiveProviderSettings):
        self.settings = settings

    @property
    def mode(self) -> ProviderMode:
        try:
            return ProviderMode(self.settings.provider_mode)
        except ValueError as exc:
            raise LiveProviderCallDenied("PROVIDER_MODE must be mock, recorded, or live") from exc

    def assert_live_allowed(self) -> None:
        failures: list[str] = []
        if self.mode is not ProviderMode.LIVE:
            failures.append("PROVIDER_MODE=live")
        if self.settings.allow_live_provider_calls is not True:
            failures.append("ALLOW_LIVE_PROVIDER_CALLS=true")
        if self.settings.live_provider_confirmation != LIVE_PROVIDER_CONFIRMATION:
            failures.append(f"LIVE_PROVIDER_CONFIRMATION={LIVE_PROVIDER_CONFIRMATION}")
        if failures:
            raise LiveProviderCallDenied("live provider call denied; missing: " + ", ".join(failures))


@dataclass(frozen=True)
class ProviderHttpRequest:
    method: str
    path: str
    json_body: dict[str, Any] | None = None
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parsed = urlparse(self.path)
        if parsed.scheme or parsed.netloc or not self.path.startswith("/"):
            raise ValueError("provider request path must be an absolute path, not a URL")


@dataclass(frozen=True)
class ProviderHttpResponse:
    status_code: int
    json_body: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


class ProviderTransport(ABC):
    mode: ProviderMode

    def assert_ready(self) -> None:
        """Validate local transport gates before a durable paid-call boundary.

        Non-live transports have no paid remote boundary. Live transports
        override this hook so adapters can fail before reserving/marking spend.
        The transport still repeats the check in ``send`` as defense in depth.
        """

        return None

    @abstractmethod
    async def send(self, request: ProviderHttpRequest) -> ProviderHttpResponse: ...


FixtureHandler = Callable[[ProviderHttpRequest], ProviderHttpResponse]


class MockProviderTransport(ProviderTransport):
    """Deterministic, network-free transport used by normal tests and development."""

    mode = ProviderMode.MOCK

    def __init__(
        self,
        fixtures: Mapping[tuple[str, str], ProviderHttpResponse] | None = None,
        *,
        handler: FixtureHandler | None = None,
    ):
        self._fixtures = {
            (method.upper(), path): response for (method, path), response in (fixtures or {}).items()
        }
        self._handler = handler
        self.requests: list[ProviderHttpRequest] = []

    def add_fixture(self, method: str, path: str, response: ProviderHttpResponse) -> None:
        self._fixtures[(method.upper(), path)] = response

    async def send(self, request: ProviderHttpRequest) -> ProviderHttpResponse:
        self.requests.append(request)
        if self._handler is not None:
            return self._handler(request)
        try:
            return self._fixtures[(request.method.upper(), request.path)]
        except KeyError as exc:
            raise ProviderFixtureMiss(
                f"no mock provider fixture for {request.method.upper()} {request.path}"
            ) from exc


class RecordedFixtureTransport(ProviderTransport):
    """Read-only captured-response transport; it never falls through to HTTP."""

    mode = ProviderMode.RECORDED

    def __init__(self, fixtures: Mapping[tuple[str, str], ProviderHttpResponse]):
        self._fixtures = {(method.upper(), path): response for (method, path), response in fixtures.items()}
        self.requests: list[ProviderHttpRequest] = []

    async def send(self, request: ProviderHttpRequest) -> ProviderHttpResponse:
        self.requests.append(request)
        try:
            return self._fixtures[(request.method.upper(), request.path)]
        except KeyError as exc:
            raise ProviderFixtureMiss(
                f"no recorded provider fixture for {request.method.upper()} {request.path}"
            ) from exc


class HttpxLiveTransport(ProviderTransport):
    mode = ProviderMode.LIVE

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        gate: LiveProviderGate,
        timeout_seconds: float = 120,
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        default_headers: Mapping[str, str] | None = None,
    ):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("live provider base URL must be an HTTPS origin without credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("live provider base URL cannot include a query or fragment")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.auth_header = auth_header
        self.auth_scheme = auth_scheme
        self.default_headers = dict(default_headers or {})
        self._gate = gate

    def assert_ready(self) -> None:
        self._gate.assert_live_allowed()

    async def send(self, request: ProviderHttpRequest) -> ProviderHttpResponse:
        self._gate.assert_live_allowed()
        headers = {"Content-Type": "application/json", **self.default_headers, **request.headers}
        if self._api_key:
            headers[self.auth_header] = f"{self.auth_scheme} {self._api_key}".strip()
        async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False) as client:
            response = await client.request(
                request.method,
                f"{self.base_url}{request.path}",
                json=request.json_body,
                params=request.query,
                headers=headers,
            )
        try:
            body = response.json()
        except ValueError:
            body = {"error": {"message": "provider returned a non-JSON response"}}
        if not isinstance(body, dict):
            body = {"data": body}
        return ProviderHttpResponse(
            status_code=response.status_code,
            json_body=body,
            headers={key: value for key, value in response.headers.items()},
        )


def create_provider_transport(
    *,
    settings: LiveProviderSettings,
    base_url: str,
    api_key: str,
    fixtures: Mapping[tuple[str, str], ProviderHttpResponse] | None = None,
    timeout_seconds: float = 120,
    default_headers: Mapping[str, str] | None = None,
) -> ProviderTransport:
    gate = LiveProviderGate(settings)
    if gate.mode is ProviderMode.MOCK:
        return MockProviderTransport(fixtures)
    if gate.mode is ProviderMode.RECORDED:
        return RecordedFixtureTransport(fixtures or {})
    return HttpxLiveTransport(
        base_url=base_url,
        api_key=api_key,
        gate=gate,
        timeout_seconds=timeout_seconds,
        default_headers=default_headers,
    )


__all__ = [
    "HttpxLiveTransport",
    "LIVE_PROVIDER_CONFIRMATION",
    "LiveProviderCallDenied",
    "LiveProviderGate",
    "LiveProviderSettings",
    "MockProviderTransport",
    "ProviderFixtureMiss",
    "ProviderHttpRequest",
    "ProviderHttpResponse",
    "ProviderMode",
    "ProviderTransport",
    "RecordedFixtureTransport",
    "create_provider_transport",
]
