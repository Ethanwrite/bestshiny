"""Outbound HTTP to a user-supplied endpoint, fenced where the socket is opened.

A connection's base URL is typed by a user, so every request the platform
makes on its behalf is a request into whatever network that URL resolves to.
Checking the name before httpx resolves it again leaves a rebinding window;
checking the peer after the response has already sent the request - and the
user's key - to the internal host. The fence therefore lives in the network
backend: it resolves the host itself, refuses the connection unless every
address is public, and dials the address it validated. TLS still verifies the
certificate against the hostname, because httpcore takes the SNI name from the
request origin rather than from the address dialled.

Proxies are never consulted (``trust_env=False``): a proxy would receive the
connection instead, and the address check would be checking the proxy.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx

from .errors import ConnectionInvalid, EgressDenied, ResponseTooLarge

Resolver = Callable[[str, int], Awaitable[list[str]]]

#: Hostnames that name the local machine or an internal zone. The address
#: check catches all of them after resolution; refusing them at validation
#: time just gives the user a clear message instead of a failed test.
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


async def _system_resolver(host: str, port: int) -> list[str]:
    try:
        records = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise EgressDenied(f"the host {host[:120]!r} could not be resolved") from exc
    return list(dict.fromkeys(str(record[4][0]) for record in records))


def address_allowed(value: str, *, allow_private: bool) -> bool:
    """Whether a resolved address may be dialled.

    Public means ``is_global``, which already excludes loopback, RFC 1918,
    link-local (cloud metadata at 169.254.169.254), carrier-grade NAT
    (Alibaba's 100.100.100.200) and the benchmark range a fake-IP proxy uses.
    An IPv4-mapped IPv6 address is judged as the IPv4 address it carries.
    """

    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return False
    if allow_private:
        return True
    return ip.is_global


class FencedNetworkBackend(httpcore.AsyncNetworkBackend):
    """httpcore backend that only ever dials addresses it has validated."""

    def __init__(self, *, allow_private: bool = False, resolver: Resolver | None = None):
        self._inner = httpcore.AnyIOBackend()
        self._allow_private = allow_private
        self._resolve = resolver or _system_resolver

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._resolve(host, port)
        if not addresses:
            raise EgressDenied(f"the host {host[:120]!r} resolved to no addresses")
        refused = [
            address
            for address in addresses
            if not address_allowed(address, allow_private=self._allow_private)
        ]
        if refused:
            # All or nothing, as the provider-media fence does: a name that
            # resolves to one public and one private address is a rebinding
            # setup, not a network with a spare route.
            raise EgressDenied(
                f"the host {host[:120]!r} resolves to a non-public address; "
                "connections may only reach the public internet"
            )
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                last_error = exc
            except httpcore.ConnectTimeout as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise EgressDenied("connections cannot use a unix socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class FencedTransport(httpx.AsyncHTTPTransport):
    """``httpx.AsyncHTTPTransport`` whose connection pool dials through the fence.

    httpx does not expose the pool's network backend, so the pool is rebuilt
    with the same settings plus the fenced backend. A test pins the rebuild to
    the httpx release in use by asserting a loopback request is refused.
    """

    def __init__(self, *, allow_private: bool = False, resolver: Resolver | None = None):
        super().__init__(trust_env=False, retries=0)
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=5, keepalive_expiry=30)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=False,
            retries=0,
            network_backend=FencedNetworkBackend(allow_private=allow_private, resolver=resolver),
        )


def fenced_client(
    *,
    timeout_seconds: float,
    allow_private: bool = False,
    resolver: Resolver | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=FencedTransport(allow_private=allow_private, resolver=resolver),
        timeout=httpx.Timeout(timeout_seconds, connect=min(15.0, timeout_seconds)),
        follow_redirects=False,
        trust_env=False,
    )


def normalize_base_url(value: str, *, allow_private: bool = False) -> str:
    """Validate and canonicalize a connection's base URL before it is stored.

    HTTPS only, no credentials, query or fragment, a real hostname. Plain HTTP
    is accepted only where private networks are (local development against a
    self-hosted server). The address itself is checked again at every
    connection, so this is about clear errors, not the security boundary.
    """

    raw = (value or "").strip()
    if not raw:
        raise ConnectionInvalid("a base URL is required")
    if len(raw) > 500:
        raise ConnectionInvalid("the base URL is longer than 500 characters")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise ConnectionInvalid("the base URL is not a valid URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in {"https", "http"}:
        raise ConnectionInvalid("the base URL must start with https://")
    if scheme == "http" and not allow_private:
        raise ConnectionInvalid("the base URL must use https://")
    if parts.username or parts.password:
        raise ConnectionInvalid("the base URL cannot contain a username or password")
    if parts.query or parts.fragment:
        raise ConnectionInvalid("the base URL cannot contain a query string or fragment")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise ConnectionInvalid("the base URL has no host")
    if not allow_private:
        if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
            raise ConnectionInvalid("the base URL must be a public internet host")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not address_allowed(host, allow_private=False):
            raise ConnectionInvalid("the base URL must be a public internet host")
    netloc = host if ":" not in host else f"[{host}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


async def read_bounded(response: httpx.Response, limit: int) -> bytes:
    """Read a streamed response body, refusing one larger than ``limit`` bytes."""

    declared = response.headers.get("content-length")
    if declared:
        try:
            if int(declared) > limit:
                raise ResponseTooLarge(f"the provider response exceeds {limit} bytes")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge(f"the provider response exceeds {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = [
    "FencedNetworkBackend",
    "FencedTransport",
    "Resolver",
    "address_allowed",
    "fenced_client",
    "host_of",
    "normalize_base_url",
    "read_bounded",
]
