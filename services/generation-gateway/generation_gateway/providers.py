from __future__ import annotations

from provider_sdk import GenerationProvider


class ProviderRouter:
    def __init__(self) -> None:
        self._providers: dict[str, GenerationProvider] = {}

    def register(self, provider: GenerationProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"provider already registered: {provider.name}")
        self._providers[provider.name] = provider

    def get(self, name: str) -> GenerationProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise LookupError(f"provider is not configured: {name}") from exc

    def list(self) -> list[str]:
        return sorted(self._providers)
