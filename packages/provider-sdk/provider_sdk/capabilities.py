from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any


class ProviderCapability(StrEnum):
    CHAT = "chat"
    RESPONSES = "responses"
    EMBEDDINGS = "embeddings"
    IMAGE = "image"
    VIDEO = "video"


class CapabilityProviderNotFound(LookupError):
    pass


class ProviderCapabilityCatalog:
    """Infrastructure lookup used after ModelRole resolution selects a provider."""

    def __init__(self) -> None:
        self._providers: dict[str, object] = {}
        self._capabilities: dict[str, frozenset[str]] = {}

    def register(self, name: str, implementation: object, capabilities: set[str]) -> None:
        normalized = name.strip()
        if not normalized or not capabilities:
            raise ValueError("provider name and capabilities are required")
        if normalized in self._providers:
            raise ValueError(f"provider capability client already registered: {normalized}")
        self._providers[normalized] = implementation
        self._capabilities[normalized] = frozenset(capabilities)

    def resolve(self, provider: str, capability: ProviderCapability | str) -> object:
        normalized = str(capability)
        if provider not in self._providers:
            raise CapabilityProviderNotFound(f"provider client is not registered: {provider}")
        implementation = self._providers[provider]
        deployment_ready = getattr(
            implementation,
            "capability_configured",
            getattr(implementation, "configured", True),
        )
        if deployment_ready is False:
            raise CapabilityProviderNotFound(f"provider client is not configured: {provider}")
        if normalized not in self._capabilities[provider]:
            raise CapabilityProviderNotFound(
                f"provider {provider} does not implement capability {normalized}"
            )
        return implementation

    def capabilities(self, provider: str) -> frozenset[str]:
        return self._capabilities.get(provider, frozenset())


class ChatCapability(ABC):
    @abstractmethod
    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class ResponsesCapability(ABC):
    @abstractmethod
    async def create_response(
        self,
        *,
        model: str,
        input_value: str | list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class EmbeddingCapability(ABC):
    @abstractmethod
    async def create_embeddings(
        self,
        *,
        model: str,
        inputs: str | list[str] | list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


__all__ = [
    "CapabilityProviderNotFound",
    "ChatCapability",
    "EmbeddingCapability",
    "ProviderCapability",
    "ProviderCapabilityCatalog",
    "ResponsesCapability",
]
