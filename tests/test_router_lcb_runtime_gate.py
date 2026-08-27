"""The LCB in the running system: off by default, and off means unchanged."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from model_registry_core import RoutingEvidence, ShotRequirements
from router_evidence_core import (
    HierarchicalPosteriorEngine,
    OutcomeName,
    ProductionObservation,
    PromptComplexity,
    ReferenceMode,
    Scenario,
    TaskType,
)
from router_evidence_core.service import RouterObservationService

BASE = datetime(2026, 7, 1, tzinfo=UTC)


#: The version the *registry* declares for wan — a profile version, not a
#: provider snapshot. Observations have to be recorded under the same string
#: the router will look up, and a test that hard-codes "wan-2.7" instead
#: silently exercises the miss path and proves nothing.
WAN_PROFILE_VERSION = "wan-2.7-manual-v4"


def _seed(
    service: RouterObservationService,
    *,
    provider: str,
    model_id: str,
    version: str,
    rate: float,
    count: int = 120,
) -> None:
    for index in range(count):
        service.record(
            ProductionObservation(
                observation_id=f"{model_id}-{index:05d}",
                occurred_at=BASE + timedelta(minutes=index),
                provider=provider,
                model_id=model_id,
                exact_version=version,
                task_type=TaskType.T2V,
                scenario=Scenario.GENERIC,
                asset_criticality="STANDARD",
                prompt_complexity=PromptComplexity.MODERATE,
                reference_mode=ReferenceMode.NONE,
                duration_seconds=8.0,
                resolution="720p",
                generation_success=True,
                accepted_output=index < int(count * rate),
                cost_credits=40.0,
            )
        )


def test_the_registry_version_is_what_an_observation_must_carry(container) -> None:  # type: ignore[no-untyped-def]
    """Pin the string, so a registry bump that breaks the join fails here."""

    profile = next(
        item
        for item in container.visual_runtime.router.registry.all()
        if (item.provider, item.model_id) == ("wan", "wan-2.7")
    )
    assert profile.version == WAN_PROFILE_VERSION


def test_the_flag_is_off_in_the_default_container(container) -> None:  # type: ignore[no-untyped-def]
    assert container.settings.feature_router_lcb is False
    assert container.feature_flags.enabled("router_lcb") is False
    assert "router_lcb" in container.feature_flags.known_flags


def test_with_the_flag_off_the_evidence_is_returned_untouched(container, project) -> None:  # type: ignore[no-untyped-def]
    """The claim that publishing this changes no decision, asserted rather than asserted-to."""

    runtime = container.visual_runtime
    service = container.router_observations
    _seed(service, provider="wan", model_id="wan-2.7", version=WAN_PROFILE_VERSION, rate=0.95)
    run = HierarchicalPosteriorEngine().compute(
        service.observations(), run_id="run-1", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    service.save_posterior_run(run)

    baseline = RoutingEvidence(production_adjustments={"wan:wan-2.7": {"visual_quality": 0.11}})
    result = runtime._apply_conservative_lcb(
        baseline, ShotRequirements(), project_id=project.id
    )
    assert result is baseline


def test_with_the_flag_on_but_no_snapshot_the_evidence_is_still_untouched(container, project) -> None:  # type: ignore[no-untyped-def]
    """The first of three documented fallbacks to the routing that already exists."""

    container.feature_flags.set("router_lcb", True)
    baseline = RoutingEvidence()
    result = container.visual_runtime._apply_conservative_lcb(
        baseline, ShotRequirements(), project_id=project.id
    )
    assert result is baseline


def test_with_the_flag_on_and_thin_data_the_evidence_is_still_untouched(container, project) -> None:  # type: ignore[no-untyped-def]
    container.feature_flags.set("router_lcb", True)
    service = container.router_observations
    _seed(service, provider="wan", model_id="wan-2.7", version=WAN_PROFILE_VERSION, rate=0.9, count=6)
    run = HierarchicalPosteriorEngine().compute(
        service.observations(), run_id="run-1", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    service.save_posterior_run(run)
    baseline = RoutingEvidence()
    result = container.visual_runtime._apply_conservative_lcb(
        baseline, ShotRequirements(), project_id=project.id
    )
    assert result is baseline


def test_with_the_flag_on_and_enough_data_the_lower_bound_reaches_the_router(container, project) -> None:  # type: ignore[no-untyped-def]
    container.feature_flags.set("router_lcb", True)
    service = container.router_observations
    _seed(service, provider="wan", model_id="wan-2.7", version=WAN_PROFILE_VERSION, rate=0.85)
    run = HierarchicalPosteriorEngine().compute(
        service.observations(), run_id="run-1", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    service.save_posterior_run(run)

    baseline = RoutingEvidence(production_adjustments={"openrouter:google/veo-3.1": {"visual_quality": 0.7}})
    result = container.visual_runtime._apply_conservative_lcb(
        baseline, ShotRequirements(), project_id=project.id
    )
    assert result is not baseline
    offered = result.production_adjustments["wan:wan-2.7"]["visual_quality"]
    # The condition cell: every seeded shot shares 8s / 720p / no reference.
    record = next(item for item in run.leaf_records())
    assert offered == pytest.approx(record.posterior_lower_quantile, abs=1e-6)
    assert offered < record.posterior_mean
    # The baseline's view of a model the LCB says nothing about survives.
    assert result.production_adjustments["openrouter:google/veo-3.1"]["visual_quality"] == 0.7


def test_the_snapshot_is_cached_and_not_queried_per_request(container, project) -> None:  # type: ignore[no-untyped-def]
    container.feature_flags.set("router_lcb", True)
    service = container.router_observations
    _seed(service, provider="wan", model_id="wan-2.7", version=WAN_PROFILE_VERSION, rate=0.85)
    run = HierarchicalPosteriorEngine().compute(
        service.observations(), run_id="run-1", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    service.save_posterior_run(run)
    runtime = container.visual_runtime
    first = runtime._conservative_lcb_lookup()
    second = runtime._conservative_lcb_lookup()
    assert first is second


def test_a_newer_run_is_not_picked_up_until_the_ttl_expires(container, project) -> None:  # type: ignore[no-untyped-def]
    """The cache is the point: without it every generation queries for a run id."""

    from production_engine.runtime import _LCB_SNAPSHOT_TTL_SECONDS

    container.feature_flags.set("router_lcb", True)
    service = container.router_observations
    _seed(service, provider="wan", model_id="wan-2.7", version=WAN_PROFILE_VERSION, rate=0.85)
    runtime = container.visual_runtime
    service.save_posterior_run(
        HierarchicalPosteriorEngine().compute(
            service.observations(), run_id="run-1", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
        )
    )
    first = runtime._conservative_lcb_lookup()
    service.save_posterior_run(
        HierarchicalPosteriorEngine().compute(
            service.observations(), run_id="run-2", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
        )
    )
    # Inside the window the saved run is not even looked for.
    assert runtime._conservative_lcb_lookup() is first

    # Past it, the newer run replaces the snapshot.
    runtime._lcb_snapshot_checked_at -= _LCB_SNAPSHOT_TTL_SECONDS + 1
    assert runtime._conservative_lcb_lookup() is not first
    assert runtime._lcb_snapshot is not None and runtime._lcb_snapshot[0] == "run-2"


def test_the_latest_run_is_the_last_saved_not_the_last_dated(container, project) -> None:  # type: ignore[no-untyped-def]
    """Ties used to break on a uuid, so which snapshot the router read was random."""

    from datetime import UTC, datetime

    service = container.router_observations
    _seed(service, provider="wan", model_id="wan-2.7", version=WAN_PROFILE_VERSION, rate=0.85, count=30)
    stamp = datetime(2026, 8, 27, tzinfo=UTC)
    for run_id in ("run-a", "run-b", "run-c"):
        service.save_posterior_run(
            HierarchicalPosteriorEngine().compute(
                service.observations(),
                run_id=run_id,
                outcomes=[OutcomeName.ACCEPTED_OUTPUT],
                now=stamp,
            )
        )
    # All three share `calculated_at` exactly; insertion order decides.
    assert service.latest_posterior_run_id() == "run-c"


def test_the_router_itself_was_not_changed() -> None:
    """The mandate says not to refactor a stable base, so pin its identity."""

    from model_registry_core import VideoModelRouter

    assert VideoModelRouter.version == "video-router-v2"
    signature = VideoModelRouter.rank.__code__.co_varnames[: VideoModelRouter.rank.__code__.co_argcount]
    assert signature == ("self", "requirements")
    assert set(VideoModelRouter.profile_weights) == {
        "generic",
        "action",
        "commercial_hero",
        "dialogue",
    }
