"""Authenticating an artefact fetch without leaking the key across a redirect.

OpenRouter serves a finished video from `GET /api/v1/videos/{id}/content`, which
answers 401 without the API key. The fetcher sent no credential at all, so a
video generated there was billed and then unretrievable — proven in production
on 2026-08-29 with `alibaba/wan-3.0`, provider job `ZYwPpPq4r9wgJHbsyBOs`:
CONFIRMED and charged, then `POLL_PROCESSING_ERROR ... 401 Unauthorized`.

The fix has to buy that without opening the hole next to it. A redirect from an
artefact endpoint commonly lands on a signed CDN that needs no authorization,
and forwarding a bearer token to whatever host a provider names is how
credentials leak. So the credential is presented to the host the provider's own
API named, and to nothing else.
"""

from __future__ import annotations

import httpx
import pytest
from media_service import registry as media_registry_module
from media_service.registry import RemoteMediaSecurityError


def _resolver(mapping):  # type: ignore[no-untyped-def]
    def resolve(host: str, port: int, **_kwargs):  # type: ignore[no-untyped-def]
        return [(2, 1, 6, "", (mapping[host], port))]

    return resolve


def _install(monkeypatch, handler, hosts) -> list[httpx.Request]:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(media_registry_module.socket, "getaddrinfo", _resolver(hosts))
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        media_registry_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    return seen


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00"
    b"\x00\x00IEND\xaeB`\x82"
)


@pytest.mark.asyncio
async def test_the_provider_that_gates_its_artefacts_is_authenticated(  # type: ignore[no-untyped-def]
    container, project, monkeypatch
) -> None:
    container.media.provider_media_hosts = {"openrouter": ("openrouter.ai",)}
    container.media.provider_media_credentials = {"openrouter": "sk-or-secret"}

    seen = _install(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=PNG, headers={"content-type": "image/png"}, request=request
        ),
        {"openrouter.ai": "104.18.3.115"},
    )

    await container.media.download_and_register(
        project.id,
        "IMAGE",
        "https://openrouter.ai/api/v1/videos/abc/content",
        filename="out.png",
        provider="openrouter",
        provider_media_id="pm-1",
    )

    assert seen[0].headers.get("authorization") == "Bearer sk-or-secret"


@pytest.mark.asyncio
async def test_the_credential_does_not_follow_a_redirect_to_another_host(  # type: ignore[no-untyped-def]
    container, project, monkeypatch
) -> None:
    """The whole point of doing this by hand rather than with follow_redirects."""

    container.media.provider_media_hosts = {"openrouter": ("openrouter.ai", "cdn.example.com")}
    container.media.provider_media_credentials = {"openrouter": "sk-or-secret"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(
                302, headers={"location": "https://cdn.example.com/signed/out.png"}, request=request
            )
        return httpx.Response(
            200, content=PNG, headers={"content-type": "image/png"}, request=request
        )

    seen = _install(
        monkeypatch, handler, {"openrouter.ai": "104.18.3.115", "cdn.example.com": "93.184.216.34"}
    )

    await container.media.download_and_register(
        project.id,
        "IMAGE",
        "https://openrouter.ai/api/v1/videos/abc/content",
        filename="out.png",
        provider="openrouter",
        provider_media_id="pm-2",
    )

    origin, redirected = seen[0], seen[1]
    assert origin.url.host == "openrouter.ai"
    assert origin.headers.get("authorization") == "Bearer sk-or-secret"
    assert redirected.url.host == "cdn.example.com"
    assert "authorization" not in redirected.headers, "the key must not cross a host boundary"


@pytest.mark.asyncio
async def test_a_provider_with_no_configured_credential_stays_anonymous(  # type: ignore[no-untyped-def]
    container, project, monkeypatch
) -> None:
    # Most providers hand back a signed CDN URL that carries its own
    # authorization. Presenting a bearer token there buys nothing and exposes
    # the key to a host that never asked for it.
    container.media.provider_media_hosts = {"google_flow": ("media.example.com",)}
    container.media.provider_media_credentials = {"openrouter": "sk-or-secret"}

    seen = _install(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=PNG, headers={"content-type": "image/png"}, request=request
        ),
        {"media.example.com": "93.184.216.34"},
    )

    await container.media.download_and_register(
        project.id,
        "IMAGE",
        "https://media.example.com/out.png",
        filename="out.png",
        provider="google_flow",
        provider_media_id="pm-3",
    )

    assert "authorization" not in seen[0].headers


@pytest.mark.asyncio
async def test_authentication_does_not_loosen_the_ssrf_fence(container, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A credentialed fetch is still refused when the redirect target is private."""

    container.media.provider_media_hosts = {"openrouter": ("openrouter.ai", "169.254.169.254")}
    container.media.provider_media_credentials = {"openrouter": "sk-or-secret"}

    seen = _install(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": "https://169.254.169.254/latest/meta-data"}, request=request
        ),
        {"openrouter.ai": "104.18.3.115", "169.254.169.254": "169.254.169.254"},
    )

    with pytest.raises(RemoteMediaSecurityError, match="non-public"):
        await container.media.download_and_register(
            "unused-project",
            "IMAGE",
            "https://openrouter.ai/api/v1/videos/abc/content",
            filename="out.png",
            provider="openrouter",
            provider_media_id="pm-4",
        )

    # Refused before the second hop was ever dispatched, so the key was never
    # offered to the metadata address either.
    assert len(seen) == 1
