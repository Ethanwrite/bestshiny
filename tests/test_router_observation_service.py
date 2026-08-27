"""The production observation table: what it stores, and what it refuses to store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from production_domain.models import Episode, GenerationJob, JobStatus, RouterObservation, Scene, Shot
from router_evidence_core import (
    HierarchicalPosteriorEngine,
    OutcomeName,
    ProductionObservation,
    PromptComplexity,
    ReferenceMode,
    ReplayHarness,
    Scenario,
    TaskType,
    fixed_order_policy,
)
from router_evidence_core.service import (
    RouterObservationService,
    UnattributableObservation,
    summarize_observations,
)

BASE = datetime(2026, 7, 1, tzinfo=UTC)


def _observation(index: int = 0, **overrides: object) -> ProductionObservation:
    values: dict[str, object] = {
        "observation_id": f"obs-{index:05d}",
        "occurred_at": BASE + timedelta(minutes=index),
        "provider": "wan",
        "model_id": "wan-2.7",
        "exact_version": "wan-2.7",
        "task_type": TaskType.I2V,
        "scenario": Scenario.IDENTITY,
        "asset_criticality": "STANDARD",
        "prompt_complexity": PromptComplexity.COMPLEX,
        "reference_mode": ReferenceMode.FIRST_FRAME,
        "duration_seconds": 5.0,
        "resolution": "720P",
        "aspect_ratio": "16:9",
        "generation_success": True,
        "latency_ms": 42_000,
        "cost_credits": 44.0,
        "cost_usd": 0.35,
        "user_rating": 4,
        "user_preference_ab": "win",
        "user_preference_opponent": "openrouter:google/veo-3.1",
        "regenerated": False,
        "switched_model": False,
        "downloaded": True,
        "accepted_output": True,
        "used_in_next_shot": True,
        "qc_identity_score": 0.88,
        "qc_motion_score": 0.72,
        "qc_prompt_alignment": 0.81,
        "qc_temporal_consistency": 0.90,
        "router_version": "video-router-v2",
    }
    values.update(overrides)
    return ProductionObservation(**values)  # type: ignore[arg-type]


def _job(container, project_id: str) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        job = GenerationJob(
            project_id=project_id,
            generation_type="video",
            provider="wan",
            model="wan-2.7",
            status=JobStatus.COMPLETED,
            request_json={},
            request_hash=f"hash-{project_id}",
        )
        session.add(job)
        session.flush()
        return job.id


def test_the_whole_contract_survives_a_round_trip(container, project) -> None:  # type: ignore[no-untyped-def]
    service = RouterObservationService(container.database)
    service.record(_observation())
    stored = service.observations()
    assert len(stored) == 1
    read = stored[0]
    original = _observation()
    for field in (
        "provider",
        "model_id",
        "exact_version",
        "task_type",
        "scenario",
        "asset_criticality",
        "prompt_complexity",
        "reference_mode",
        "duration_seconds",
        "resolution",
        "generation_success",
        "latency_ms",
        "cost_credits",
        "user_rating",
        "user_preference_ab",
        "downloaded",
        "accepted_output",
        "used_in_next_shot",
        "qc_identity_score",
        "qc_motion_score",
        "qc_prompt_alignment",
        "qc_temporal_consistency",
    ):
        assert getattr(read, field) == getattr(original, field), field
    assert read.cost_usd == pytest.approx(0.35)


def test_an_alias_observation_is_refused_at_the_boundary(container, project) -> None:  # type: ignore[no-untyped-def]
    service = RouterObservationService(container.database)
    with pytest.raises(UnattributableObservation, match="recorded by alias"):
        service.record(_observation(model_is_alias=True))
    assert service.observations() == []


def test_recording_is_idempotent_per_generation_job(container, project) -> None:  # type: ignore[no-untyped-def]
    """A retried worker must not double the count the LCB gate reads."""

    service = RouterObservationService(container.database)
    job_id = _job(container, project.id)
    first = service.record(_observation(generation_job_id=job_id, project_id=project.id))
    second = service.record(
        _observation(1, generation_job_id=job_id, project_id=project.id, accepted_output=False)
    )
    assert first.id == second.id
    assert len(service.observations()) == 1


def test_two_attempts_without_a_job_are_two_rows(container, project) -> None:  # type: ignore[no-untyped-def]
    service = RouterObservationService(container.database)
    service.record(_observation(0))
    service.record(_observation(1))
    assert len(service.observations()) == 2


def test_the_database_refuses_a_failed_row_carrying_a_quality_score(container, project) -> None:  # type: ignore[no-untyped-def]
    """The contract validates it; the constraint is what holds when someone bypasses the contract."""

    with pytest.raises(sa.exc.IntegrityError):
        with container.database.session() as session:
            session.add(
                RouterObservation(
                    occurred_at=BASE,
                    provider="wan",
                    model_id="wan-2.7",
                    exact_version="wan-2.7",
                    task_type="T2V",
                    scenario="motion",
                    asset_criticality="STANDARD",
                    prompt_complexity="MODERATE",
                    reference_mode="NONE",
                    generation_success=False,
                    qc_identity_score=0.9,
                    metadata_json={},
                )
            )
            session.flush()


def test_the_database_refuses_a_rating_outside_one_to_five(container, project) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(sa.exc.IntegrityError):
        with container.database.session() as session:
            session.add(
                RouterObservation(
                    occurred_at=BASE,
                    provider="wan",
                    model_id="wan-2.7",
                    exact_version="wan-2.7",
                    task_type="T2V",
                    scenario="motion",
                    asset_criticality="STANDARD",
                    prompt_complexity="MODERATE",
                    reference_mode="NONE",
                    generation_success=True,
                    user_rating=9,
                    metadata_json={},
                )
            )
            session.flush()


def test_observations_come_back_oldest_first_and_deterministically(container, project) -> None:  # type: ignore[no-untyped-def]
    service = RouterObservationService(container.database)
    for index in reversed(range(10)):
        service.record(_observation(index))
    stored = service.observations()
    assert [item.occurred_at for item in stored] == sorted(item.occurred_at for item in stored)


def test_a_time_window_and_a_provider_filter_narrow_the_read(container, project) -> None:  # type: ignore[no-untyped-def]
    service = RouterObservationService(container.database)
    for index in range(10):
        service.record(_observation(index))
    for index in range(10, 15):
        service.record(_observation(index, provider="openrouter", model_id="google/veo-3.1"))
    assert len(service.observations(provider="openrouter")) == 5
    assert len(service.observations(since=BASE + timedelta(minutes=5))) == 10


def test_a_posterior_run_round_trips_through_the_database(container, project) -> None:  # type: ignore[no-untyped-def]
    service = RouterObservationService(container.database)
    observations = [_observation(index, accepted_output=index % 5 != 0) for index in range(60)]
    for observation in observations:
        service.record(observation)
    run = HierarchicalPosteriorEngine().compute(
        service.observations(), run_id="run-1", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    written = service.save_posterior_run(run)
    assert written == len(run.records)
    assert service.latest_posterior_run_id() == "run-1"

    lookup = service.lookup_for("run-1")
    # The lookup holds only the two levels the LCB reads — scenario and
    # condition — not the version and task aggregates above them.
    assert len(lookup) == len(run.scenario_records()) + len(run.leaf_records())
    assert len(lookup) < written
    original = next(item for item in run.scenario_records())
    restored = lookup.scenario(original.key.token, OutcomeName.ACCEPTED_OUTPUT)
    assert restored is not None
    assert restored.posterior_lower_quantile == pytest.approx(original.posterior_lower_quantile)
    assert restored.observation_count == original.observation_count
    assert restored.condition is None


def test_a_condition_row_keeps_its_bucket_through_the_database(container, project) -> None:  # type: ignore[no-untyped-def]
    service = RouterObservationService(container.database)
    for index in range(30):
        service.record(_observation(index))
    run = HierarchicalPosteriorEngine().compute(
        service.observations(), run_id="run-2", outcomes=[OutcomeName.ACCEPTED_OUTPUT]
    )
    service.save_posterior_run(run)
    lookup = service.lookup_for("run-2")
    leaf = next(item for item in run.leaf_records())
    assert leaf.condition is not None
    restored = lookup.condition(leaf.key.token, leaf.condition, OutcomeName.ACCEPTED_OUTPUT)
    assert restored is not None
    assert restored.condition == leaf.condition


def test_a_replay_result_is_saved_with_its_verdict(container, project) -> None:  # type: ignore[no-untyped-def]
    service = RouterObservationService(container.database)
    observations = []
    for index in range(400):
        provider, model_id, version = (
            ("wan", "wan-2.7", "wan-2.7") if index % 2 else ("openrouter", "google/veo-3.1", "veo-3.1")
        )
        observations.append(
            _observation(
                index,
                provider=provider,
                model_id=model_id,
                exact_version=version,
                accepted_output=(index % 2 == 1) or (index % 7 == 0),
            )
        )
    for observation in observations:
        service.record(observation)
    result = ReplayHarness().run(
        service.observations(),
        run_id="replay-1",
        baseline_policy=fixed_order_policy(["openrouter:google/veo-3.1"]),
    )
    row = service.save_replay(result, posterior_run_id="run-1")
    assert row.run_id == "replay-1"
    assert row.passed is result.passed
    assert row.coverage_json["nominal"] == pytest.approx(0.8)
    assert (service.latest_passing_replay(OutcomeName.ACCEPTED_OUTPUT) is not None) is result.passed


def test_coverage_counts_are_keyed_by_exact_version(container, project) -> None:  # type: ignore[no-untyped-def]
    service = RouterObservationService(container.database)
    for index in range(5):
        service.record(_observation(index))
    for index in range(5, 8):
        service.record(_observation(index, exact_version="wan-2.7-preview"))
    counts = service.coverage_counts()
    assert counts == {"wan:wan-2.7@wan-2.7": 5, "wan:wan-2.7@wan-2.7-preview": 3}


def test_the_summary_reports_volume_and_shape(container, project) -> None:  # type: ignore[no-untyped-def]
    service = RouterObservationService(container.database)
    for index in range(6):
        service.record(_observation(index))
    service.record(
        _observation(
            99,
            generation_success=False,
            provider_failure="PROVIDER_TIMEOUT",
            user_rating=None,
            accepted_output=None,
            qc_identity_score=None,
            qc_motion_score=None,
            qc_prompt_alignment=None,
            qc_temporal_consistency=None,
        )
    )
    summary = summarize_observations(service.observations())
    assert summary["observations"] == 7
    assert summary["successes"] == 6
    assert summary["provider_failures"] == 1
    assert summary["distinct_versions"] == 1


def test_an_observation_links_to_a_shot_and_a_job(container, project) -> None:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="One", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="A")
        session.add(scene)
        session.flush()
        shot = Shot(scene_id=scene.id, sequence=1, prompt="a shot")
        session.add(shot)
        session.flush()
        shot_id = shot.id
    job_id = _job(container, project.id)
    service = RouterObservationService(container.database)
    service.record(_observation(project_id=project.id, shot_id=shot_id, generation_job_id=job_id))
    stored = service.observations()[0]
    assert (stored.project_id, stored.shot_id, stored.generation_job_id) == (
        project.id,
        shot_id,
        job_id,
    )


def test_an_over_long_observation_id_is_refused_not_replaced(container, project) -> None:  # type: ignore[no-untyped-def]
    """A row stored under an id the caller never chose cannot be found again."""

    service = RouterObservationService(container.database)
    with pytest.raises(UnattributableObservation, match="longer than"):
        service.record(_observation(observation_id="o" * 40))
    assert service.observations() == []


def test_coverage_counts_agree_with_the_rows_they_summarise(container, project) -> None:  # type: ignore[no-untyped-def]
    service = RouterObservationService(container.database)
    for index in range(7):
        service.record(_observation(index))
    for index in range(7, 11):
        service.record(_observation(index, exact_version="wan-2.7-manual-v5"))
    counts = service.coverage_counts()
    assert counts == {"wan:wan-2.7@wan-2.7": 7, "wan:wan-2.7@wan-2.7-manual-v5": 4}
    assert sum(counts.values()) == len(service.observations())
