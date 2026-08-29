"""Cross-episode style drift monitoring over the real evaluation path.

Evaluations are produced by the actual ProjectStyleService pixel pipeline
against a real lock — no fabricated similarity numbers — with candidate
outputs colored progressively away from the locked style so the aggregate
walk is real.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from production_domain.models import (
    CandidateStatus,
    Episode,
    GenerationCandidate,
    Scene,
    Shot,
    TimelineState,
    User,
)
from sqlalchemy import select, update
from style_core import StyleDriftMonitor


def _png(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (96, 96), color)
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    return payload.getvalue()


def _media(container, project_id: str, payload: bytes, name: str):  # type: ignore[no-untyped-def]
    return container.media.register(
        project_id, "REFERENCE", io.BytesIO(payload), filename=name, mime_type="image/png"
    )[0]


def _locked_style(container, project_id: str, payload: bytes):  # type: ignore[no-untyped-def]
    media = _media(container, project_id, payload, "style.png")
    asset = container.asset_registry.create(project_id, "STYLE", "锁定画风")
    version = container.asset_registry.add_version(asset.id, primary_media_asset_id=media.id)
    container.asset_registry.promote(asset.id, version.id, reason="user approved style")
    with container.database.session() as session:
        actor = User(email=f"drift-{project_id}@example.com", display_name="Style Owner")
        session.add(actor)
        session.flush()
        actor_id = actor.id
    container.styles.lock(
        project_id,
        version.id,
        locked_by_user_id=actor_id,
        reason="整部作品锁定这一版画风",
        explicit_confirmation=True,
    )


def _shot_in_episode(container, project_id: str, episode_number: int, sequence: int) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = session.scalar(
            select(Episode).where(
                Episode.project_id == project_id,
                Episode.episode_number == episode_number,
            )
        )
        if episode is None:
            episode = Episode(
                project_id=project_id,
                title=f"EP{episode_number}",
                episode_number=episode_number,
            )
            session.add(episode)
            session.flush()
        scene = session.scalar(select(Scene).where(Scene.episode_id == episode.id))
        if scene is None:
            scene = Scene(episode_id=episode.id, sequence=1)
            session.add(scene)
            session.flush()
        start = TimelineState(project_id=project_id, episode_id=episode.id, scene_id=scene.id)
        end = TimelineState(project_id=project_id, episode_id=episode.id, scene_id=scene.id)
        session.add_all([start, end])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=sequence,
            prompt="a drift probe",
            input_state_id=start.id,
            output_state_id=end.id,
        )
        session.add(shot)
        session.flush()
        return shot.id


def _committed_evaluated_candidate(  # type: ignore[no-untyped-def]
    container, project_id: str, episode_number: int, sequence: int, color: tuple[int, int, int]
) -> str:
    shot_id = _shot_in_episode(container, project_id, episode_number, sequence)
    output = _media(container, project_id, _png(color), f"take-{episode_number}-{sequence}.png")
    with container.database.session() as session:
        candidate = GenerationCandidate(
            shot_id=shot_id,
            attempt_number=1,
            output_asset_id=output.id,
            status=CandidateStatus.VALIDATING.value,
        )
        session.add(candidate)
        session.flush()
        candidate_id = candidate.id
    evaluation = container.styles.evaluate_candidate(candidate_id)
    assert evaluation is not None
    with container.database.session() as session:
        session.execute(
            update(GenerationCandidate)
            .where(GenerationCandidate.id == candidate_id)
            .values(status=CandidateStatus.COMMITTED.value)
        )
    return candidate_id


@pytest.fixture
def monitor(container):  # type: ignore[no-untyped-def]
    return StyleDriftMonitor(container.database)


STYLE_COLOR = (40, 90, 160)


def test_no_evaluations_reports_insufficient_data(container, project, monitor) -> None:  # type: ignore[no-untyped-def]
    report = monitor.series_report(project.id)
    assert report.status == "INSUFFICIENT_DATA"
    assert report.episodes == []


def test_a_series_walking_away_from_episode_one_is_flagged(  # type: ignore[no-untyped-def]
    container, project, monitor
) -> None:
    _locked_style(container, project.id, _png(STYLE_COLOR))
    # Episode 1 matches the lock; later episodes drift further per episode.
    _committed_evaluated_candidate(container, project.id, 1, 1, STYLE_COLOR)
    _committed_evaluated_candidate(container, project.id, 1, 2, STYLE_COLOR)
    _committed_evaluated_candidate(container, project.id, 2, 1, (90, 120, 150))
    _committed_evaluated_candidate(container, project.id, 3, 1, (200, 180, 60))

    report = monitor.series_report(project.id)
    assert report.baseline_episode_number == 1
    assert [item.episode_number for item in report.episodes] == [1, 2, 3]
    baseline, second, third = report.episodes
    assert baseline.committed_evaluations == 2
    assert baseline.drift_from_baseline is None
    assert second.drift_from_baseline is not None and second.drift_from_baseline > 0
    assert third.drift_from_baseline is not None and third.drift_from_baseline > 0
    assert report.status == "DRIFTING"
    assert set(report.flagged_episode_numbers) == {2, 3}, (
        "every episode that walked past the threshold is named"
    )
    assert report.max_drift == max(second.drift_from_baseline, third.drift_from_baseline)


def test_a_stable_series_is_stable_and_uncommitted_takes_do_not_count(  # type: ignore[no-untyped-def]
    container, project, monitor
) -> None:
    _locked_style(container, project.id, _png(STYLE_COLOR))
    _committed_evaluated_candidate(container, project.id, 1, 1, STYLE_COLOR)
    _committed_evaluated_candidate(container, project.id, 2, 1, STYLE_COLOR)
    # A wildly off-style take exists in episode 2 but was never committed:
    # the adopted series did not move, and neither may the report.
    shot_id = _shot_in_episode(container, project.id, 2, 7)
    rejected_output = _media(container, project.id, _png((250, 20, 20)), "rejected.png")
    with container.database.session() as session:
        rejected = GenerationCandidate(
            shot_id=shot_id,
            attempt_number=1,
            output_asset_id=rejected_output.id,
            status=CandidateStatus.VALIDATING.value,
        )
        session.add(rejected)
        session.flush()
        rejected_id = rejected.id
    container.styles.evaluate_candidate(rejected_id)

    report = monitor.series_report(project.id)
    assert report.status == "STABLE"
    assert report.flagged_episode_numbers == []
    second = next(item for item in report.episodes if item.episode_number == 2)
    assert second.committed_evaluations == 1
    assert abs(second.drift_from_baseline or 0.0) <= 0.01
