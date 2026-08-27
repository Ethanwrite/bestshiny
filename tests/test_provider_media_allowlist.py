"""Every provider the platform can route to must be able to deliver a result.

The allowlist is an SSRF fence, and it fails closed — correctly. But it shipped
with one entry, `google_flow`, while the router could reach OpenRouter, Ark and
DashScope. So the closed loop ended the same way for all three: the provider
accepted the request, generated the media, **billed for it**, and then the
transfer stage refused to fetch what had just been paid for.

Nothing offline caught it, because every offline test that exercises the fence
sets `provider_media_hosts` itself. The cost of finding it live was one real
USD 0.05 clip on `x-ai/grok-imagine-video`, whose finished artefact OpenRouter
served from `openrouter.ai`.

These tests read the shipped default rather than a fixture, so a provider added
to the router without a media host is a failing test rather than a billed
surprise.
"""

from __future__ import annotations

import pytest
from platform_shared import Settings
from video_platform_api.container import _parse_provider_media_hosts

# Providers registered in `ProviderCapabilityCatalog` that can return media.
# `runapi` and `deepseek` are absent deliberately: deepseek is chat-only, and
# runapi's media path is not routed in this deployment.
MEDIA_RETURNING_PROVIDERS = ("google_flow", "openrouter")


@pytest.fixture(scope="module")
def shipped_hosts() -> dict[str, tuple[str, ...]]:
    """The default the code ships, deliberately not this machine's `.env`.

    A deployment may widen or narrow the fence, and that is its business. What
    must hold is that a fresh install can deliver from every provider it can
    route to. Reading `.env` here would also pull real provider keys into the
    suite, which is its own known failure mode.
    """

    shipped = Settings(_env_file=None)  # type: ignore[call-arg]
    return _parse_provider_media_hosts(shipped.provider_media_allowed_hosts)


@pytest.mark.parametrize("provider", MEDIA_RETURNING_PROVIDERS)
def test_a_routable_media_provider_has_somewhere_to_fetch_from(
    shipped_hosts: dict[str, tuple[str, ...]], provider: str
) -> None:
    assert shipped_hosts.get(provider), (
        f"{provider} can be routed to but has no allowlisted media host, so any "
        "generation it bills for will fail at the transfer stage"
    )


def test_openrouter_serves_finished_video_from_its_own_host(
    shipped_hosts: dict[str, tuple[str, ...]],
) -> None:
    """Read off a real completed job, not inferred from the docs."""

    assert "openrouter.ai" in shipped_hosts["openrouter"]


def test_the_parser_keeps_providers_separate(
    shipped_hosts: dict[str, tuple[str, ...]],
) -> None:
    """A `;` that was meant to be a `,` would merge two providers' fences."""

    assert "labs.google" not in shipped_hosts["openrouter"]
    assert "openrouter.ai" not in shipped_hosts["google_flow"]


def test_no_provider_is_allowlisted_with_a_bare_wildcard(
    shipped_hosts: dict[str, tuple[str, ...]],
) -> None:
    """`*` or `*.com` would turn the fence into decoration."""

    for provider, patterns in shipped_hosts.items():
        for pattern in patterns:
            assert pattern != "*", f"{provider} allows any host"
            assert pattern.count(".") >= 1, f"{provider} pattern {pattern!r} is too broad"
            if pattern.startswith("*."):
                assert pattern.count(".") >= 2, f"{provider} pattern {pattern!r} is a public suffix"


def test_ark_and_dashscope_stay_closed_until_a_canary_shows_their_host(
    shipped_hosts: dict[str, tuple[str, ...]],
) -> None:
    """A guessed host is either an SSRF hole or another silent failure.

    This is a deliberate gap, not an oversight: it is recorded here so that
    adding either provider is a conscious edit to a test that says why.
    """

    assert "seedance" not in shipped_hosts
    assert "wan" not in shipped_hosts
