"""The one-time audit touches only rows nothing has ever claimed.

An orphaned empty candidate is recognised by what it lacks — CREATED, no job
bound from either direction, no media, old enough that no transaction is still
in flight. A legitimate CREATED candidate is bound to its job in the very
transaction that creates it, so it can never satisfy that predicate; the audit
must leave it, and everything younger or busier, exactly alone.
"""

from __future__ import annotations

from datetime import timedelta

from production_domain.models import (
    CandidateStatus,
    DecisionRecord,
    Episode,
    GenerationCandidate,
    GenerationJob,
    MediaAsset,
    Scene,
    Shot,
    utcnow,
)
from sqlalchemy import select

from scripts.retire_empty_candidates import (
    RETIREMENT_REASON,
    find_orphaned_empty_candidates,
    retire_candidates,
)


def _shot(container, project_id: str) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(project_id=project_id, title="Episode", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="alley")
        session.add(scene)
        session.flush()
        shot = Shot(scene_id=scene.id, sequence=1, prompt="a lantern-lit alley", duration=4)
        session.add(shot)
        session.flush()
        return shot.id


def _candidate(
    container,  # type: ignore[no-untyped-def]
    shot_id: str,
    attempt: int,
    *,
    status: str = "CREATED",
    age: timedelta = timedelta(days=2),
    batch_index: int | None = 1,
) -> str:
    with container.database.session() as session:
        candidate = GenerationCandidate(
            shot_id=shot_id,
            attempt_number=attempt,
            status=status,
            metadata_json={"batch_index": batch_index} if batch_index is not None else {},
        )
        session.add(candidate)
        session.flush()
        candidate.created_at = utcnow() - age
        session.flush()
        return candidate.id


def _bind_job(container, project_id: str, candidate_id: str, *, forward_only: bool = False) -> str:  # type: ignore[no-untyped-def]
    """Bind a job to the candidate; `forward_only` sets only GenerationJob.candidate_id."""

    with container.database.session() as session:
        job = GenerationJob(
            project_id=project_id,
            candidate_id=candidate_id,
            generation_type="image",
            provider="fake",
            model="fake-model",
            request_json={"prompt": "bound"},
            request_hash="0" * 64,
        )
        session.add(job)
        session.flush()
        if not forward_only:
            candidate = session.get(GenerationCandidate, candidate_id)
            candidate.generation_job_id = job.id
        return job.id


def test_audit_finds_only_old_unclaimed_empty_created_rows(container, project) -> None:  # type: ignore[no-untyped-def]
    shot_id = _shot(container, project.id)
    orphan = _candidate(container, shot_id, 1)
    young_orphan = _candidate(container, shot_id, 2, age=timedelta(minutes=5))
    generating = _candidate(container, shot_id, 3, status="GENERATING")
    bound_both_ways = _candidate(container, shot_id, 4)
    _bind_job(container, project.id, bound_both_ways)
    # A job that claims the candidate without the back-pointer still counts as
    # a claim: the forward pointer alone must protect the row.
    claimed_forward = _candidate(container, shot_id, 5)
    _bind_job(container, project.id, claimed_forward, forward_only=True)
    with_media = _candidate(container, shot_id, 6)
    with container.database.session() as session:
        session.add(
            MediaAsset(
                project_id=project.id,
                asset_type="IMAGE",
                sha256="b" * 64,
                lineage_key=f"candidate:{with_media}",
                storage_key="somewhere/asset.png",
                mime_type="image/png",
                size_bytes=10,
                generation_candidate_id=with_media,
            )
        )

    found = find_orphaned_empty_candidates(
        container.database, older_than=timedelta(hours=24), limit=100
    )
    assert [item["candidate_id"] for item in found] == [orphan]
    assert found[0]["project_id"] == project.id
    assert found[0]["shot_id"] == shot_id
    assert found[0]["batch_index"] == 1
    # The others are all invisible to the audit, whatever their age.
    assert {young_orphan, generating, bound_both_ways, claimed_forward, with_media}.isdisjoint(
        {item["candidate_id"] for item in found}
    )


def test_retirement_is_fenced_and_audited(container, project) -> None:  # type: ignore[no-untyped-def]
    shot_id = _shot(container, project.id)
    orphan = _candidate(container, shot_id, 1)
    contested = _candidate(container, shot_id, 2)

    found = find_orphaned_empty_candidates(
        container.database, older_than=timedelta(hours=24), limit=100
    )
    assert {item["candidate_id"] for item in found} == {orphan, contested}

    # Between audit and retirement, a job claims one row. The conditional
    # update re-checks emptiness and must skip it.
    _bind_job(container, project.id, contested)

    outcomes = retire_candidates(container.database, found)
    by_id = {item["candidate_id"]: item["retired"] for item in outcomes}
    assert by_id == {orphan: True, contested: False}

    with container.database.session() as session:
        retired = session.get(GenerationCandidate, orphan)
        assert retired.status == CandidateStatus.RETIRED.value
        assert retired.rejection_reason == RETIREMENT_REASON
        survivor = session.get(GenerationCandidate, contested)
        assert survivor.status == CandidateStatus.CREATED.value
        assert survivor.rejection_reason is None
        audit_rows = session.scalars(
            select(DecisionRecord).where(DecisionRecord.decision_type == "CANDIDATE_RETIREMENT")
        ).all()
        assert len(audit_rows) == 1
        assert audit_rows[0].input_features["candidate_id"] == orphan
        assert audit_rows[0].shot_id == shot_id

    # Re-running the audit finds nothing: RETIRED rows fail the predicate.
    assert (
        find_orphaned_empty_candidates(container.database, older_than=timedelta(hours=24), limit=100)
        == []
    )
