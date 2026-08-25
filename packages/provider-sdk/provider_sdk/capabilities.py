from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from .base import GenerationProvider


class ProviderCapability(StrEnum):
    CHAT = "chat"
    RESPONSES = "responses"
    EMBEDDINGS = "embeddings"
    IMAGE = "image"
    VIDEO = "video"


class CapabilityProviderNotFound(LookupError):
    pass


class ProviderCapabilityCatalog:
    """Adapter-interface lookup after the model registry selects a model.

    This catalog describes Python interfaces implemented by a transport. It is
    deliberately not a source of model capability truth; supported operations
    are authorized by the persisted ModelCapabilityRegistry first.
    """

    def __init__(self) -> None:
        self._providers: dict[str, object] = {}
        self._capabilities: dict[str, frozenset[str]] = {}

    def register(
        self,
        name: str,
        implementation: object,
        capabilities: set[str] | None = None,
    ) -> None:
        normalized = name.strip()
        if not normalized:
            raise ValueError("provider name is required")
        if normalized in self._providers:
            raise ValueError(f"provider capability client already registered: {normalized}")
        implemented: set[str] = set()
        if isinstance(implementation, ChatCapability):
            implemented.add(ProviderCapability.CHAT.value)
        if isinstance(implementation, ResponsesCapability):
            implemented.add(ProviderCapability.RESPONSES.value)
        if isinstance(implementation, EmbeddingCapability):
            implemented.add(ProviderCapability.EMBEDDINGS.value)
        if isinstance(implementation, GenerationProvider):
            implemented.update({ProviderCapability.IMAGE.value, ProviderCapability.VIDEO.value})
        if capabilities is not None and not capabilities.issubset(implemented):
            unsupported = ", ".join(sorted(capabilities.difference(implemented)))
            raise ValueError(f"adapter does not implement claimed interfaces: {unsupported}")
        if not implemented:
            raise ValueError("provider implementation exposes no supported interface")
        self._providers[normalized] = implementation
        self._capabilities[normalized] = frozenset(implemented)

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

    def implementation(self, provider: str) -> object | None:
        """The registered adapter, whatever interfaces it implements.

        `resolve` answers "can this provider serve this capability"; this
        answers the narrower "is there an adapter here at all". A chat-only
        model has no entry in the generation router, and reading its absence
        there as "no transport" marks a perfectly configured model dead.
        """

        return self._providers.get(provider)

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
