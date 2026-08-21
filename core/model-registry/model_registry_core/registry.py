from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, Literal, cast

from platform_database import Database
from production_domain.models import (
    ModelCapabilityProfile as ModelCapabilityProfileRow,
)
from production_domain.models import (
    ModelDefinition,
)
from provider_sdk import AssetCriticality, ProviderTrustLevel
from sqlalchemy import select

from .schemas import ModelCapabilityProfile


class CapabilityObservationConflict(ValueError):
    """A runtime observation attempted to expand the reviewed registry truth."""


_BOOLEAN_CAPABILITIES = {
    "supports_t2v",
    "supports_i2v",
    "supports_v2v",
    "supports_reference_image",
    "supports_multi_reference",
    "supports_start_frame",
    "supports_end_frame",
    "supports_start_end",
    "supports_character_reference",
    "supports_video_extension",
    "supports_camera_instruction",
    "supports_audio",
    "supports_text_rendering",
}


class ModelCapabilityRegistry:
    """Single runtime truth for model operations, limits, and quality priors.

    Version-controlled configuration is only a bootstrap input. Every runtime
    read comes from the persisted one-to-one profile joined to its current
    ModelDefinition identity and enablement state.
    """

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _to_profile(
        definition: ModelDefinition,
        row: ModelCapabilityProfileRow,
    ) -> ModelCapabilityProfile:
        if row.confidence_level not in {"initial", "experimental", "validated"}:
            raise ValueError(
                f"unknown capability confidence level for {definition.provider_model_id}: "
                f"{row.confidence_level}"
            )
        status: Literal["active", "disabled", "experimental"] = (
            "disabled"
            if not definition.enabled
            else "experimental"
            if row.confidence_level == "experimental"
            else "active"
        )
        return ModelCapabilityProfile(
            model_definition_id=definition.id,
            logical_name=definition.logical_name,
            model_id=definition.provider_model_id,
            provider=definition.provider,
            modality=definition.modality,
            version=row.profile_version,
            status=status,
            confidence_level=cast(Literal["initial", "experimental", "validated"], row.confidence_level),
            supported_operations=list(row.supported_operations),
            supports_t2v=row.supports_t2v,
            supports_i2v=row.supports_i2v,
            supports_v2v=row.supports_v2v,
            supports_reference_image=row.supports_reference_image,
            supports_multi_reference=row.supports_multi_reference,
            supports_start_frame=row.supports_start_frame,
            supports_end_frame=row.supports_end_frame,
            supports_start_end=row.supports_start_end,
            supports_character_reference=row.supports_character_reference,
            supports_video_extension=row.supports_video_extension,
            supports_camera_instruction=row.supports_camera_instruction,
            supports_audio=row.supports_audio,
            supports_text_rendering=row.supports_text_rendering,
            max_reference_images=row.max_reference_images,
            min_duration=row.min_duration,
            max_duration=row.max_duration,
            supported_aspect_ratios=list(row.supported_aspect_ratios),
            supported_resolutions=list(row.supported_resolutions),
            physics_prior=row.physics_prior,
            identity_prior=row.identity_prior,
            camera_prior=row.camera_prior,
            render_prior=row.render_prior,
            action_prior=row.action_prior,
            dialogue_prior=row.dialogue_prior,
            text_render_prior=row.text_render_prior,
            provider_metadata=dict(row.provider_metadata),
            provider_trust_level=ProviderTrustLevel(definition.provider_trust_level),
            criticality_allowed=[AssetCriticality(item) for item in definition.criticality_allowed],
            source=row.source,
        )

    def all(self, *, include_disabled: bool = False) -> list[ModelCapabilityProfile]:
        with self.database.session() as session:
            statement = (
                select(ModelDefinition, ModelCapabilityProfileRow)
                .join(
                    ModelCapabilityProfileRow,
                    ModelCapabilityProfileRow.model_definition_id == ModelDefinition.id,
                )
                .order_by(ModelDefinition.logical_name)
            )
            if not include_disabled:
                statement = statement.where(ModelDefinition.enabled.is_(True))
            return [self._to_profile(*pair) for pair in session.execute(statement).all()]

    def get(self, model_id: str, provider: str | None = None) -> ModelCapabilityProfile | None:
        normalized_model = model_id.strip()
        normalized_provider = provider.strip() if provider else None
        if not normalized_model:
            raise ValueError("model_id is required")
        with self.database.session() as session:
            statement = select(ModelDefinition, ModelCapabilityProfileRow).join(
                ModelCapabilityProfileRow,
                ModelCapabilityProfileRow.model_definition_id == ModelDefinition.id,
            )
            if normalized_provider:
                rows = session.execute(statement.where(ModelDefinition.provider == normalized_provider)).all()
                matches = [
                    pair
                    for pair in rows
                    if pair[0].provider_model_id == normalized_model
                    or normalized_model
                    in {str(alias) for alias in pair[0].metadata_json.get("legacy_execution_aliases", [])}
                ]
            else:
                matches = list(
                    session.execute(
                        statement.where(ModelDefinition.provider_model_id == normalized_model)
                    ).all()
                )
            if len(matches) > 1:
                raise LookupError(f"model ID is ambiguous without an exact provider identity: {model_id}")
            return self._to_profile(*matches[0]) if matches else None

    def get_by_definition_id(self, definition_id: str) -> ModelCapabilityProfile | None:
        with self.database.session() as session:
            pair = session.execute(
                select(ModelDefinition, ModelCapabilityProfileRow)
                .join(
                    ModelCapabilityProfileRow,
                    ModelCapabilityProfileRow.model_definition_id == ModelDefinition.id,
                )
                .where(ModelDefinition.id == definition_id)
            ).one_or_none()
            return self._to_profile(*pair) if pair else None

    def by_provider(self, provider: str) -> list[ModelCapabilityProfile]:
        return [profile for profile in self.all() if profile.provider == provider]

    def supports(self, provider: str, model_id: str, operation: str) -> bool:
        profile = self.get(model_id, provider)
        return bool(profile and operation in profile.supported_operations)

    def register_test_profile(
        self,
        provider: str,
        model_id: str,
        media_type: str,
    ) -> ModelCapabilityProfile:
        """Create explicit persisted fixture truth for an isolated test database."""

        if media_type not in {"image", "video"}:
            raise ValueError("test capability profile only supports image/video fixtures")
        existing = self.get(model_id, provider)
        if existing is not None:
            operation = f"{media_type}_generation"
            if operation in existing.supported_operations:
                return existing
            if existing.source != "TEST_FIXTURE":
                return existing
            with self.database.session() as session:
                definition = session.get(ModelDefinition, existing.model_definition_id)
                row = session.get(ModelCapabilityProfileRow, existing.model_definition_id)
                assert definition is not None and row is not None
                definition.capabilities = [*definition.capabilities, operation]
                row.supported_operations = [*row.supported_operations, operation]
                if media_type == "video":
                    row.supports_video_generation = True
                    row.supports_t2v = True
                    row.supports_camera_instruction = True
                else:
                    row.supports_image_generation = True
            expanded = self.get(model_id, provider)
            if expanded is None:  # pragma: no cover - defensive transaction guard.
                raise RuntimeError("expanded test capability profile did not persist")
            return expanded
        stable_id = str(
            uuid.uuid5(
                uuid.UUID("f8390154-0e27-54cf-83be-7a9cbd08631a"),
                f"{provider}:{media_type}:{model_id}",
            )
        )
        with self.database.session() as session:
            definition = ModelDefinition(
                id=stable_id,
                logical_name=f"test-fixture-{stable_id}",
                provider=provider,
                provider_model_id=model_id,
                modality=media_type,
                capabilities=[f"{media_type}_generation"],
                provider_trust_level=ProviderTrustLevel.PRODUCTION.value,
                criticality_allowed=[item.value for item in AssetCriticality],
                metadata_json={"test_fixture": True},
            )
            session.add(definition)
            session.flush()
            session.add(
                ModelCapabilityProfileRow(
                    model_definition_id=definition.id,
                    profile_version="test-fixture-v1",
                    supported_operations=[f"{media_type}_generation"],
                    supports_image_generation=media_type == "image",
                    supports_video_generation=media_type == "video",
                    supports_t2v=media_type == "video",
                    supports_camera_instruction=media_type == "video",
                    provider_metadata={"adapter": provider, "test_fixture": True},
                    source="TEST_FIXTURE",
                )
            )
        profile = self.get(model_id, provider)
        if profile is None:  # pragma: no cover - defensive transaction guard.
            raise RuntimeError("test capability profile did not persist")
        return profile

    def replace(self, profile: ModelCapabilityProfile) -> None:
        """Persist an explicit admin/test profile update without changing model identity."""

        with self.database.session() as session:
            definition = session.get(ModelDefinition, profile.model_definition_id)
            row = session.get(ModelCapabilityProfileRow, profile.model_definition_id)
            if definition is None or row is None:
                raise LookupError("model capability profile does not exist")
            if (
                definition.provider != profile.provider
                or definition.provider_model_id != profile.model_id
                or definition.logical_name != profile.logical_name
            ):
                raise ValueError("model identity belongs to ModelDefinition and cannot be replaced here")
            for field in (
                "supported_operations",
                *_BOOLEAN_CAPABILITIES,
                "max_reference_images",
                "min_duration",
                "max_duration",
                "supported_aspect_ratios",
                "supported_resolutions",
                "physics_prior",
                "identity_prior",
                "camera_prior",
                "render_prior",
                "action_prior",
                "dialogue_prior",
                "text_render_prior",
                "provider_metadata",
                "source",
            ):
                setattr(row, field, getattr(profile, field))
            row.supports_image_generation = "image_generation" in profile.supported_operations
            row.supports_video_generation = "video_generation" in profile.supported_operations
            row.profile_version = profile.version
            row.confidence_level = profile.confidence_level

    def merge_runtime_observation(
        self,
        model_definition_id: str,
        observation: Mapping[str, Any],
    ) -> ModelCapabilityProfile:
        """Validate and conservatively merge an adapter observation.

        Observations may narrow the reviewed profile for the current request;
        they cannot claim a capability, larger bound, or new enum value that
        the persisted profile does not already allow.
        """

        profile = self.get_by_definition_id(model_definition_id)
        if profile is None:
            raise LookupError("model capability profile does not exist")
        unknown = set(observation).difference(
            _BOOLEAN_CAPABILITIES
            | {
                "max_reference_images",
                "min_duration",
                "max_duration",
                "supported_aspect_ratios",
                "supported_resolutions",
            }
        )
        if unknown:
            raise CapabilityObservationConflict(
                f"unknown runtime capability fields: {', '.join(sorted(unknown))}"
            )
        updates: dict[str, Any] = {}
        for field, observed in observation.items():
            registered = getattr(profile, field)
            if field in _BOOLEAN_CAPABILITIES:
                if bool(observed) and not registered:
                    raise CapabilityObservationConflict(f"runtime observation expands {field}")
                updates[field] = bool(observed) and bool(registered)
            elif field == "max_reference_images":
                reference_limit = int(observed)
                if reference_limit < 0 or reference_limit > registered:
                    raise CapabilityObservationConflict("runtime max_reference_images exceeds the registry")
                updates[field] = reference_limit
            elif field == "min_duration":
                minimum_duration = float(observed)
                if profile.min_duration is not None and minimum_duration < profile.min_duration:
                    raise CapabilityObservationConflict("runtime min_duration expands the registry")
                updates[field] = minimum_duration
            elif field == "max_duration":
                maximum_duration = float(observed)
                if profile.max_duration is not None and maximum_duration > profile.max_duration:
                    raise CapabilityObservationConflict("runtime max_duration expands the registry")
                updates[field] = maximum_duration
            else:
                values = [str(item) for item in observed]
                if not set(values).issubset(set(registered)):
                    raise CapabilityObservationConflict(f"runtime observation expands {field}")
                updates[field] = values
        return profile.model_copy(update=updates)


__all__ = [
    "CapabilityObservationConflict",
    "ModelCapabilityRegistry",
]
