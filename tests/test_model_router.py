import pytest
from model_registry_core import (
    ShotRequirements,
)
from production_domain.models import (
    ModelCapabilityProfile as ModelCapabilityProfileRow,
)
from production_domain.models import (
    ModelDefinition,
)
from provider_sdk import AssetCriticality, ProviderTrustLevel


def test_registry_loads_persisted_manual_profiles(container):
    registry = container.model_registry
    grok = registry.get("grok-video", "grok")
    assert grok is not None
    assert grok.failure_priors["end_frame_direct_gaze"] == 0.8
    assert grok.adapter == "grok"
    wan = registry.get("wan-2.7", "wan")
    assert wan is not None
    assert wan.source == "MANUAL_PRIOR"
    assert wan.supports_t2v is True
    assert wan.supports_i2v is False


def test_router_penalizes_grok_for_rear_view_ending(container):
    router = container.video_router
    neutral = router.rank(
        ShotRequirements(
            profile="dialogue",
            requires_dialogue=True,
            requires_chinese_dialogue=True,
            cost_priority=0.7,
            latency_priority=0.7,
        )
    )
    rear = router.rank(
        ShotRequirements(
            profile="dialogue",
            requires_dialogue=True,
            requires_chinese_dialogue=True,
            requires_rear_view_ending=True,
            forbid_camera_gaze=True,
            cost_priority=0.7,
            latency_priority=0.7,
        )
    )
    neutral_grok = next(candidate for candidate in neutral.candidates if candidate.provider == "grok")
    rear_grok = next(candidate for candidate in rear.candidates if candidate.provider == "grok")
    assert rear_grok.score < neutral_grok.score
    assert any("direct-gaze" in item for item in rear_grok.penalties)


def test_router_uses_dynamic_commercial_and_action_weights(container):
    router = container.video_router
    commercial = router.rank(
        ShotRequirements(profile="commercial_hero", product_fidelity_priority=1, cost_priority=0)
    )
    action = router.rank(
        ShotRequirements(
            profile="action",
            requires_complex_action=True,
            requires_physical_plausibility=True,
            cost_priority=0,
        )
    )
    assert commercial.candidates[0].model in {"veo-3.1-quality", "flow-veo-3.1"}
    assert action.candidates[0].model in {
        "kwaivgi/kling-v3.0-std",
        "kwaivgi/kling-v3.0-pro",
    }


def test_router_rejects_models_without_required_duration_or_features(container):
    with pytest.raises(LookupError, match="no active model"):
        container.video_router.rank(
            ShotRequirements(duration=20, requires_end_frame=True, resolution="1080p")
        )


@pytest.mark.parametrize("criticality", [AssetCriticality.CANONICAL, AssetCriticality.HERO])
def test_router_never_routes_canonical_or_hero_assets_to_edge_provider(
    container,
    criticality,
):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        definition = ModelDefinition(
            logical_name="edge-video-test",
            provider="runapi",
            provider_model_id="edge-video",
            modality="video",
            capabilities=["video_generation"],
            provider_trust_level=ProviderTrustLevel.EDGE.value,
            criticality_allowed=[AssetCriticality.EDGE.value, AssetCriticality.TEMPORARY.value],
        )
        session.add(definition)
        session.flush()
        session.add(
            ModelCapabilityProfileRow(
                model_definition_id=definition.id,
                supported_operations=["video_generation"],
                supports_t2v=True,
                max_duration=60,
                supported_resolutions=["720p", "1080p"],
                render_prior=1.0,
                provider_metadata={
                    "adapter": "wan",
                    "cost": {"normalized": 0.0},
                    "latency": {"normalized": 0.0},
                },
            )
        )
    decision = container.video_router.rank(
        ShotRequirements(
            profile="commercial_hero",
            asset_criticality=criticality,
            cost_priority=1,
            latency_priority=1,
        )
    )
    assert all(candidate.provider != "runapi" for candidate in decision.candidates)
