from pathlib import Path

import pytest
from model_registry_core import (
    ModelInfrastructureService,
    ModelRole,
    load_model_infrastructure_config,
)
from platform_contracts import GenerationRequest
from production_domain.models import ModelDefinition, ModelRoleBinding
from provider_sdk import (
    AssetCriticality,
    ProviderTrustLevel,
    ProviderTrustViolation,
    assert_provider_can_handle,
    provider_can_handle,
    required_trust_for_asset,
)
from sqlalchemy import func, select

CONFIG_PATH = Path(__file__).parents[1] / "config" / "model-registry" / "defaults.json"


def test_trust_policy_is_explicit_and_blocks_edge_for_important_assets() -> None:
    assert required_trust_for_asset(AssetCriticality.CANONICAL) == ProviderTrustLevel.PRODUCTION
    assert required_trust_for_asset(AssetCriticality.HERO) == ProviderTrustLevel.PRODUCTION
    assert provider_can_handle(ProviderTrustLevel.PRODUCTION, AssetCriticality.CANONICAL)
    assert provider_can_handle(ProviderTrustLevel.EDGE, AssetCriticality.EDGE)
    assert provider_can_handle(ProviderTrustLevel.TEST_ONLY, AssetCriticality.TEMPORARY)
    assert not provider_can_handle(ProviderTrustLevel.EDGE, AssetCriticality.CANONICAL)
    assert not provider_can_handle(ProviderTrustLevel.EDGE, AssetCriticality.HERO)
    assert not provider_can_handle(ProviderTrustLevel.EDGE, AssetCriticality.IMPORTANT)
    with pytest.raises(ProviderTrustViolation, match="minimum trust is PRODUCTION"):
        assert_provider_can_handle(ProviderTrustLevel.EDGE, AssetCriticality.HERO)


def test_every_generation_request_serializes_an_explicit_asset_criticality() -> None:
    request = GenerationRequest(
        project_id="project",
        type="image",
        prompt="temporary composition test",
        idempotency_key="criticality-default",
    )
    assert request.model_dump(mode="json")["asset_criticality"] == "STANDARD"


def test_versioned_defaults_include_frozen_provider_models_and_no_secrets() -> None:
    config = load_model_infrastructure_config(CONFIG_PATH)
    provider_ids = {item.provider_model_id for item in config.models}
    # 22 + the three OpenRouter Veo 3.1 variants added 2026-08-25.
    assert len(config.models) == 25
    assert {
        "openai/gpt-5.6-sol",
        "anthropic/claude-opus-5",
        "deepseek-v4-flash",
        "qwen3.8-max",
        "glm-5.2",
        # Corrected 2026-08-26: `seedream-5-0` is the BytePlus stem and is not a
        # model ID Volcengine Ark publishes, so it named nothing on the provider
        # this platform actually calls.
        "doubao-seedream-5-0-260128",
        "openai/gpt-image-2",
        "google/gemini-embedding-2",
        "x-ai/grok-imagine-video",
        "anthropic/claude-sonnet-5",
        "kwaivgi/kling-v3.0-std",
        "kwaivgi/kling-v3.0-pro",
        "voyageai/voyage-multimodal-3.5",
        "flow-veo-3.1",
        "NARWHAL",
        "doubao-seedance-2-5-260628",
        "veo-3.1-quality",
        "grok-video",
        "wan-2.7",
    } <= provider_ids
    assert all(not item.live_enabled for item in config.models)
    source = CONFIG_PATH.read_text(encoding="utf-8")
    assert "sk-" not in source
    assert "ark-" not in source
    assert "runapi_" not in source
    free_refinement_roles = {
        binding.role
        for binding in config.role_bindings
        if binding.plan_tier == "FREE" and binding.model_logical_name == "doubao-free-reasoner"
    }
    assert {
        ModelRole.PROMPT_REFINER,
        ModelRole.PROMPT_REFINER_LOW_COST,
        ModelRole.PROMPT_REFINER_FALLBACK,
    } <= free_refinement_roles


def test_defaults_are_persisted_idempotently_without_overwriting_admin_state(container) -> None:
    service = container.model_infrastructure
    with container.database.session() as session:
        model_count = session.scalar(select(func.count()).select_from(ModelDefinition))
        binding_count = session.scalar(select(func.count()).select_from(ModelRoleBinding))
        gpt = session.scalar(
            select(ModelDefinition).where(ModelDefinition.logical_name == "gpt-5.6-sol-openrouter")
        )
        assert gpt is not None
        gpt.enabled = False

    result = service.ensure_defaults()
    assert result.models_created == 0
    assert result.bindings_created == 0
    with container.database.session() as session:
        assert session.scalar(select(func.count()).select_from(ModelDefinition)) == model_count
        assert session.scalar(select(func.count()).select_from(ModelRoleBinding)) == binding_count
        gpt = session.scalar(
            select(ModelDefinition).where(ModelDefinition.logical_name == "gpt-5.6-sol-openrouter")
        )
        assert gpt is not None
        assert gpt.enabled is False


def test_flow_legacy_alias_and_image_target_resolve_persisted_switches(container) -> None:
    service = container.model_infrastructure
    legacy_video = service.runtime_model_for_target("google_flow", "veo", "video")
    canonical_video = service.runtime_model_for_target(
        "google_flow",
        "flow-veo-3.1",
        "video",
    )
    image = service.runtime_model_for_target("google_flow", "NARWHAL", "image")

    assert legacy_video is not None
    assert canonical_video is not None
    assert legacy_video.definition_id == canonical_video.definition_id
    assert legacy_video.logical_name == "flow-veo-3.1-internal"
    assert image is not None
    assert image.logical_name == "flow-narwhal-image-internal"
    assert image.live_enabled is False


def test_free_doubao_override_fails_closed_instead_of_using_openrouter(container) -> None:
    service = container.model_infrastructure
    with pytest.raises(LookupError, match="role=DIRECTOR, plan=FREE"):
        service.resolve_role(
            ModelRole.DIRECTOR,
            plan_tier="FREE",
            asset_criticality=AssetCriticality.STANDARD,
        )


def test_paid_and_unscoped_plans_still_inherit_all_bindings(container) -> None:
    service = container.model_infrastructure

    paid = service.resolve_role(ModelRole.DIRECTOR, plan_tier="PRO")
    unscoped = service.resolve_role(ModelRole.DIRECTOR, plan_tier="ALL")

    assert paid.provider == "openrouter"
    assert paid.plan_tier == "ALL"
    assert unscoped.provider == "openrouter"
    assert unscoped.plan_tier == "ALL"


def test_runtime_model_configuration_explicitly_enables_doubao_without_enabling_live(container) -> None:
    service = container.model_infrastructure
    configured = service.configure_runtime_model(
        "doubao-free-reasoner",
        "ark-deployment-doubao-free",
        enabled=True,
    )
    assert configured.provider_model_id == "ark-deployment-doubao-free"
    assert configured.enabled is True
    assert configured.live_enabled is False

    route = service.resolve_role(
        ModelRole.DIRECTOR,
        plan_tier="FREE",
        asset_criticality=AssetCriticality.STANDARD,
    )
    assert route.provider == "seedance"
    assert route.provider_model_id == "ark-deployment-doubao-free"
    with pytest.raises(LookupError, match="role=DIRECTOR, plan=FREE"):
        service.resolve_role(
            ModelRole.DIRECTOR,
            plan_tier="FREE",
            asset_criticality=AssetCriticality.STANDARD,
            require_live=True,
        )

    live = service.configure_runtime_model(
        "doubao-free-reasoner",
        "ark-deployment-doubao-free",
        enabled=True,
        live_enabled=True,
    )
    assert live.live_enabled is True
    assert (
        service.resolve_role(
            ModelRole.DIRECTOR,
            plan_tier="FREE",
            require_live=True,
        ).provider_model_id
        == "ark-deployment-doubao-free"
    )


def test_runtime_model_configuration_rejects_empty_or_implicit_placeholder(container) -> None:
    service = container.model_infrastructure
    with pytest.raises(ValueError, match="provider_model_id is required"):
        service.configure_runtime_model(
            "doubao-free-reasoner",
            " ",
            enabled=True,
        )
    with pytest.raises(ValueError, match="placeholder provider model ID"):
        service.configure_runtime_model(
            "doubao-free-reasoner",
            "CONFIGURE_DOUBAO_MODEL_ID",
            enabled=True,
        )
    with pytest.raises(ValueError, match="live_enabled requires enabled=true"):
        service.configure_runtime_model(
            "doubao-free-reasoner",
            "ark-deployment-doubao-free",
            enabled=False,
            live_enabled=True,
        )


def test_unconfigured_free_seedance_binding_fails_closed(container) -> None:
    with pytest.raises(LookupError, match="no compatible model binding"):
        container.model_infrastructure.resolve_role(
            ModelRole.VIDEO_SEEDANCE,
            plan_tier="FREE",
            asset_criticality=AssetCriticality.HERO,
        )


def test_edge_role_binding_can_only_resolve_low_criticality(container) -> None:
    service: ModelInfrastructureService = container.model_infrastructure
    with container.database.session() as session:
        edge = session.scalar(
            select(ModelDefinition).where(ModelDefinition.logical_name == "runapi-prompt-refiner-edge")
        )
        assert edge is not None
        edge.enabled = True

    route = service.resolve_role(
        ModelRole.PROMPT_REFINER_LOW_COST,
        asset_criticality=AssetCriticality.EDGE,
    )
    assert route.provider_trust_level == ProviderTrustLevel.EDGE
    for criticality in (AssetCriticality.CANONICAL, AssetCriticality.HERO):
        with pytest.raises(LookupError, match=f"criticality={criticality.value}"):
            service.resolve_role(
                ModelRole.PROMPT_REFINER_LOW_COST,
                asset_criticality=criticality,
            )


def test_live_reconciliation_reports_before_it_writes(container) -> None:  # type: ignore[no-untyped-def]
    """Adding a credential and restarting used to open nothing.

    Startup seeds the registry once and deliberately never replays defaults over
    an administrator's changes. The cost was that a model disabled for want of a
    credential stayed disabled after the credential arrived, with no operator
    path to open it. This is that path, and it reports before it writes.
    """

    from fastapi.testclient import TestClient
    from video_platform_api.main import create_app

    container.settings.platform_api_key = "reconcile-key"
    headers = {"Authorization": "Bearer reconcile-key"}
    with TestClient(create_app(container)) as client:
        report = client.post("/internal/models/reconcile-live", headers=headers)
        assert report.status_code == 200, report.text
        body = report.json()

    # The gate is shut in this environment, so nothing may be opened.
    assert body["applied"] is False
    assert body["live_gate_ready"] is False
    assert body["models"], "every registered model must be accounted for"
    assert all(row["live_enabled"] is False for row in body["models"])
    # Every model is reported, each with a reason it is not live.
    assert {row["logical_name"] for row in body["models"]} == {
        state.logical_name for state in container.model_infrastructure.all_runtime_models()
    }
    assert all(row["blocked_by"] for row in body["models"] if not row["live_enabled"])


def test_reconciliation_never_restates_an_administrator_model_id(container) -> None:  # type: ignore[no-untyped-def]
    """Only enablement moves. The execution ID is an operator's choice."""

    infrastructure = container.model_infrastructure
    before = infrastructure.runtime_model("wan-2.7-official")
    infrastructure.set_enablement("wan-2.7-official", enabled=True, live_enabled=False)
    after = infrastructure.runtime_model("wan-2.7-official")
    assert after.provider_model_id == before.provider_model_id
    assert after.enabled is True and after.live_enabled is False

    with pytest.raises(ValueError, match="live_enabled requires enabled"):
        infrastructure.set_enablement("wan-2.7-official", enabled=False, live_enabled=True)
