from __future__ import annotations

from pathlib import Path

import pytest
from generation_gateway import GenerationTargetError
from model_registry_core import CapabilityObservationConflict, ModelRole
from production_domain.models import (
    ModelCapabilityProfile as ModelCapabilityProfileRow,
)
from production_domain.models import (
    ModelDefinition,
)
from sqlalchemy import select


def test_model_capability_has_single_truth_source(container) -> None:
    """Legacy ModelDefinition mirrors cannot authorize an operation."""

    with container.database.session() as session:
        definition = session.scalar(
            select(ModelDefinition).where(ModelDefinition.logical_name == "flow-veo-3.1-internal")
        )
        assert definition is not None
        profile = session.get(ModelCapabilityProfileRow, definition.id)
        assert profile is not None
        definition.capabilities = ["video_generation", "invented_operation"]
        definition.max_duration = 999
        profile.supported_operations = []

    assert container.model_registry.get("veo", "google_flow").max_duration == 8
    with pytest.raises(LookupError, match="no compatible model binding"):
        container.model_infrastructure.resolve_role(ModelRole.VIDEO_FLOW)
    with pytest.raises(GenerationTargetError) as exc:
        container.providers.validate_target("google_flow", "veo", "video")
    assert exc.value.code == "CAPABILITY_NOT_SUPPORTED"


def test_runtime_capability_observation_can_only_narrow_registry(container) -> None:
    profile = container.model_registry.get("flow-veo-3.1", "google_flow")
    assert profile is not None

    narrowed = container.model_registry.merge_runtime_observation(
        profile.model_definition_id,
        {
            "supports_end_frame": False,
            "max_reference_images": 1,
            "supported_resolutions": ["720p"],
        },
    )
    assert narrowed.supports_end_frame is False
    assert narrowed.max_reference_images == 1
    assert narrowed.supported_resolutions == ["720p"]
    assert container.model_registry.get("flow-veo-3.1", "google_flow").supports_end_frame is True

    with pytest.raises(CapabilityObservationConflict, match="expands supports_text_rendering"):
        container.model_registry.merge_runtime_observation(
            profile.model_definition_id,
            {"supports_text_rendering": True},
        )
    with pytest.raises(CapabilityObservationConflict, match="expands supported_resolutions"):
        container.model_registry.merge_runtime_observation(
            profile.model_definition_id,
            {"supported_resolutions": ["4k"]},
        )


def test_manual_prior_and_admin_profile_survive_default_sync(container) -> None:
    with container.database.session() as session:
        definition = session.scalar(
            select(ModelDefinition).where(ModelDefinition.logical_name == "grok-video-official")
        )
        assert definition is not None
        profile = session.get(ModelCapabilityProfileRow, definition.id)
        assert profile is not None
        assert profile.source == "MANUAL_PRIOR"
        profile.physics_prior = 0.31

    result = container.model_infrastructure.ensure_defaults()

    assert result.models_created == 0
    assert result.profiles_created == 0
    assert container.model_registry.get("grok-video", "grok").physics_prior == 0.31


def test_each_wan_version_carries_its_own_reviewed_profile(container) -> None:
    """Versions may coexist, but neither may borrow the other's priors."""

    wan27 = container.model_registry.get("wan-2.7", "wan")
    wan30 = container.model_registry.get("wan-3.0", "wan")
    assert wan27 is not None and wan30 is not None
    assert wan27.logical_name == "wan-2.7-official"
    assert wan30.logical_name == "wan-3.0-official"
    # Distinct profile versions: 3.0 is reviewed on its own evidence.
    assert wan27.version == "wan-2.7-manual-v1"
    assert wan30.version == "wan-3.0-manual-v1"
    assert wan27.supports_t2v is True
    assert wan27.supports_i2v is False
    # 3.0's published envelope is materially different and must not be flattened.
    assert wan30.max_duration == 30 and wan27.max_duration < 30
    # 2.7 stays the primary route; 3.0 is an explicit fallback, not a silent swap.
    assert container.model_infrastructure.resolve_role(ModelRole.VIDEO_WAN).provider_model_id == "wan-2.7"
    assert not Path("config/video-models").exists() or not list(Path("config/video-models").glob("*.json"))
