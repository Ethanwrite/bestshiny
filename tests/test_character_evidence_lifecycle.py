"""Durable shadow Character Evidence lifecycle: enqueue, dispatch, timeout, reconcile.

Everything here runs against stub producers — MockTransport-level fixtures.
Nothing in this file is, or may be described as, verification of the real
Modal deployment or the real model stack.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import httpx
import pytest
from character_evidence.api import create_api, deliver_or_spool
from character_evidence.client import CharacterEvidenceRemoteError
from character_evidence.schemas import CallbackEnvelope
from character_evidence.tracking import CharacterEvidenceTracker
from fastapi.testclient import TestClient
from production_domain.models import (
    Character,
    CharacterEvidenceSubmission,
    CharacterIdentityVersion,
    Episode,
    GenerationCandidate,
    MediaAsset,
    Scene,
    Shot,
    utcnow,
)
from qa_core import CharacterEvidenceSubmission as SubmissionResult
from sqlalchemy import select

API_KEY = "character-evidence-test-api-key-32-bytes-A7z9"


def _seed_candidate(container, project, *, with_binding: bool, suffix: str = "1"):  # type: ignore[no-untyped-def]
    """A candidate with registered video output, optionally with a bound identity."""

    with container.database.session() as session:
        episode = Episode(
            project_id=project.id, title=f"E{suffix}", episode_number=int(suffix)
        )
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1)
        session.add(scene)
        session.flush()
        shot = Shot(scene_id=scene.id, sequence=1, prompt="a shot", shot_type="DIALOGUE")
        session.add(shot)
        session.flush()
        video_bytes = f"video-{suffix}".encode()
        video = MediaAsset(
            project_id=project.id,
            mime_type="video/mp4",
            asset_type="GENERATED_VIDEO",
            storage_key=f"outputs/candidate-{suffix}.mp4",
            local_path=f"/data/outputs/candidate-{suffix}.mp4",
            sha256=hashlib.sha256(video_bytes).hexdigest(),
            size_bytes=len(video_bytes),
        )
        session.add(video)
        session.flush()
        metadata = {}
        character_id = None
        if with_binding:
            reference_bytes = f"reference-{suffix}".encode()
            reference = MediaAsset(
                project_id=project.id,
                mime_type="image/png",
                asset_type="CHARACTER_MASTER",
                storage_key=f"identities/identity-{suffix}.png",
                sha256=hashlib.sha256(reference_bytes).hexdigest(),
                size_bytes=len(reference_bytes),
            )
            session.add(reference)
            session.flush()
            character = Character(project_id=project.id, name=f"Lin{suffix}")
            session.add(character)
            session.flush()
            identity = CharacterIdentityVersion(
                character_id=character.id,
                version=1,
                master_asset_id=reference.id,
                front_asset_id=reference.id,
            )
            session.add(identity)
            session.flush()
            character.current_identity_version_id = identity.id
            metadata = {"character_state_context": [{"character_id": character.id}]}
            character_id = character.id
        candidate = GenerationCandidate(
            shot_id=shot.id,
            attempt_number=1,
            status="CREATED",
            output_asset_id=video.id,
            metadata_json=metadata,
        )
        session.add(candidate)
        session.flush()
        return candidate.id, character_id


class _AcceptingProducer:
    def __init__(self) -> None:
        self.submissions: list[str] = []
        #: Every character each submission was asked about, in request order.
        self.characters: list[list[str]] = []

    def submit(  # type: ignore[no-untyped-def]
        self,
        video_path,
        *,
        candidate_id,
        character_id=None,
        references=(),
        characters=None,
        shot_type="DIALOGUE",
        sample_positions=None,
    ):
        del video_path, shot_type, sample_positions
        targets = list(characters) if characters is not None else []
        assert targets or references, "dispatch must supply confirmed identity references"
        assert all(target.references for target in targets)
        self.submissions.append(candidate_id)
        self.characters.append(
            [target.character_id for target in targets] or [character_id]
        )
        return SubmissionResult(
            job_id=candidate_id,
            candidate_id=candidate_id,
            status="ACCEPTED",
            submitted_at=utcnow().isoformat(),
        )


class _FailingProducer:
    def submit(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise CharacterEvidenceRemoteError("Modal character evidence is unavailable")


def _tracker(container, producer, **kwargs):  # type: ignore[no-untyped-def]
    # The container QA pipeline carries no producer in tests; route the POST
    # through the stub while keeping the real candidate-metadata bookkeeping.
    container.qa.evidence_producer = producer
    return CharacterEvidenceTracker(
        container.database,
        container.qa,
        threshold_version="character-evidence-thresholds-2026-08-27-v1",
        **kwargs,
    )


def test_enqueue_is_idempotent_and_records_the_shadow_event(container, project) -> None:  # type: ignore[no-untyped-def]
    candidate_id, _ = _seed_candidate(container, project, with_binding=False)
    tracker = _tracker(container, _AcceptingProducer())
    assert tracker.enqueue_ready_candidates() == 1
    assert tracker.enqueue_ready_candidates() == 0
    with container.database.session() as session:
        rows = session.scalars(select(CharacterEvidenceSubmission)).all()
        assert len(rows) == 1
        assert rows[0].candidate_id == candidate_id
        assert rows[0].status == "PENDING"
        assert rows[0].operating_mode == "SHADOW"


def test_dispatch_accepts_once_and_never_reposts_the_same_candidate(container, project) -> None:  # type: ignore[no-untyped-def]
    _seed_candidate(container, project, with_binding=True)
    producer = _AcceptingProducer()
    tracker = _tracker(container, producer)
    tracker.enqueue_ready_candidates()
    first = tracker.dispatch_pending()
    assert first.dispatched == 1
    # The second sweep finds no PENDING row: the same candidate_id can never
    # start a second remote GPU job from this side.
    second = tracker.dispatch_pending()
    assert second.dispatched == 0
    assert len(producer.submissions) == 1
    with container.database.session() as session:
        row = session.scalar(select(CharacterEvidenceSubmission))
        assert row.status == "ACCEPTED"
        assert row.submission_count == 1
        assert row.accepted_at is not None
        assert row.character_id is not None
        candidate = session.get(GenerationCandidate, row.candidate_id)
        assert candidate.metadata_json["character_evidence_status"] == "ACCEPTED"
        assert candidate.metadata_json["character_evidence_mode"] == "SHADOW"


def test_dispatch_without_bindings_is_an_explicit_skip(container, project) -> None:  # type: ignore[no-untyped-def]
    _seed_candidate(container, project, with_binding=False)
    tracker = _tracker(container, _AcceptingProducer())
    tracker.enqueue_ready_candidates()
    result = tracker.dispatch_pending()
    assert result.skipped == 1
    with container.database.session() as session:
        row = session.scalar(select(CharacterEvidenceSubmission))
        assert row.status == "SKIPPED"
        assert row.skip_reason == "NO_CHARACTER_BINDINGS"


def test_remote_failure_burns_bounded_attempts_then_fails_loudly(container, project) -> None:  # type: ignore[no-untyped-def]
    _seed_candidate(container, project, with_binding=True)
    tracker = _tracker(container, _FailingProducer(), max_submission_attempts=2)
    tracker.enqueue_ready_candidates()
    first = tracker.dispatch_pending()
    assert first.retried == 1
    with container.database.session() as session:
        row = session.scalar(select(CharacterEvidenceSubmission))
        assert row.status == "PENDING"
        assert row.submission_count == 1
        assert row.error_code == "CharacterEvidenceRemoteError"
    second = tracker.dispatch_pending()
    assert second.failed == 1
    with container.database.session() as session:
        row = session.scalar(select(CharacterEvidenceSubmission))
        assert row.status == "FAILED"
        assert row.submission_count == 2


def test_missing_producer_leaves_the_backlog_visible(container, project) -> None:  # type: ignore[no-untyped-def]
    _seed_candidate(container, project, with_binding=True)
    container.qa.evidence_producer = None
    tracker = CharacterEvidenceTracker(
        container.database,
        container.qa,
        threshold_version="character-evidence-thresholds-2026-08-27-v1",
    )
    tracker.enqueue_ready_candidates()
    result = tracker.dispatch_pending()
    assert result.dispatcher_absent is True
    with container.database.session() as session:
        row = session.scalar(select(CharacterEvidenceSubmission))
        assert row.status == "PENDING"


def test_silent_acceptance_times_out_into_reconciliation_and_operator_resolves(  # type: ignore[no-untyped-def]
    container, project
) -> None:
    _seed_candidate(container, project, with_binding=True)
    tracker = _tracker(container, _AcceptingProducer(), callback_timeout_seconds=60)
    tracker.enqueue_ready_candidates()
    tracker.dispatch_pending()
    assert tracker.scan_accepted_timeouts() == 0
    with container.database.session() as session:
        row = session.scalar(select(CharacterEvidenceSubmission))
        row.accepted_at = utcnow() - timedelta(seconds=120)
        submission_id = row.id
    assert tracker.scan_accepted_timeouts() == 1
    with container.database.session() as session:
        row = session.scalar(select(CharacterEvidenceSubmission))
        assert row.status == "RECONCILIATION_REQUIRED"
        assert "no signed callback" in row.reconciliation_note
    with pytest.raises(ValueError, match="RESUBMIT or MARK_FAILED"):
        tracker.resolve_reconciliation(
            submission_id, action="RETRY", note="x", resolved_by="op"
        )
    resolved = tracker.resolve_reconciliation(
        submission_id,
        action="RESUBMIT",
        note="Modal logs show the container was recycled; requeueing once.",
        resolved_by="operator@bestshiny",
    )
    assert resolved.status == "PENDING"
    assert resolved.reconciled_by == "operator@bestshiny"


def test_callback_moves_the_submission_to_reported(container, project) -> None:  # type: ignore[no-untyped-def]
    candidate_id, _ = _seed_candidate(container, project, with_binding=True)
    tracker = _tracker(container, _AcceptingProducer())
    tracker.enqueue_ready_candidates()
    tracker.dispatch_pending()
    # A success envelope that reports none of the characters it was asked
    # about is not a report: the job stays ACCEPTED, under its deadline, with
    # the gap on record - REPORTED means every requested character reported.
    tracker.record_callback(candidate_id, status="SUCCEEDED")
    with container.database.session() as session:
        row = session.scalar(select(CharacterEvidenceSubmission))
        assert row.status == "ACCEPTED"
        assert row.reported_at is None
        assert row.metadata_json["missing_character_ids"]
        missing = list(row.metadata_json["missing_character_ids"])
    for character_id in missing:
        tracker.record_character_report(
            candidate_id,
            character_id=character_id,
            producer_run_id="run-1",
            decision="ABSTAIN",
            qa_result_id=None,
        )
    tracker.record_callback(candidate_id, status="SUCCEEDED", character_ids=missing)
    with container.database.session() as session:
        row = session.scalar(select(CharacterEvidenceSubmission))
        assert row.status == "REPORTED"
        assert row.reported_at is not None
        assert row.metadata_json["missing_character_ids"] == []
    # A failure callback for an unknown candidate is tolerated silently: the
    # webhook's lineage checks already rejected anything not ours.
    tracker.record_callback("nonexistent-candidate", status="FAILED")


def test_duplicate_job_claim_returns_202_without_a_second_spawn(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHARACTER_EVIDENCE_API_KEY", API_KEY)
    spawned: list[dict] = []
    claimed: set[str] = set()

    def claim(job_id: str) -> bool:
        if job_id in claimed:
            return False
        claimed.add(job_id)
        return True

    app = create_api(spawned.append, claim_job=claim)
    payload = {
        "job_id": "candidate-dup",
        "project_id": "project-1",
        "shot_id": "shot-1",
        "video_url": "https://media.example/video.mp4?sig=x",
        "characters": [
            {
                "character_id": "character-1",
                "reference_assets": [
                    {
                        "asset_id": "reference-1",
                        "asset_version": "sha256:immutable-v1",
                        "url": "https://media.example/ref.png?sig=x",
                        "view": "FRONT",
                    }
                ],
            }
        ],
        "threshold_version": "character-evidence-thresholds-2026-08-27-v1",
    }
    with TestClient(app) as client:
        first = client.post(
            "/v1/character-evidence/analyze",
            json=payload,
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        second = client.post(
            "/v1/character-evidence/analyze",
            json=payload,
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
    assert first.status_code == 202 and first.json()["duplicate"] is False
    assert second.status_code == 202 and second.json()["duplicate"] is True
    assert len(spawned) == 1


def test_failed_callback_delivery_is_spooled_not_lost(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("CHARACTER_EVIDENCE_CALLBACK_URL", "https://api.bestshiny.example/v1/webhooks/character-evidence")
    monkeypatch.setenv("CHARACTER_EVIDENCE_CALLBACK_SIGNING_KEY", "k" * 40)
    attempts: list[str] = []

    def refusing_post(url, **kwargs):  # type: ignore[no-untyped-def]
        attempts.append(url)
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr(httpx, "post", refusing_post)
    monkeypatch.setattr("character_evidence.api.time.sleep", lambda seconds: None)
    spooled: list[dict] = []
    envelope = CallbackEnvelope(
        job_id="candidate-x",
        project_id="project-1",
        shot_id="shot-1",
        status="FAILED",
        error_code="PipelineError",
        error_message="inference failed",
    )
    delivered = deliver_or_spool(envelope, spooled.append)
    assert delivered is False
    assert len(attempts) == 3, "bounded in-process retries before spooling"
    assert len(spooled) == 1
    assert spooled[0]["envelope"]["job_id"] == "candidate-x"
    assert spooled[0]["attempts"] == 3
