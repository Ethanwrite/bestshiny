from pathlib import Path

import pytest
from model_registry_core import (
    ModelCapabilityProfile,
    ModelCapabilityRegistry,
    ShotRequirements,
    VideoModelRouter,
)
from provider_sdk import AssetCriticality, ProviderTrustLevel

CONFIG_ROOT = Path(__file__).parents[1] / "config" / "video-models"


def test_registry_loads_valid_versioned_profiles():
    registry = ModelCapabilityRegistry(CONFIG_ROOT)
    grok = registry.get("grok-video", "grok")
    assert grok is not None
    assert grok.failure_priors["end_frame_direct_gaze"] == 0.8
    assert grok.adapter == "grok"
    assert registry.get("wan-3.0", "wan").confidence_level == "experimental"


def test_router_penalizes_grok_for_rear_view_ending():
    router = VideoModelRouter(ModelCapabilityRegistry(CONFIG_ROOT))
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


def test_router_uses_dynamic_commercial_and_action_weights():
    router = VideoModelRouter(ModelCapabilityRegistry(CONFIG_ROOT))
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
    assert action.candidates[0].model == "kling-3.0"


def test_router_rejects_models_without_required_duration_or_features():
    decision = VideoModelRouter(ModelCapabilityRegistry(CONFIG_ROOT)).rank(
        ShotRequirements(duration=20, requires_end_frame=True, resolution="1080p")
    )
    assert [candidate.model for candidate in decision.candidates] == ["wan-3.0"]


@pytest.mark.parametrize("criticality", [AssetCriticality.CANONICAL, AssetCriticality.HERO])
def test_router_never_routes_canonical_or_hero_assets_to_edge_provider(criticality):  # type: ignore[no-untyped-def]
    registry = ModelCapabilityRegistry(CONFIG_ROOT)
    registry.replace(
        ModelCapabilityProfile(
            model_id="edge-video",
            provider="runapi",
            version="test",
            max_duration=60,
            supported_resolutions=["720p", "1080p"],
            capability_prior={"visual_quality": 1.0, "product_fidelity": 1.0},
            cost={"normalized": 0.0},
            latency={"normalized": 0.0},
            adapter="wan",
            provider_trust_level=ProviderTrustLevel.EDGE,
            criticality_allowed=[AssetCriticality.EDGE, AssetCriticality.TEMPORARY],
        )
    )
    decision = VideoModelRouter(registry).rank(
        ShotRequirements(
            profile="commercial_hero",
            asset_criticality=criticality,
            cost_priority=1,
            latency_priority=1,
        )
    )
    assert all(candidate.provider != "runapi" for candidate in decision.candidates)
