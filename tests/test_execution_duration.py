"""Requested duration is the director's; execution duration is the model's.

A shot asks for a length. Which model renders it is decided by the router,
and a model declares what it can run: a range, and sometimes a discrete set
(Veo publishes 4, 6 and 8 seconds). The router plans the execution length -
the request itself when legal, the shortest legal length above it otherwise
- and carries it on the candidate; the planner and passenger admission quote
and submit that length while the canonical shot keeps the request. A request
over a model's ceiling is not clamped: the model is rejected with the
SPLIT_SHOT plan that would fit, so narrative intent is never truncated in
silence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from model_registry_core import (
    EXACT,
    SNAP_UP,
    SPLIT_SHOT,
    ModelCapabilityProfile,
    ShotRequirements,
    VideoModelRouter,
    plan_execution_duration,
)
from platform_contracts import GenerationRequest
from platform_database import Database
from production_domain.models import ModelCapabilityProfile as ModelCapabilityProfileRow
from production_domain.models import ModelDefinition

ROOT = Path(__file__).resolve().parents[1]


# --- the plan --------------------------------------------------------------------


def test_a_legal_request_runs_exactly_as_asked() -> None:
    plan = plan_execution_duration(6, min_duration=4, max_duration=8, supported_durations=[4, 6, 8])
    assert (plan.strategy, plan.execution_duration, plan.segments) == (EXACT, 6.0, (6.0,))
    assert plan.runnable
    continuous = plan_execution_duration(7.5, min_duration=2, max_duration=15)
    assert (continuous.strategy, continuous.execution_duration) == (EXACT, 7.5)


def test_a_request_between_declared_steps_runs_at_the_next_step_up() -> None:
    plan = plan_execution_duration(5, min_duration=4, max_duration=8, supported_durations=[4, 6, 8])
    assert (plan.strategy, plan.execution_duration) == (SNAP_UP, 6.0)
    # Never down: 7s becomes 8s, not 6s. Cutting a shot short truncates it.
    assert plan_execution_duration(7, supported_durations=[4, 6, 8]).execution_duration == 8.0


def test_a_request_below_the_minimum_runs_at_the_minimum() -> None:
    plan = plan_execution_duration(1, min_duration=2, max_duration=15)
    assert (plan.strategy, plan.execution_duration) == (SNAP_UP, 2.0)
    stepped = plan_execution_duration(3, min_duration=4, max_duration=8, supported_durations=[4, 6, 8])
    assert (stepped.strategy, stepped.execution_duration) == (SNAP_UP, 4.0)


def test_a_request_over_the_ceiling_is_a_split_plan_not_a_clamp() -> None:
    plan = plan_execution_duration(12, min_duration=4, max_duration=8, supported_durations=[4, 6, 8])
    assert plan.strategy == SPLIT_SHOT and not plan.runnable
    assert plan.segments == (6.0, 6.0)
    assert plan.execution_duration == 12.0
    assert "SPLIT_SHOT" in plan.detail and "2 segments" in plan.detail
    fifteen = plan_execution_duration(15, supported_durations=[4, 6, 8])
    assert fifteen.segments == (8.0, 8.0), "each segment snaps up to a legal step"
    continuous = plan_execution_duration(20, min_duration=2, max_duration=8)
    assert continuous.strategy == SPLIT_SHOT and continuous.segments == (6.67, 6.67, 6.67)


def test_declared_steps_outside_the_range_are_ignored() -> None:
    plan = plan_execution_duration(9, min_duration=4, max_duration=8, supported_durations=[4, 6, 8, 12])
    assert plan.strategy == SPLIT_SHOT, "12 is declared but above the ceiling, so it is not legal"


# --- the router ------------------------------------------------------------------


def _profile(
    logical_name: str,
    provider: str,
    model_id: str,
    *,
    max_duration: float = 15,
    min_duration: float = 1,
    provider_metadata: dict[str, object] | None = None,
    priors: float = 0.5,
) -> ModelCapabilityProfile:
    metadata: dict[str, object] = {"cost": {"normalized": 0.5, "estimated_per_second": 0.1}}
    metadata.update(provider_metadata or {})
    return ModelCapabilityProfile(
        model_definition_id=f"def-{logical_name}",
        logical_name=logical_name,
        model_id=model_id,
        provider=provider,
        modality="video",
        version=f"{logical_name}-test-v1",
        supported_operations=["video_generation"],
        supports_t2v=True,
        supports_i2v=True,
        supports_reference_image=True,
        supports_multi_reference=True,
        supports_start_frame=True,
        supports_end_frame=True,
        max_duration=max_duration,
        min_duration=min_duration,
        supported_resolutions=["720p", "1080p"],
        supported_aspect_ratios=["9:16", "16:9"],
        max_reference_images=4,
        physics_prior=priors,
        identity_prior=priors,
        camera_prior=priors,
        render_prior=priors,
        action_prior=priors,
        dialogue_prior=priors,
        text_render_prior=priors,
        provider_metadata=metadata,
    )


class _Registry:
    def __init__(self, profiles: list[ModelCapabilityProfile]):
        self._profiles = profiles

    def all(self, include_disabled: bool = False) -> list[ModelCapabilityProfile]:
        return list(self._profiles)


def test_the_router_carries_each_candidates_execution_length() -> None:
    stepped = _profile(
        "veo-like", "prov-a", "model-a", min_duration=4, max_duration=8,
        provider_metadata={"supported_durations": [4, 6, 8]},
    )
    ranged = _profile("range-like", "prov-b", "model-b", min_duration=2, max_duration=15)
    router = VideoModelRouter(_Registry([stepped, ranged]))

    decision = router.rank(ShotRequirements(duration=5))

    by_model = {candidate.model: candidate for candidate in decision.candidates}
    assert by_model["model-a"].execution_duration == 6.0
    assert by_model["model-a"].execution_strategy == SNAP_UP
    assert by_model["model-a"].execution_segments == [6.0]
    assert by_model["model-b"].execution_duration == 5.0
    assert by_model["model-b"].execution_strategy == EXACT
    assert decision.requested_duration == 5.0
    assert decision.execution_duration == by_model[decision.recommended].execution_duration
    assert decision.rejected == []


def test_a_below_minimum_request_is_eligible_at_the_minimum() -> None:
    ranged = _profile("wan-like", "prov-b", "model-b", min_duration=2, max_duration=15)
    decision = VideoModelRouter(_Registry([ranged])).rank(ShotRequirements(duration=1))
    assert decision.recommended == "model-b"
    assert (decision.execution_duration, decision.execution_strategy) == (2.0, SNAP_UP)


def test_a_request_over_a_models_ceiling_rejects_it_with_the_split_plan() -> None:
    short = _profile("short", "prov-a", "model-a", min_duration=4, max_duration=8,
                     provider_metadata={"supported_durations": [4, 6, 8]})
    long = _profile("long", "prov-b", "model-b", min_duration=2, max_duration=15)
    decision = VideoModelRouter(_Registry([short, long])).rank(ShotRequirements(duration=12))

    assert decision.recommended == "model-b"
    assert decision.execution_strategy == EXACT
    rejected = {f"{item.provider}:{item.model}": item for item in decision.rejected}
    assert rejected["prov-a:model-a"].reason_codes == ["DURATION_UNSUPPORTED"]
    assert "SPLIT_SHOT" in rejected["prov-a:model-a"].details[0]
    assert "6s, 6s" in rejected["prov-a:model-a"].details[0]


def test_no_route_names_the_split_that_would_fit() -> None:
    short = _profile("short", "prov-a", "model-a", min_duration=4, max_duration=8)
    with pytest.raises(LookupError, match="DURATION_UNSUPPORTED") as refused:
        VideoModelRouter(_Registry([short])).rank(ShotRequirements(duration=12))
    assert "SPLIT_SHOT would fit: prov-a:model-a" in str(refused.value)


def test_mode_bounds_and_steps_still_govern_the_plan() -> None:
    """Wan's per-mode ceiling with a reference video narrows the envelope."""

    wan = _profile(
        "wan", "wan", "wan-2.7", min_duration=2, max_duration=15,
        provider_metadata={
            "modes": {
                "r2v": {"min_duration": 2, "max_duration": 15, "max_duration_with_reference_video": 10,
                        "accepts": ["first_frame", "reference_image", "reference_video"]},
            }
        },
    )
    wan = wan.model_copy(update={"supports_v2v": True})
    router = VideoModelRouter(_Registry([wan]))
    fits = router.rank(ShotRequirements(duration=8, requires_reference_video=True))
    assert fits.execution_duration == 8.0
    with pytest.raises(LookupError, match="DURATION_UNSUPPORTED"):
        router.rank(ShotRequirements(duration=12, requires_reference_video=True))
    assert router.rank(ShotRequirements(duration=12)).execution_duration == 12.0


# --- passenger admission -----------------------------------------------------------


def test_passenger_video_is_quoted_and_submitted_at_the_execution_length(container, project, monkeypatch):  # type: ignore[no-untyped-def]
    """A 5-second request on a 4/6/8 model is quoted for 6 seconds, and says so."""

    monkeypatch.setattr(container.providers.get("openrouter"), "configured", True, raising=False)
    priced: dict[str, object] = {}
    original = container.credit_pricing.estimate

    def estimate(**kwargs):  # type: ignore[no-untyped-def]
        priced.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(container.credit_pricing, "estimate", estimate)
    admitted = container.generation_admission.admit_passenger(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="openrouter",
            model="google/veo-3.1",
            prompt="a lantern-lit alley after rain",
            duration=5,
            idempotency_key="veo-5s",
        ),
        enforce_plan=False,
    )
    assert admitted.request.duration == 6.0
    assert priced["duration"] == 6.0, "the quote is for what runs"
    assert admitted.request.metadata["requested_duration_seconds"] == 5.0
    assert admitted.request.metadata["execution_duration_seconds"] == 6.0
    assert admitted.request.metadata["execution_strategy"] == SNAP_UP


def test_passenger_video_over_the_ceiling_is_refused_before_any_reservation(container, project, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(container.providers.get("openrouter"), "configured", True, raising=False)
    with pytest.raises(ValueError, match="SPLIT_SHOT"):
        container.generation_admission.admit_passenger(
            GenerationRequest(
                project_id=project.id,
                type="video",
                provider="openrouter",
                model="google/veo-3.1",
                prompt="a lantern-lit alley after rain",
                duration=9,
                idempotency_key="veo-9s",
            ),
            enforce_plan=False,
        )


def test_a_legal_passenger_duration_is_untouched(container, project, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(container.providers.get("openrouter"), "configured", True, raising=False)
    admitted = container.generation_admission.admit_passenger(
        GenerationRequest(
            project_id=project.id,
            type="video",
            provider="openrouter",
            model="google/veo-3.1",
            prompt="a lantern-lit alley after rain",
            duration=8,
            idempotency_key="veo-8s",
        ),
        enforce_plan=False,
    )
    assert admitted.request.duration == 8.0
    assert admitted.request.metadata["execution_strategy"] == EXACT


# --- the registry declaration and its migration ----------------------------------------


def test_the_seeded_veo_profiles_declare_their_steps(container) -> None:  # type: ignore[no-untyped-def]
    for model_id in ("google/veo-3.1",):
        profile = container.model_registry.get(model_id, "openrouter")
        assert profile is not None
        assert profile.provider_metadata["supported_durations"] == [4, 6, 8]


def test_migration_0081_writes_the_steps_onto_existing_veo_rows_and_takes_them_away(  # type: ignore[no-untyped-def]
    tmp_path, monkeypatch
) -> None:
    database_url = f"sqlite:///{tmp_path / 'veo-durations.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    command.upgrade(config, "0080_creative_turn_claims")

    database = Database(database_url)
    with database.session() as session:
        veo = ModelDefinition(
            logical_name="veo-3.1-openrouter",
            provider="openrouter",
            provider_model_id="google/veo-3.1",
            modality="video",
            capabilities=["video_generation"],
            quality_tier="PREMIUM",
            cost_class="STANDARD",
            provider_trust_level="PRODUCTION",
            criticality_allowed=["STANDARD"],
            enabled=True,
            live_enabled=False,
            pricing_status="VERIFIED",
            max_duration=8,
            metadata_json={
                "transport": "openrouter",
                "supported_durations_seconds": [4, 6, 8],
                "duration_admission_gap": "the profile holds only min/max",
            },
        )
        session.add(veo)
        session.flush()
        session.add(
            ModelCapabilityProfileRow(
                model_definition_id=veo.id,
                profile_version="veo-3.1-openrouter-manual-v1",
                supported_operations=["video_generation"],
                min_duration=4,
                max_duration=8,
                provider_metadata={"adapter": "openrouter", "cost": {"normalized": 0.6}},
            )
        )
    database.engine.dispose()

    command.upgrade(config, "head")
    database = Database(database_url)
    with database.session() as session:
        definition = session.scalar(
            sa.select(ModelDefinition).where(ModelDefinition.logical_name == "veo-3.1-openrouter")
        )
        assert definition is not None
        profile = session.scalar(
            sa.select(ModelCapabilityProfileRow).where(
                ModelCapabilityProfileRow.model_definition_id == definition.id
            )
        )
        assert profile is not None
        assert profile.provider_metadata["supported_durations"] == [4, 6, 8]
        assert profile.provider_metadata["adapter"] == "openrouter", "nothing else on the row moved"
        assert "duration_admission_gap" not in definition.metadata_json
        assert "duration_admission_note" in definition.metadata_json
        assert definition.metadata_json["supported_durations_seconds"] == [4, 6, 8]
    database.engine.dispose()

    command.downgrade(config, "0080_creative_turn_claims")
    database = Database(database_url)
    with database.session() as session:
        definition = session.scalar(
            sa.select(ModelDefinition).where(ModelDefinition.logical_name == "veo-3.1-openrouter")
        )
        profile = session.scalar(
            sa.select(ModelCapabilityProfileRow).where(
                ModelCapabilityProfileRow.model_definition_id == definition.id
            )
        )
        assert "supported_durations" not in profile.provider_metadata
        assert "duration_admission_gap" in definition.metadata_json
    database.engine.dispose()
