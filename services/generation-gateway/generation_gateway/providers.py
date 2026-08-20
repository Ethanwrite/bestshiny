from __future__ import annotations

import builtins

from provider_sdk import GenerationProvider, NotConfiguredProvider


class GenerationTargetError(LookupError):
    """A generation target is unknown or cannot be executed by this deployment."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ProviderRouter:
    def __init__(self) -> None:
        self._providers: dict[str, GenerationProvider] = {}
        self._models: dict[tuple[str, str, str], bool] = {}

    def register(self, provider: GenerationProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"provider already registered: {provider.name}")
        self._providers[provider.name] = provider

    def get(self, name: str) -> GenerationProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise LookupError(f"provider is not configured: {name}") from exc

    def register_model(
        self,
        provider: str,
        model: str,
        media_type: str,
        *,
        available: bool = True,
    ) -> None:
        if media_type not in {"image", "video"}:
            raise ValueError(f"unsupported generation media type: {media_type}")
        key = (provider.strip(), media_type, model.strip())
        if not key[0] or not key[2]:
            raise ValueError("provider and model names cannot be empty")
        existing = self._models.get(key)
        if existing is not None and existing != available:
            raise ValueError(f"generation model availability already registered: {provider}:{model}")
        self._models[key] = available

    def validate_target(self, provider: str, model: str, media_type: str) -> GenerationProvider:
        implementation = self._providers.get(provider)
        if implementation is None:
            raise GenerationTargetError(
                "PROVIDER_NOT_REGISTERED",
                f"selected provider is not registered: {provider}",
            )
        if isinstance(implementation, NotConfiguredProvider):
            raise GenerationTargetError(
                "PROVIDER_NOT_CONFIGURED",
                f"selected provider has no configured generation transport: {provider}",
            )
        availability = self._models.get((provider, media_type, model))
        if availability is None:
            raise GenerationTargetError(
                "MODEL_NOT_REGISTERED",
                f"selected {media_type} model is not registered for this provider: {provider}:{model}",
            )
        if not availability:
            raise GenerationTargetError(
                "MODEL_NOT_AVAILABLE",
                f"selected {media_type} model is not available: {provider}:{model}",
            )
        return implementation

    def list(self) -> list[str]:
        return sorted(self._providers)

    def is_configured(self, name: str) -> bool:
        provider = self._providers.get(name)
        return provider is not None and not isinstance(provider, NotConfiguredProvider)

    def configured(self) -> builtins.list[str]:
        return sorted(name for name in self._providers if self.is_configured(name))
