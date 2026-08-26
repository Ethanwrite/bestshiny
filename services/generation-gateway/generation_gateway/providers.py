from __future__ import annotations

import builtins
from collections.abc import Sequence
from typing import Protocol

from provider_sdk import (
    AssetCriticality,
    GenerationProvider,
    NotConfiguredProvider,
    ProviderTrustLevel,
    ProviderTrustViolation,
    assert_provider_can_handle,
)


class ModelCapabilityProfileView(Protocol):
    @property
    def status(self) -> str: ...

    @property
    def supported_operations(self) -> Sequence[str]: ...

    @property
    def provider_trust_level(self) -> ProviderTrustLevel: ...

    @property
    def criticality_allowed(self) -> Sequence[AssetCriticality]: ...


class ModelCapabilityRegistryView(Protocol):
    def get(self, model_id: str, provider: str | None = None) -> ModelCapabilityProfileView | None: ...

    def register_test_profile(
        self, provider: str, model_id: str, media_type: str
    ) -> ModelCapabilityProfileView: ...

    def provider_enabled(self, provider: str) -> bool: ...


class GenerationTargetError(LookupError):
    """A generation target is unknown or cannot be executed by this deployment."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ProviderRouter:
    def __init__(
        self,
        capability_registry: ModelCapabilityRegistryView | None = None,
        *,
        allow_test_target_registration: bool = False,
    ) -> None:
        self._providers: dict[str, GenerationProvider] = {}
        self._availability: dict[tuple[str, str, str], bool] = {}
        self._capability_registry = capability_registry
        self._allow_test_target_registration = allow_test_target_registration

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
        if self._capability_registry is not None:
            profile = self._capability_registry.get(model, provider)
            if profile is None:
                if not self._allow_test_target_registration:
                    raise ValueError("generation targets require an authoritative model capability profile")
                profile = self._capability_registry.register_test_profile(
                    provider,
                    model,
                    media_type,
                )
            elif (
                f"{media_type}_generation" not in profile.supported_operations
                and self._allow_test_target_registration
            ):
                profile = self._capability_registry.register_test_profile(
                    provider,
                    model,
                    media_type,
                )
            if f"{media_type}_generation" not in profile.supported_operations:
                raise ValueError("runtime availability cannot expand registry operations")
        existing = self._availability.get(key)
        if existing is not None and existing != available:
            raise ValueError(f"generation model availability already registered: {provider}:{model}")
        self._availability[key] = available

    def mark_model_unavailable(self, provider: str, model: str, media_type: str) -> None:
        """Narrow a registered target's runtime availability without changing capabilities."""

        key = (provider.strip(), media_type, model.strip())
        if key not in self._availability:
            raise LookupError(f"generation model availability is not registered: {provider}:{model}")
        self._availability[key] = False

    def validate_target(
        self,
        provider: str,
        model: str,
        media_type: str,
        *,
        asset_criticality: AssetCriticality | str | None = None,
    ) -> GenerationProvider:
        implementation = self._providers.get(provider)
        if implementation is None:
            raise GenerationTargetError(
                "PROVIDER_NOT_REGISTERED",
                f"selected provider is not registered: {provider}",
            )
        if (
            self._capability_registry is not None
            and hasattr(self._capability_registry, "provider_enabled")
            and not self._capability_registry.provider_enabled(provider)
        ):
            raise GenerationTargetError(
                "PROVIDER_DISABLED",
                f"selected provider is disabled by platform operations: {provider}",
            )
        if (
            isinstance(implementation, NotConfiguredProvider)
            or getattr(implementation, "configured", True) is False
        ):
            raise GenerationTargetError(
                "PROVIDER_NOT_CONFIGURED",
                f"selected provider has no configured generation transport: {provider}",
            )
        profile = self._capability_registry.get(model, provider) if self._capability_registry else None
        if self._capability_registry is not None and profile is None:
            raise GenerationTargetError(
                "MODEL_NOT_REGISTERED",
                f"selected {media_type} model is not registered in the authoritative "
                "capability registry: "
                f"{provider}:{model}",
            )
        required_operation = f"{media_type}_generation"
        if profile is not None and required_operation not in profile.supported_operations:
            raise GenerationTargetError(
                "CAPABILITY_NOT_SUPPORTED",
                f"selected model does not support {required_operation}: {provider}:{model}",
            )
        if profile is not None and profile.status == "disabled":
            raise GenerationTargetError(
                "MODEL_NOT_AVAILABLE",
                f"selected {media_type} model is disabled: {provider}:{model}",
            )
        availability = self._availability.get((provider, media_type, model))
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
        if asset_criticality is not None:
            try:
                if profile is not None and AssetCriticality(asset_criticality) not in set(
                    profile.criticality_allowed
                ):
                    raise ProviderTrustViolation(
                        f"model {provider}:{model} does not allow "
                        f"{AssetCriticality(asset_criticality).value} assets"
                    )
                assert_provider_can_handle(
                    (
                        profile.provider_trust_level
                        if profile is not None
                        else getattr(implementation, "trust_level", ProviderTrustLevel.PRODUCTION)
                    ),
                    asset_criticality,
                )
            except ProviderTrustViolation as exc:
                raise GenerationTargetError("PROVIDER_TRUST_DENIED", str(exc)) from exc
        return implementation

    def list(self) -> list[str]:
        return sorted(self._providers)

    def is_configured(self, name: str) -> bool:
        provider = self._providers.get(name)
        return (
            provider is not None
            and not isinstance(provider, NotConfiguredProvider)
            and getattr(provider, "configured", True) is not False
        )

    def configured(self) -> builtins.list[str]:
        return sorted(name for name in self._providers if self.is_configured(name))
