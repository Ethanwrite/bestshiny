"""Every bound character is analysed, and the analysis stays SHADOW.

The dispatcher looped over a candidate's bound characters and returned on the
first one that resolved identity references, putting the rest into a metadata
key nothing read. A two-hander produced evidence for one face and silence for
the other. The wire contract could not express more than one character either
- the Modal client built `characters` as a one-element literal - and a
per-character fan-out of separate POSTs could not work, because the Modal side
claims idempotency on `job_id` alone, which is the bare candidate id.

Everything here runs against stub producers. Nothing in this file is, or may be
described as, verification of the real Modal deployment or the real model stack.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

import pytest
from production_domain.models import (
    Character,
    CharacterEvidenceCoverage,
    CharacterEvidenceSubmission,
    CharacterIdentityVersion,
    Episode,
    GenerationCandidate,
    MediaAsset,
    Scene,
    Shot,
)
from sqlalchemy import select
from test_character_evidence_lifecycle import _AcceptingProducer, _tracker


def _seed_two_hander(container, project, *, second_has_identity: bool = True):  # type: ignore[no-untyped-def]
    """A candidate whose shot binds two characters, one or both with an identity."""

    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="E1", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1)
        session.add(scene)
        session.flush()
        shot = Shot(scene_id=scene.id, sequence=1, prompt="a two-hander", shot_type="DIALOGUE")
        session.add(shot)
        session.flush()
        video_bytes = b"two-hander-output"
        video = MediaAsset(
            project_id=project.id,
            mime_type="video/mp4",
            asset_type="SHOT_OUTPUT",
            storage_key="outputs/two-hander.mp4",
            sha256=hashlib.sha256(video_bytes).hexdigest(),
            size_bytes=len(video_bytes),
        )
        session.add(video)
        session.flush()
        character_ids: list[str] = []
        for index, name in enumerate(("Mira", "Ren"), 1):
            character = Character(project_id=project.id, name=name)
            session.add(character)
            session.flush()
            character_ids.append(character.id)
            if index == 2 and not second_has_identity:
                continue
            reference_bytes = f"reference-{name}".encode()
            reference = MediaAsset(
                project_id=project.id,
                mime_type="image/png",
                asset_type="CHARACTER_MASTER",
                storage_key=f"identities/{name}.png",
                sha256=hashlib.sha256(reference_bytes).hexdigest(),
                size_bytes=len(reference_bytes),
            )
            session.add(reference)
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
        candidate = GenerationCandidate(
            shot_id=shot.id,
            attempt_number=1,
            status="CREATED",
            output_asset_id=video.id,
            metadata_json={
                "character_state_context": [
                    {"character_id": character_id} for character_id in character_ids
                ]
            },
        )
        session.add(candidate)
        session.flush()
        return candidate.id, character_ids


def test_both_bound_characters_enter_one_analysis_request(container, project) -> None:  # type: ignore[no-untyped-def]
    candidate_id, character_ids = _seed_two_hander(container, project)
    producer = _AcceptingProducer()
    tracker = _tracker(container, producer)
    tracker.enqueue_ready_candidates()
    result = tracker.dispatch_pending()

    assert result.dispatched == 1
    # ONE job for the candidate, with BOTH characters inside it - not two POSTs,
    # which the remote job_id dedup would answer `202 {duplicate: true}`.
    assert producer.submissions == [candidate_id]
    assert producer.characters == [character_ids]

    with container.database.session() as session:
        submission = session.scalar(
            select(CharacterEvidenceSubmission).where(
                CharacterEvidenceSubmission.candidate_id == candidate_id
            )
        )
        rows = {
            row.character_id: row
            for row in session.scalars(
                select(CharacterEvidenceCoverage).where(
                    CharacterEvidenceCoverage.candidate_id == candidate_id
                )
            )
        }
    assert submission.metadata_json["covered_character_ids"] == character_ids
    assert submission.metadata_json["uncovered_character_ids"] == []
    assert set(rows) == set(character_ids)
    for character_id in character_ids:
        row = rows[character_id]
        assert row.status == "REQUESTED"
        assert row.operating_mode == "SHADOW"
        # Each character's own references are recorded, not a shared list.
        assert len(row.reference_asset_ids) >= 1


def test_a_character_without_references_is_recorded_and_the_other_still_runs(
    container, project
) -> None:  # type: ignore[no-untyped-def]
    candidate_id, character_ids = _seed_two_hander(container, project, second_has_identity=False)
    producer = _AcceptingProducer()
    tracker = _tracker(container, producer)
    tracker.enqueue_ready_candidates()
    assert tracker.dispatch_pending().dispatched == 1

    assert producer.characters == [[character_ids[0]]]
    coverage = {item["character_id"]: item for item in tracker.coverage(candidate_id)}
    assert coverage[character_ids[0]]["status"] == "REQUESTED"
    assert coverage[character_ids[1]]["status"] == "SKIPPED"
    assert coverage[character_ids[1]]["skip_reason"] == "NO_CONFIRMED_IDENTITY_REFERENCES"
    # Partial coverage never becomes a gate: the analysis still ran.
    assert all(item["operating_mode"] == "SHADOW" for item in coverage.values())


def test_a_candidate_with_no_identities_at_all_is_skipped_whole(container, project) -> None:  # type: ignore[no-untyped-def]
    candidate_id, _ids = _seed_two_hander(container, project, second_has_identity=False)
    with container.database.session() as session:
        for character in session.scalars(select(Character)):
            character.current_identity_version_id = None
    tracker = _tracker(container, _AcceptingProducer())
    tracker.enqueue_ready_candidates()
    result = tracker.dispatch_pending()
    assert result.skipped == 1
    with container.database.session() as session:
        submission = session.scalar(
            select(CharacterEvidenceSubmission).where(
                CharacterEvidenceSubmission.candidate_id == candidate_id
            )
        )
    assert submission.status == "SKIPPED"
    assert submission.skip_reason == "NO_CONFIRMED_IDENTITY_REFERENCES"


def test_each_character_result_is_recorded_and_a_repeated_callback_changes_nothing(
    container, project
) -> None:  # type: ignore[no-untyped-def]
    candidate_id, character_ids = _seed_two_hander(container, project)
    tracker = _tracker(container, _AcceptingProducer())
    tracker.enqueue_ready_candidates()
    tracker.dispatch_pending()

    for index, character_id in enumerate(character_ids, 1):
        for _ in range(3):  # idempotent per (job, character)
            tracker.record_character_report(
                candidate_id,
                character_id=character_id,
                producer_run_id=f"run-{index}",
                decision="ABSTAIN",
                qa_result_id=f"qa-{index}",
                similarity={"face_similarity_p50": 0.5 + index / 10},
            )
    tracker.record_callback(candidate_id, status="SUCCEEDED", character_ids=character_ids)

    coverage = {item["character_id"]: item for item in tracker.coverage(candidate_id)}
    assert set(coverage) == set(character_ids)
    for index, character_id in enumerate(character_ids, 1):
        row = coverage[character_id]
        assert row["status"] == "REPORTED"
        assert row["producer_run_id"] == f"run-{index}"
        assert row["qa_result_id"] == f"qa-{index}"
        assert row["similarity"]["face_similarity_p50"] == pytest.approx(0.5 + index / 10)
        assert row["operating_mode"] == "SHADOW"
    with container.database.session() as session:
        rows = list(
            session.scalars(
                select(CharacterEvidenceCoverage).where(
                    CharacterEvidenceCoverage.candidate_id == candidate_id
                )
            )
        )
        submission = session.scalar(
            select(CharacterEvidenceSubmission).where(
                CharacterEvidenceSubmission.candidate_id == candidate_id
            )
        )
    assert len(rows) == len(character_ids)  # three callbacks, two rows
    assert submission.status == "REPORTED"
    assert submission.metadata_json["reported_character_ids"] == character_ids


def test_a_failed_job_marks_every_requested_character_failed(container, project) -> None:  # type: ignore[no-untyped-def]
    candidate_id, character_ids = _seed_two_hander(container, project)
    tracker = _tracker(container, _AcceptingProducer())
    tracker.enqueue_ready_candidates()
    tracker.dispatch_pending()
    tracker.record_callback(candidate_id, status="FAILED", error_code="GPU_OOM")

    coverage = {item["character_id"]: item for item in tracker.coverage(candidate_id)}
    assert {item["status"] for item in coverage.values()} == {"FAILED"}
    assert all(item["failure_reason"] == "GPU_OOM" for item in coverage.values())
    assert set(coverage) == set(character_ids)


def test_the_client_sends_one_request_carrying_every_character(container, project) -> None:  # type: ignore[no-untyped-def]
    """The wire contract itself, not just the dispatcher's intent."""

    import httpx
    from character_evidence.client import ModalCharacterEvidenceProducer
    from qa_core import CanonicalIdentityReference, CharacterSubmissionTarget

    candidate_id, character_ids = _seed_two_hander(container, project)
    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        output = session.get(MediaAsset, candidate.output_asset_id)
        video_path = output.local_path or output.storage_key
        references = {
            character_id: [
                CanonicalIdentityReference(
                    reference_asset_id=row.master_asset_id,
                    view="FRONT",
                    image_bytes=b"",
                    reference_asset_version="UNVERSIONED",
                )
                for row in session.scalars(
                    select(CharacterIdentityVersion).where(
                        CharacterIdentityVersion.character_id == character_id
                    )
                )
            ]
            for character_id in character_ids
        }

    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        sent.append(_json.loads(request.content))
        return httpx.Response(202, json={"job_id": candidate_id, "status": "ACCEPTED"})

    class _Media:
        def reference_url(self, asset_id, **_kwargs):  # type: ignore[no-untyped-def]
            return f"https://cdn.example.com/{asset_id}.bin"

    producer = ModalCharacterEvidenceProducer(
        container.database,
        _Media(),  # type: ignore[arg-type]
        base_url="https://modal.example.com",
        api_key="k" * 32,
        threshold_version="character-evidence-thresholds-2026-08-27-v1",
        transport=httpx.MockTransport(handler),
    )
    from pathlib import Path

    producer.submit(
        Path(video_path),
        candidate_id=candidate_id,
        characters=[
            CharacterSubmissionTarget(character_id, tuple(references[character_id]))
            for character_id in character_ids
        ],
    )
    assert len(sent) == 1
    payload = sent[0]
    # One job id (the candidate), and every character inside it.
    assert payload["job_id"] == candidate_id
    assert [item["character_id"] for item in payload["characters"]] == character_ids
    assert all(item["reference_assets"] for item in payload["characters"])


def test_the_client_refuses_a_duplicate_or_oversized_character_list(container, project) -> None:  # type: ignore[no-untyped-def]
    from character_evidence.client import MAX_CHARACTERS_PER_ANALYSIS, ModalCharacterEvidenceProducer
    from qa_core import CanonicalIdentityReference, CharacterSubmissionTarget

    reference = CanonicalIdentityReference(
        reference_asset_id="a", view="FRONT", image_bytes=b""
    )
    producer = ModalCharacterEvidenceProducer(
        container.database,
        object(),  # type: ignore[arg-type]
        base_url="https://modal.example.com",
        api_key="k" * 32,
        threshold_version="v",
    )
    from pathlib import Path

    with pytest.raises(ValueError, match="once in one analysis request"):
        producer.submit(
            Path("x"),
            candidate_id="c",
            characters=[
                CharacterSubmissionTarget("same", (reference,)),
                CharacterSubmissionTarget("same", (reference,)),
            ],
        )
    with pytest.raises(ValueError, match="at most"):
        producer.submit(
            Path("x"),
            candidate_id="c",
            characters=[
                CharacterSubmissionTarget(f"c{index}", (reference,))
                for index in range(MAX_CHARACTERS_PER_ANALYSIS + 1)
            ],
        )


def test_the_shadow_contract_is_unchanged(container, project) -> None:  # type: ignore[no-untyped-def]
    """Explicitly pinned: this workstream must not turn Modal into a gate."""

    candidate_id, character_ids = _seed_two_hander(container, project)
    tracker = _tracker(container, _AcceptingProducer())
    tracker.enqueue_ready_candidates()
    tracker.dispatch_pending()
    tracker.record_character_report(
        candidate_id,
        character_id=character_ids[0],
        producer_run_id="run-1",
        decision="FAIL",
        qa_result_id=None,
    )
    with container.database.session() as session:
        submission = session.scalar(
            select(CharacterEvidenceSubmission).where(
                CharacterEvidenceSubmission.candidate_id == candidate_id
            )
        )
        candidate = session.get(GenerationCandidate, candidate_id)
        rows = list(
            session.scalars(
                select(CharacterEvidenceCoverage).where(
                    CharacterEvidenceCoverage.candidate_id == candidate_id
                )
            )
        )
    assert submission.operating_mode == "SHADOW"
    assert {row.operating_mode for row in rows} == {"SHADOW"}
    # A FAIL from the shadow analysis leaves the candidate exactly where it was.
    assert candidate.status == "CREATED"
    assert candidate.committed_at is None if hasattr(candidate, "committed_at") else True


# ------------------------------------------------------------------ webhook
def _signed_callback(client, container, payload: dict[str, Any]):  # type: ignore[no-untyped-def]
    import json as _json

    from character_evidence.client import callback_signature

    raw = _json.dumps(payload).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = callback_signature(
        raw, timestamp, container.settings.character_evidence_callback_signing_key
    )
    return client.post(
        "/v1/webhooks/character-evidence",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-character-evidence-timestamp": timestamp,
            "x-character-evidence-signature": signature,
        },
    )


def _webhook_container(tmp_path):  # type: ignore[no-untyped-def]
    from platform_shared import Settings
    from video_platform_api.container import build_container

    return build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'evidence.db'}",
            storage_root=tmp_path / "media",
            public_base_url="http://testserver",
            auth_required=False,
            deployment_environment="test",
            character_evidence_callback_signing_key="k" * 48,
        )
    )


@pytest.fixture
def webhook_container(tmp_path):  # type: ignore[no-untyped-def]
    built = _webhook_container(tmp_path)
    try:
        yield built
    finally:
        built.database.engine.dispose()


#: The five roles `report_from_payload` requires, with a valid digest each.
_PROVENANCE_ROLES = (
    "person_detection",
    "multi_object_tracking",
    "face_detection",
    "face_identity",
    "appearance_encoding",
)


def _report_payloads(candidate_id: str, character_ids: list[str]) -> list[dict[str, Any]]:
    """Wire-shaped reports, one per character, as Modal would send them.

    Built from the real producer so the sample and threshold shapes are the
    ones the callback parser actually accepts, then completed with the model
    provenance the deployed pipeline attaches and the fixture producer does
    not.
    """

    from dataclasses import asdict

    from test_character_evidence import FIXTURE_VIDEO, _producer, _reference

    payloads: list[dict[str, Any]] = []
    for index, character_id in enumerate(character_ids, 1):
        report = _producer().produce(
            FIXTURE_VIDEO,
            candidate_id=candidate_id,
            character_id=character_id,
            references=[_reference()],
        )
        payload = asdict(report)
        threshold_version = payload["threshold_profile"]["version"]
        payload["producer_run_id"] = f"run-{index}-{character_id}"
        for sample in payload["samples"]:
            # The deployed pipeline sends a content-addressed reference
            # version; the offline fixture reference has none, and the callback
            # parser rightly refuses "UNVERSIONED" provenance.
            sample["reference_asset_version"] = "sha256:fixture-reference-v1"
        payload["model_manifest_version"] = "fixture-manifest-v1"
        payload["model_provenance"] = {
            role: {
                "sha256": hashlib.sha256(role.encode()).hexdigest(),
                "source_revision": "fixture",
                "threshold_version": threshold_version,
            }
            for role in _PROVENANCE_ROLES
        }
        payloads.append(payload)
    return payloads


def test_one_callback_records_every_character_and_leaves_the_candidate_alone(
    webhook_container,
):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient
    from production_domain.models import Project
    from video_platform_api.main import create_app

    container = webhook_container
    with container.database.session() as session:
        project = Project(title="Evidence project")
        session.add(project)
        session.flush()
        project_row = project

    candidate_id, character_ids = _seed_two_hander(container, project_row)
    tracker = _tracker(container, _AcceptingProducer())
    tracker.enqueue_ready_candidates()
    tracker.dispatch_pending()

    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        shot = session.get(Shot, candidate.shot_id)
        output = session.get(MediaAsset, candidate.output_asset_id)
        shot_id, project_id = shot.id, output.project_id
        before_status = candidate.status

    reports = _report_payloads(candidate_id, character_ids)
    payload = {
        "job_id": candidate_id,
        "project_id": project_id,
        "shot_id": shot_id,
        "status": "SUCCEEDED",
        "reports": reports,
    }
    with TestClient(create_app(container)) as client:
        response = _signed_callback(client, container, payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reports"] == 2
        assert body["characters"] == character_ids
        assert sorted(body["submitted_characters"]) == sorted(character_ids)

        # A replayed envelope changes nothing.
        replay = _signed_callback(client, container, payload)
        assert replay.status_code == 200
        assert replay.json()["characters"] == character_ids

    coverage = {item["character_id"]: item for item in tracker.coverage(candidate_id)}
    assert {item["status"] for item in coverage.values()} == {"REPORTED"}
    assert len({item["producer_run_id"] for item in coverage.values()}) == 2
    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        by_character = candidate.metadata_json["character_evidence_by_character"]
    # Both characters are described, not just the last report to arrive.
    assert set(by_character) == set(character_ids)
    # SHADOW: the candidate's own status is untouched.
    assert candidate.status == before_status
    assert candidate.metadata_json["character_evidence_mode"] == "SHADOW"


def test_a_report_for_a_character_this_candidate_never_submitted_is_refused(
    webhook_container,
):  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient
    from production_domain.models import Project
    from video_platform_api.main import create_app

    container = webhook_container
    with container.database.session() as session:
        project = Project(title="Evidence project")
        session.add(project)
        session.flush()
        project_row = project

    candidate_id, character_ids = _seed_two_hander(container, project_row)
    tracker = _tracker(container, _AcceptingProducer())
    tracker.enqueue_ready_candidates()
    tracker.dispatch_pending()
    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        shot = session.get(Shot, candidate.shot_id)
        output = session.get(MediaAsset, candidate.output_asset_id)
        shot_id, project_id = shot.id, output.project_id

    stranger = _report_payloads(candidate_id, ["some-other-character"])[0]
    payload = {
        "job_id": candidate_id,
        "project_id": project_id,
        "shot_id": shot_id,
        "status": "SUCCEEDED",
        "reports": [stranger],
    }
    with TestClient(create_app(container)) as client:
        response = _signed_callback(client, container, payload)
    assert response.status_code == 409, response.text
    assert "did not submit" in response.text
    assert all(item["status"] == "REQUESTED" for item in tracker.coverage(candidate_id))
    assert character_ids
