import pytest
from model_registry_core import (
    RoutingEvidence,
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
    assert wan.supports_i2v is True


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
    # 45s exceeds every registered envelope, including Wan 3.0's 30s maximum.
    with pytest.raises(LookupError, match="no active model"):
        container.video_router.rank(
            ShotRequirements(duration=45, requires_end_frame=True, resolution="1080p")
        )


def test_the_long_end_frame_shot_is_unroutable_again_with_wan_30_disabled(container):
    """Wan 3.0 was the only 30s model, and this account has no access to it.

    Disabling it in the registry (2026-08-25) puts the ceiling back at 15s, so a
    20s shot has no home. That is the honest outcome and it is asserted here
    rather than left to be discovered by a user: routing fails before a Job
    exists, naming the reason, instead of silently truncating the shot.
    """

    with pytest.raises(LookupError, match="DURATION_UNSUPPORTED"):
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


def test_router_never_ranks_a_model_that_cannot_generate_video(container):
    """The registry holds every modality; this router must rank only one.

    Before the modality gate, a plain T2V request ranked eight non-video
    models — chat, embedding and multimodal-embedding alike — and
    `openrouter:google/gemini-embedding-2` scored *above* `wan:wan-2.7`, the
    one model with live evidence behind it. With the better-scoring video
    models excluded (an unconfigured provider is enough), the router would
    have recommended an embedding model for a video shot.
    """

    router = container.video_router
    modalities = {profile.key: profile.modality for profile in router.registry.all()}
    assert {"embedding", "text_multimodal"} <= set(modalities.values()), (
        "fixture no longer registers non-video models, so this gate proves nothing"
    )

    decision = router.rank(ShotRequirements())

    assert all(modalities[f"{c.provider}:{c.model}"] == "video" for c in decision.candidates)
    assert all("video_generation" in router.registry.get(c.model, c.provider).supported_operations
               for c in decision.candidates)


def test_a_rejected_model_records_why_rather_than_vanishing(container):
    decision = container.video_router.rank(ShotRequirements())
    rejected = {f"{item.provider}:{item.model}": item for item in decision.rejected}

    embedding = rejected["openrouter:google/gemini-embedding-2"]
    assert embedding.modality == "embedding"
    assert embedding.reason_codes == ["MODALITY_MISMATCH", "VIDEO_GENERATION_UNSUPPORTED"]

    chat = rejected["openrouter:anthropic/claude-opus-5"]
    assert "MODALITY_MISMATCH" in chat.reason_codes


def test_an_unroutable_request_names_the_reasons_it_could_not_route(container):
    with pytest.raises(LookupError, match="MODALITY_MISMATCH"):
        container.video_router.rank(
            ShotRequirements(duration=45, requires_end_frame=True, resolution="1080p")
        )


def test_live_evidence_is_passed_per_call_and_cannot_be_written_onto_the_router(container):
    """The router is a container singleton shared across concurrent requests."""

    router = container.video_router
    with pytest.raises(AttributeError):
        router.production_adjustments = {"wan:wan-2.7": {"visual_quality": 1.0}}

    baseline = router.rank(ShotRequirements(profile="commercial_hero", cost_priority=0))
    boosted = router.rank(
        ShotRequirements(profile="commercial_hero", cost_priority=0),
        evidence=RoutingEvidence(
            production_adjustments={"wan:wan-2.7": {"visual_quality": 1.0}},
            production_sample_counts={"wan:wan-2.7": 100},
        ),
    )
    baseline_wan = next(c for c in baseline.candidates if c.model == "wan-2.7")
    boosted_wan = next(c for c in boosted.candidates if c.model == "wan-2.7")
    assert boosted_wan.score > baseline_wan.score

    # The boosted call must not have leaked into the router for the next one.
    replayed = router.rank(ShotRequirements(profile="commercial_hero", cost_priority=0))
    replayed_wan = next(c for c in replayed.candidates if c.model == "wan-2.7")
    assert replayed_wan.score == baseline_wan.score
