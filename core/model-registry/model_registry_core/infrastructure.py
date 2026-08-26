from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    ModelCapabilityProfile as ModelCapabilityProfileRow,
)
from production_domain.models import (
    ModelDefinition,
    ModelPricingProfile,
    ModelRoleBinding,
)
from provider_sdk import AssetCriticality, ProviderTrustLevel, provider_can_handle
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .schemas import (
    ROLE_CAPABILITY,
    ModelBindingKind,
    ModelInfrastructureConfig,
    ModelRole,
)

_MODEL_NAMESPACE = uuid.UUID("ec221fda-f111-5e35-a334-1aa42596708c")
_BINDING_NAMESPACE = uuid.UUID("d5cc2010-2907-5d19-87fa-71e1ed3d7472")


@dataclass(frozen=True)
class ModelDefaultSyncResult:
    models_created: int
    bindings_created: int
    model_names_created: tuple[str, ...] = ()
    profiles_created: int = 0


@dataclass(frozen=True)
class ResolvedModel:
    definition_id: str
    logical_name: str
    provider: str
    provider_model_id: str
    modality: str
    provider_trust_level: ProviderTrustLevel
    role: ModelRole
    plan_tier: str
    binding_kind: ModelBindingKind
    priority: int


@dataclass(frozen=True)
class RuntimeModelConfiguration:
    logical_name: str
    provider_model_id: str
    enabled: bool
    live_enabled: bool


@dataclass(frozen=True)
class RuntimeModelState:
    definition_id: str
    logical_name: str
    provider: str
    provider_model_id: str
    modality: str
    enabled: bool
    live_enabled: bool
    lifecycle_status: str
    router_enabled: bool
    supported_operations: tuple[str, ...]
    capability_profile_version: str


def load_model_infrastructure_config(path: Path) -> ModelInfrastructureConfig:
    if not path.is_file():
        raise FileNotFoundError(f"model infrastructure configuration not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ModelInfrastructureConfig.model_validate(payload)


def _stable_model_id(logical_name: str) -> str:
    return str(uuid.uuid5(_MODEL_NAMESPACE, logical_name))


def _stable_binding_id(role: str, plan_tier: str, logical_name: str) -> str:
    return str(uuid.uuid5(_BINDING_NAMESPACE, f"{role}:{plan_tier}:{logical_name}"))


def _insert_if_missing(
    session: Session,
    model: type[ModelDefinition] | type[ModelRoleBinding] | type[ModelCapabilityProfileRow],
    values: dict[str, Any],
) -> int:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        postgres_statement = postgresql_insert(model).values(**values).on_conflict_do_nothing()
        return affected_rows(session.execute(postgres_statement))
    if dialect == "sqlite":
        sqlite_statement = sqlite_insert(model).values(**values).on_conflict_do_nothing()
        return affected_rows(session.execute(sqlite_statement))
    session.add(model(**values))
    session.flush()
    return 1


class ModelInfrastructureService:
    """Persist defaults once, then resolve configurable business model roles.

    Configuration is a safe bootstrap source, while existing database rows are
    deliberately not overwritten during startup.  This keeps later admin or
    plan-specific changes durable.
    """

    def __init__(self, database: Database, config_path: Path):
        self.database = database
        self.config_path = config_path
        self.config = load_model_infrastructure_config(config_path)

    def ensure_defaults(self) -> ModelDefaultSyncResult:
        models_created = 0
        bindings_created = 0
        profiles_created = 0
        model_names_created: list[str] = []
        with self.database.session() as session:
            by_name = {item.logical_name: item for item in session.scalars(select(ModelDefinition)).all()}
            for model_config in self.config.models:
                if model_config.logical_name in by_name:
                    continue
                metadata = dict(model_config.metadata_json)
                metadata.setdefault("configuration_version", self.config.version)
                inserted = _insert_if_missing(
                    session,
                    ModelDefinition,
                    {
                        "id": _stable_model_id(model_config.logical_name),
                        "logical_name": model_config.logical_name,
                        "provider": model_config.provider,
                        "provider_model_id": model_config.provider_model_id,
                        "modality": model_config.modality,
                        "capabilities": list(model_config.capabilities),
                        "quality_tier": model_config.quality_tier,
                        "cost_class": model_config.cost_class,
                        "provider_trust_level": model_config.provider_trust_level.value,
                        "criticality_allowed": [item.value for item in model_config.criticality_allowed],
                        "enabled": model_config.enabled,
                        "live_enabled": model_config.live_enabled,
                        "context_window": model_config.context_window,
                        "max_duration": model_config.max_duration,
                        "supported_aspect_ratios": list(model_config.supported_aspect_ratios),
                        "metadata_json": metadata,
                    },
                )
                models_created += inserted
                if inserted:
                    model_names_created.append(model_config.logical_name)
            session.flush()
            by_name = {item.logical_name: item for item in session.scalars(select(ModelDefinition)).all()}
            missing_names = {
                item.logical_name for item in self.config.models if item.logical_name not in by_name
            }
            if missing_names:
                missing = ", ".join(sorted(missing_names))
                raise RuntimeError(
                    "model defaults could not be persisted because of a conflicting provider/model "
                    f"identity: {missing}"
                )

            existing_profiles = set(
                session.scalars(select(ModelCapabilityProfileRow.model_definition_id)).all()
            )
            for model_config in self.config.models:
                definition = by_name[model_config.logical_name]
                if definition.id in existing_profiles:
                    continue
                profile = model_config.resolved_capability_profile
                profiles_created += _insert_if_missing(
                    session,
                    ModelCapabilityProfileRow,
                    {
                        "model_definition_id": definition.id,
                        **profile.model_dump(mode="json"),
                        "supports_image_generation": ("image_generation" in profile.supported_operations),
                        "supports_video_generation": ("video_generation" in profile.supported_operations),
                    },
                )
                existing_profiles.add(definition.id)

            existing_keys = {
                (item.role, item.plan_tier, item.model_definition_id)
                for item in session.scalars(select(ModelRoleBinding)).all()
            }
            for binding_config in self.config.role_bindings:
                definition = by_name[binding_config.model_logical_name]
                key = (binding_config.role.value, binding_config.plan_tier, definition.id)
                if key in existing_keys:
                    continue
                metadata = dict(binding_config.metadata_json)
                metadata.setdefault("configuration_version", self.config.version)
                bindings_created += _insert_if_missing(
                    session,
                    ModelRoleBinding,
                    {
                        "id": _stable_binding_id(
                            binding_config.role.value,
                            binding_config.plan_tier,
                            binding_config.model_logical_name,
                        ),
                        "role": binding_config.role.value,
                        "plan_tier": binding_config.plan_tier,
                        "model_definition_id": definition.id,
                        "binding_kind": binding_config.binding_kind.value,
                        "priority": binding_config.priority,
                        "enabled": binding_config.enabled,
                        "metadata_json": metadata,
                    },
                )
                existing_keys.add(key)
        return ModelDefaultSyncResult(
            models_created,
            bindings_created,
            tuple(model_names_created),
            profiles_created,
        )

    def configure_runtime_model(
        self,
        logical_name: str,
        provider_model_id: str,
        *,
        enabled: bool,
        live_enabled: bool = False,
        provider_trust_level: ProviderTrustLevel | str | None = None,
        criticality_allowed: list[AssetCriticality | str] | None = None,
    ) -> RuntimeModelConfiguration:
        """Explicitly configure the execution ID and enablement of one model.

        Credentials are intentionally out of scope and `live_enabled` never
        follows `enabled` implicitly.  The caller must separately pass the
        explicit live value after applying the global paid-call gate.
        """

        normalized_name = logical_name.strip()
        normalized_model_id = provider_model_id.strip()
        if not normalized_name:
            raise ValueError("logical_name is required")
        if not normalized_model_id:
            raise ValueError("provider_model_id is required")
        if len(normalized_model_id) > 255:
            raise ValueError("provider_model_id exceeds 255 characters")
        if enabled and normalized_model_id.startswith("CONFIGURE_"):
            raise ValueError("a placeholder provider model ID cannot be enabled")
        if live_enabled and not enabled:
            raise ValueError("live_enabled requires enabled=true")

        with self.database.session() as session:
            definition = session.scalar(
                select(ModelDefinition).where(ModelDefinition.logical_name == normalized_name)
            )
            if definition is None:
                raise LookupError(f"model definition not found: {normalized_name}")
            conflict = session.scalar(
                select(ModelDefinition).where(
                    ModelDefinition.id != definition.id,
                    ModelDefinition.provider == definition.provider,
                    ModelDefinition.provider_model_id == normalized_model_id,
                    ModelDefinition.modality == definition.modality,
                )
            )
            if conflict is not None:
                raise ValueError("provider model ID is already registered for this modality")
            definition.provider_model_id = normalized_model_id
            definition.enabled = enabled
            definition.live_enabled = live_enabled
            if provider_trust_level is not None:
                definition.provider_trust_level = ProviderTrustLevel(provider_trust_level).value
            if criticality_allowed is not None:
                normalized_criticality = [AssetCriticality(item).value for item in criticality_allowed]
                if not normalized_criticality:
                    raise ValueError("criticality_allowed cannot be empty")
                definition.criticality_allowed = normalized_criticality
            session.flush()
            return RuntimeModelConfiguration(
                logical_name=definition.logical_name,
                provider_model_id=definition.provider_model_id,
                enabled=definition.enabled,
                live_enabled=definition.live_enabled,
            )

    def reconcile_pricing_status(self) -> int:
        """Make `pricing_status` agree with whether a published price exists.

        The column is a report, not a switch — the engine gates on the pricing
        profile itself. A report that can be set by hand is a report that drifts,
        and this one drifts in the direction that costs money: 0044's migration
        marked the rows that existed when it ran, so a database migrated before
        its models were seeded — every fresh deployment — would show UNVERIFIED
        for the one model whose rates were actually read off the vendor page,
        while a model whose profiles were later withdrawn would go on claiming
        VERIFIED. Derive it at boot instead, from the same table the quote uses.

        Returns the number of rows whose status was wrong.
        """

        with self.database.session() as session:
            # Provider and model id together: two providers can serve the same
            # published model name at different prices, and one of them having a
            # profile says nothing about the other.
            priced = {
                (row.provider, row.provider_model_id)
                for row in session.execute(
                    select(ModelPricingProfile.provider, ModelPricingProfile.provider_model_id)
                )
            }
            corrected = 0
            for definition in session.scalars(select(ModelDefinition)).all():
                key = (definition.provider, definition.provider_model_id)
                expected = "VERIFIED" if key in priced else "UNVERIFIED"
                if definition.pricing_status != expected:
                    definition.pricing_status = expected
                    corrected += 1
        return corrected

    def declared_model_id_divergence(self, logical_name: str, declared: str) -> str | None:
        """Report, without changing anything, that the stored ID is not the declared one.

        Deliberately read-only. An operator who edits `provider_model_id` directly
        is making a deployment decision, and a restart must not undo it — that is
        a pinned invariant, not an accident. So the environment cannot win here.

        What it may do is stop being silent. Seedance 2.5 sat with `.env` naming
        `doubao-seedance-2-5-260628` and the row still holding the seeded
        placeholder `seedance-2.5`; nothing compared them, and the first anyone
        heard of it was Ark answering "model or endpoint does not exist" after a
        reservation had already been taken. Returning the stored ID lets the
        caller say so at boot.
        """

        normalized_name = logical_name.strip()
        normalized_declared = declared.strip()
        if not normalized_name or not normalized_declared:
            return None
        with self.database.session() as session:
            stored = session.scalar(
                select(ModelDefinition.provider_model_id).where(
                    ModelDefinition.logical_name == normalized_name
                )
            )
        if stored is None or stored == normalized_declared:
            return None
        return str(stored)

    def runtime_model(self, logical_name: str) -> RuntimeModelState:
        normalized_name = logical_name.strip()
        if not normalized_name:
            raise ValueError("logical_name is required")
        with self.database.session() as session:
            pair = session.execute(
                select(ModelDefinition, ModelCapabilityProfileRow)
                .join(
                    ModelCapabilityProfileRow,
                    ModelCapabilityProfileRow.model_definition_id == ModelDefinition.id,
                )
                .where(ModelDefinition.logical_name == normalized_name)
            ).one_or_none()
            if pair is None:
                raise LookupError(f"model definition not found: {normalized_name}")
            return self._runtime_state(*pair)

    def all_runtime_models(self) -> list[RuntimeModelState]:
        """Every registered model, whatever its provider or enablement."""

        with self.database.session() as session:
            rows = session.execute(
                select(ModelDefinition, ModelCapabilityProfileRow)
                .join(
                    ModelCapabilityProfileRow,
                    ModelCapabilityProfileRow.model_definition_id == ModelDefinition.id,
                )
                .order_by(ModelDefinition.provider, ModelDefinition.logical_name)
            ).all()
            return [self._runtime_state(*row) for row in rows]

    def set_enablement(
        self,
        logical_name: str,
        *,
        enabled: bool,
        live_enabled: bool,
    ) -> RuntimeModelConfiguration:
        """Change only enablement, leaving the execution ID untouched.

        `configure_runtime_model` takes a `provider_model_id` because it is for
        *configuring* a model. Reconciling one that is already configured must
        not restate its execution ID — doing that is how a reconciliation pass
        silently overwrites the model an administrator chose.
        """

        normalized = logical_name.strip()
        if not normalized:
            raise ValueError("logical_name is required")
        if live_enabled and not enabled:
            raise ValueError("live_enabled requires enabled=true")
        with self.database.session() as session:
            definition = session.scalar(
                select(ModelDefinition).where(ModelDefinition.logical_name == normalized)
            )
            if definition is None:
                raise LookupError(f"model definition not found: {normalized}")
            if enabled and definition.provider_model_id.startswith("CONFIGURE_"):
                raise ValueError("a placeholder provider model ID cannot be enabled")
            definition.enabled = enabled
            definition.live_enabled = live_enabled
            session.flush()
            return RuntimeModelConfiguration(
                logical_name=definition.logical_name,
                provider_model_id=definition.provider_model_id,
                enabled=definition.enabled,
                live_enabled=definition.live_enabled,
            )

    def runtime_models(self, provider: str) -> list[RuntimeModelState]:
        normalized_provider = provider.strip()
        if not normalized_provider:
            raise ValueError("provider is required")
        with self.database.session() as session:
            rows = session.execute(
                select(ModelDefinition, ModelCapabilityProfileRow)
                .join(
                    ModelCapabilityProfileRow,
                    ModelCapabilityProfileRow.model_definition_id == ModelDefinition.id,
                )
                .where(ModelDefinition.provider == normalized_provider)
                .order_by(ModelDefinition.logical_name)
            ).all()
            return [self._runtime_state(*row) for row in rows]

    def runtime_model_for_target(
        self,
        provider: str,
        provider_model_id: str,
        modality: str,
    ) -> RuntimeModelState | None:
        """Return the persisted model definition for an execution target.

        The provider, provider-owned model ID, and modality together are the
        database uniqueness boundary. The one legacy Flow video alias is
        resolved to its server-owned logical definition so it cannot bypass the
        persisted enabled/live switches. Callers must derive target values from
        server-owned execution state rather than request metadata.
        """

        normalized_provider = provider.strip()
        normalized_model_id = provider_model_id.strip()
        normalized_modality = modality.strip()
        if not normalized_provider:
            raise ValueError("provider is required")
        if not normalized_model_id:
            raise ValueError("provider_model_id is required")
        if not normalized_modality:
            raise ValueError("modality is required")
        with self.database.session() as session:
            return self.runtime_model_for_target_in_session(
                session,
                normalized_provider,
                normalized_model_id,
                normalized_modality,
            )

    def runtime_model_for_target_in_session(
        self,
        session: Session,
        provider: str,
        provider_model_id: str,
        modality: str,
        *,
        for_update: bool = False,
    ) -> RuntimeModelState | None:
        """Resolve a target inside the caller's transaction, optionally locking its switch row."""

        normalized_provider = provider.strip()
        normalized_model_id = provider_model_id.strip()
        normalized_modality = modality.strip()
        if not normalized_provider:
            raise ValueError("provider is required")
        if not normalized_model_id:
            raise ValueError("provider_model_id is required")
        if not normalized_modality:
            raise ValueError("modality is required")

        statement = (
            select(ModelDefinition, ModelCapabilityProfileRow)
            .join(
                ModelCapabilityProfileRow,
                ModelCapabilityProfileRow.model_definition_id == ModelDefinition.id,
            )
            .where(
                ModelDefinition.provider == normalized_provider,
                ModelDefinition.provider_model_id == normalized_model_id,
                ModelDefinition.modality == normalized_modality,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        pair = session.execute(statement).one_or_none()
        if pair is None:
            legacy_logical_name = {
                ("google_flow", "veo", "video"): "flow-veo-3.1-internal",
            }.get((normalized_provider, normalized_model_id, normalized_modality))
            if legacy_logical_name is not None:
                alias_statement = (
                    select(ModelDefinition, ModelCapabilityProfileRow)
                    .join(
                        ModelCapabilityProfileRow,
                        ModelCapabilityProfileRow.model_definition_id == ModelDefinition.id,
                    )
                    .where(
                        ModelDefinition.logical_name == legacy_logical_name,
                        ModelDefinition.provider == normalized_provider,
                        ModelDefinition.modality == normalized_modality,
                    )
                )
                if for_update:
                    alias_statement = alias_statement.with_for_update()
                pair = session.execute(alias_statement).one_or_none()
        return self._runtime_state(*pair) if pair is not None else None

    @staticmethod
    def _runtime_state(
        definition: ModelDefinition,
        profile: ModelCapabilityProfileRow | None = None,
    ) -> RuntimeModelState:
        if profile is None:
            raise RuntimeError("model definition is missing its authoritative capability profile")
        return RuntimeModelState(
            definition_id=definition.id,
            logical_name=definition.logical_name,
            provider=definition.provider,
            provider_model_id=definition.provider_model_id,
            modality=definition.modality,
            enabled=definition.enabled,
            live_enabled=definition.live_enabled,
            lifecycle_status=definition.lifecycle_status,
            router_enabled=definition.router_enabled,
            supported_operations=tuple(profile.supported_operations),
            capability_profile_version=profile.profile_version,
        )

    @staticmethod
    def is_compatible(
        definition: ModelDefinition,
        profile: ModelCapabilityProfileRow,
        role: ModelRole | str,
        criticality: AssetCriticality | str,
        *,
        require_live: bool = False,
    ) -> bool:
        requested_role = ModelRole(role)
        requested_criticality = AssetCriticality(criticality)
        if not definition.enabled or (require_live and not definition.live_enabled):
            return False
        if ROLE_CAPABILITY[requested_role] not in profile.supported_operations:
            return False
        if requested_criticality.value not in definition.criticality_allowed:
            return False
        try:
            return provider_can_handle(definition.provider_trust_level, requested_criticality)
        except ValueError:
            return False

    def candidates_for_role(
        self,
        role: ModelRole | str,
        *,
        plan_tier: str = "ALL",
        asset_criticality: AssetCriticality | str = AssetCriticality.STANDARD,
        require_live: bool = False,
    ) -> list[ResolvedModel]:
        requested_role = ModelRole(role)
        requested_plan = plan_tier.strip().upper() or "ALL"
        # ``ALL`` is the shared paid/unscoped catalogue, not a FREE fallback.
        # FREE is a billing boundary: if it has no explicit compatible binding,
        # resolution must stop locally before a paid provider can be selected.
        # Paid tiers retain the existing ability to inherit generic bindings.
        eligible_plan_tiers = {requested_plan} if requested_plan == "FREE" else {"ALL", requested_plan}
        with self.database.session() as session:
            rows = session.execute(
                select(ModelRoleBinding, ModelDefinition, ModelCapabilityProfileRow)
                .join(
                    ModelDefinition,
                    ModelDefinition.id == ModelRoleBinding.model_definition_id,
                )
                .join(
                    ModelCapabilityProfileRow,
                    ModelCapabilityProfileRow.model_definition_id == ModelDefinition.id,
                )
                .where(
                    ModelRoleBinding.role == requested_role.value,
                    ModelRoleBinding.enabled.is_(True),
                    ModelRoleBinding.plan_tier.in_(eligible_plan_tiers),
                )
            ).all()

            # An explicit tier scope is an override boundary.  If it exists we
            # do not silently spend through a generic binding when the scoped
            # model is disabled or incompatible; the caller gets no route and
            # can apply its product-level entitlement/fallback policy.
            tier_rows = [
                row for row in rows if row[0].plan_tier == requested_plan and requested_plan != "ALL"
            ]
            if tier_rows:
                rows = tier_rows

            compatible = [
                (binding, definition, profile)
                for binding, definition, profile in rows
                if self.is_compatible(
                    definition,
                    profile,
                    requested_role,
                    asset_criticality,
                    require_live=require_live,
                )
            ]
            compatible.sort(
                key=lambda pair: (
                    0 if pair[0].plan_tier == requested_plan and requested_plan != "ALL" else 1,
                    0 if pair[0].binding_kind == ModelBindingKind.PRIMARY.value else 1,
                    pair[0].priority,
                    pair[1].logical_name,
                )
            )
            return [
                ResolvedModel(
                    definition_id=definition.id,
                    logical_name=definition.logical_name,
                    provider=definition.provider,
                    provider_model_id=definition.provider_model_id,
                    modality=definition.modality,
                    provider_trust_level=ProviderTrustLevel(definition.provider_trust_level),
                    role=requested_role,
                    plan_tier=binding.plan_tier,
                    binding_kind=ModelBindingKind(binding.binding_kind),
                    priority=binding.priority,
                )
                for binding, definition, _profile in compatible
            ]

    def resolve_role(
        self,
        role: ModelRole | str,
        *,
        plan_tier: str = "ALL",
        asset_criticality: AssetCriticality | str = AssetCriticality.STANDARD,
        require_live: bool = False,
    ) -> ResolvedModel:
        candidates = self.candidates_for_role(
            role,
            plan_tier=plan_tier,
            asset_criticality=asset_criticality,
            require_live=require_live,
        )
        if not candidates:
            raise LookupError(
                f"no compatible model binding for role={ModelRole(role).value}, "
                f"plan={plan_tier.strip().upper() or 'ALL'}, "
                f"criticality={AssetCriticality(asset_criticality).value}"
            )
        return candidates[0]
