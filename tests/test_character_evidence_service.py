from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from character_evidence.api import create_api
from character_evidence.client import (
    ANALYZE_PATH,
    CharacterEvidenceCallbackAuthenticationError,
    ModalCharacterEvidenceProducer,
    callback_signature,
    verify_callback,
)
from character_evidence.model_manifest import load_manifest
from character_evidence.schemas import AnalyzeRequest, CallbackEnvelope
from character_evidence.validation import METRIC_FIELDS, calculate_metrics, evaluate_promotion
from fastapi.testclient import TestClient
from pydantic import ValidationError
from qa_core import CanonicalIdentityReference

ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = ROOT / "services/character-evidence"
ACCEPTANCE = ROOT / "config/character-evidence/acceptance-criteria-v1.json"


def _request_payload() -> dict:
    return {
        "job_id": "candidate-1",
        "project_id": "project-1",
        "shot_id": "shot-1",
        "video_url": "https://media.example/video.mp4?signature=short-lived",
        "characters": [
            {
                "character_id": "character-1",
                "reference_assets": [
                    {
                        "asset_id": "reference-1",
                        "asset_version": "sha256:immutable-v1",
                        "url": "https://media.example/reference.png?signature=short-lived",
                        "view": "FRONT",
                    }
                ],
            }
        ],
        "threshold_version": "character-evidence-thresholds-2026-08-27-v1",
    }


def test_modal_api_has_one_authenticated_public_route(monkeypatch) -> None:
    spawned: list[dict] = []
    key = "character-evidence-test-api-key-32-bytes-A7z9"
    monkeypatch.setenv("CHARACTER_EVIDENCE_API_KEY", key)
    app = create_api(spawned.append)
    assert [(route.path, sorted(route.methods or [])) for route in app.routes] == [
        (ANALYZE_PATH, ["POST"])
    ]
    with TestClient(app) as client:
        assert client.post(ANALYZE_PATH, json=_request_payload()).status_code == 401
        response = client.post(
            ANALYZE_PATH,
            json=_request_payload(),
            headers={"Authorization": f"Bearer {key}"},
        )
    assert response.status_code == 202
    assert response.json() == {"job_id": "candidate-1", "status": "ACCEPTED", "duplicate": False}
    assert len(spawned) == 1
    assert spawned[0]["job_id"] == "candidate-1"
    assert spawned[0]["threshold_version"] == _request_payload()["threshold_version"]


def test_request_schema_rejects_non_https_and_unversioned_references() -> None:
    payload = _request_payload()
    payload["video_url"] = "http://media.example/video.mp4"
    with pytest.raises(ValidationError, match="credential-free HTTPS"):
        AnalyzeRequest.model_validate(payload)
    payload = _request_payload()
    payload["characters"][0]["reference_assets"][0]["asset_version"] = ""
    with pytest.raises(ValidationError):
        AnalyzeRequest.model_validate(payload)


def test_bestshiny_adapter_sends_only_url_contract_and_requires_exact_202() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(202, json={"job_id": "candidate-1", "status": "ACCEPTED"})

    class Media:
        def reference_url(self, asset_id: str, **kwargs) -> str:  # type: ignore[no-untyped-def]
            captured["media_call"] = (asset_id, kwargs)
            return "https://media.example/candidate.mp4?signature=short-lived"

    producer = ModalCharacterEvidenceProducer(
        object(),  # type: ignore[arg-type]
        Media(),  # type: ignore[arg-type]
        base_url="https://modal.example",
        api_key="character-evidence-test-api-key-32-bytes-A7z9",
        threshold_version="character-evidence-thresholds-2026-08-27-v1",
        transport=httpx.MockTransport(handler),
    )
    producer._submission_context = lambda candidate_id: (  # type: ignore[method-assign]
        "project-1",
        "shot-1",
        SimpleNamespace(id="video-asset-1"),
        Path("registered/video.mp4"),
    )
    submission = producer.submit(
        Path("registered/video.mp4"),
        candidate_id="candidate-1",
        character_id="character-1",
        references=[
            CanonicalIdentityReference(
                reference_asset_id="reference-1",
                view="FRONT",
                image_bytes=b"",
                reference_asset_version="reference-v7",
                source_url="https://media.example/reference.png?signature=short-lived",
            )
        ],
    )
    request = captured["request"]
    body = json.loads(request.content)
    assert request.url == "https://modal.example/v1/character-evidence/analyze"
    assert request.headers["authorization"].startswith("Bearer ")
    assert body["video_url"].startswith("https://")
    assert body["characters"][0]["reference_assets"][0]["asset_version"] == "reference-v7"
    assert "image_bytes" not in request.content.decode()
    assert submission.status == "ACCEPTED"


def test_signed_callback_rejects_tampering_and_replay_window() -> None:
    key = "character-evidence-test-callback-key-32-bytes-Z9y8"
    envelope = CallbackEnvelope(
        job_id="candidate-1",
        project_id="project-1",
        shot_id="shot-1",
        status="FAILED",
        error_code="MODEL_FAILURE",
        error_message="bounded failure",
    )
    raw = envelope.model_dump_json().encode()
    signature = callback_signature(raw, "1000", key)
    assert verify_callback(raw, timestamp="1000", signature=signature, signing_key=key, now=1001) == envelope
    with pytest.raises(CharacterEvidenceCallbackAuthenticationError):
        verify_callback(raw + b" ", timestamp="1000", signature=signature, signing_key=key, now=1001)
    with pytest.raises(CharacterEvidenceCallbackAuthenticationError, match="outside tolerance"):
        verify_callback(raw, timestamp="1000", signature=signature, signing_key=key, now=1400)


def test_model_manifest_is_exact_build_cached_and_license_traced() -> None:
    manifest = load_manifest(SERVICE_ROOT / "character_evidence_model_manifest.json")
    expected = {
        "person_detection": ("YOLOX-s", "0.1.1rc0"),
        "multi_object_tracking": ("ByteTrack", "d1bf0191"),
        "face_detection": ("YuNet", "2026may"),
        "face_identity": ("SFace", "2021dec"),
        "appearance_encoding": ("DINOv2-base", "dinov2_vitb14"),
    }
    assert {
        role: (entry["model_name"], entry["model_version"])
        for role, entry in manifest.by_role.items()
    } == expected
    assert all(entry["loaded_at_build"] for entry in manifest.by_role.values())
    assert all(entry["license"] in {"Apache-2.0", "MIT"} for entry in manifest.by_role.values())
    assert len({entry["sha256"] for entry in manifest.by_role.values()}) == 5


def _perfect_example(index: int, required_slices: list[str]) -> dict:
    same = index % 2 == 0
    face_present = index % 4 != 0
    return {
        "example_id": f"example-{index}",
        "media_asset_id": f"media-{index}",
        "media_sha256": f"{index:064x}",
        "consent_record_id": f"consent-{index}",
        "annotator_record_id": f"annotator-{index}",
        "slices": required_slices,
        "person_present": True,
        "person_detected": True,
        "face_present": face_present,
        "face_detected": face_present,
        "identity_same": same,
        "identity_decision": "MATCH" if same else "NON_MATCH",
        "tracking_opportunities": 1,
        "id_switches": 0,
        "ground_truth_tracks": 1,
        "predicted_fragments": 1,
        "appearance_same": same,
        "appearance_decision": "MATCH" if same else "NON_MATCH",
        "decision": "PASS" if same else "FAIL",
    }


def _dataset(examples: list[dict]) -> dict:
    return {
        "dataset_version": "character-evidence-validation-test-v1",
        "authorization_record": {
            "owner": "operator",
            "approved_at": "2026-08-30T00:00:00Z",
            "purpose": "CHARACTER_EVIDENCE_VALIDATION",
            "retention_policy": "delete after validation",
        },
        "examples": examples,
    }


def _approved_acceptance(tmp_path: Path, *, minimum: int = 4) -> Path:
    acceptance = json.loads(ACCEPTANCE.read_text())
    acceptance["validation_plan"] = {
        "status": "APPROVED",
        "minimum_authorized_examples": minimum,
        "minimum_examples_per_required_slice": minimum,
    }
    approved = tmp_path / "approved-acceptance.json"
    approved.write_text(json.dumps(acceptance))
    return approved


def test_validation_metrics_are_global_and_per_slice_and_unapproved_plan_cannot_promote(
    tmp_path: Path,
) -> None:
    acceptance = json.loads(ACCEPTANCE.read_text())
    empty = evaluate_promotion(_dataset([]), ACCEPTANCE)
    assert empty["eligible"] is False
    assert empty["failures"] == ["VALIDATION_PLAN_NOT_APPROVED"]
    examples = [_perfect_example(index, acceptance["required_slices"]) for index in range(4)]
    metrics = calculate_metrics(examples)
    assert METRIC_FIELDS <= metrics.keys()
    assert all(metrics[name] == 0 for name in METRIC_FIELDS)
    result = evaluate_promotion(_dataset(examples), _approved_acceptance(tmp_path))
    assert result["eligible"] is True
    assert set(result["per_slice"]) == set(acceptance["required_slices"])


def test_promotion_refuses_examples_without_consent_even_on_an_approved_plan(
    tmp_path: Path,
) -> None:
    acceptance = json.loads(ACCEPTANCE.read_text())
    examples = [_perfect_example(index, acceptance["required_slices"]) for index in range(4)]
    examples[2]["consent_record_id"] = ""
    result = evaluate_promotion(_dataset(examples), _approved_acceptance(tmp_path))
    assert result["eligible"] is False
    assert "EXAMPLE_CONSENT_MISSING:example-2" in result["failures"]
    assert result["global"] is None

    del examples[2]["consent_record_id"]
    result = evaluate_promotion(_dataset(examples), _approved_acceptance(tmp_path))
    assert result["eligible"] is False
    assert "EXAMPLE_INVALID:example-2:consent_record_id_missing" in result["failures"]


def test_promotion_refuses_a_dataset_without_an_authorization_record(tmp_path: Path) -> None:
    acceptance = json.loads(ACCEPTANCE.read_text())
    examples = [_perfect_example(index, acceptance["required_slices"]) for index in range(4)]
    dataset = _dataset(examples)
    del dataset["authorization_record"]
    result = evaluate_promotion(dataset, _approved_acceptance(tmp_path))
    assert result["eligible"] is False
    assert "DATASET_FIELD_MISSING:authorization_record" in result["failures"]

    dataset = _dataset(examples)
    dataset["authorization_record"]["purpose"] = "SOMETHING_ELSE"
    result = evaluate_promotion(dataset, _approved_acceptance(tmp_path))
    assert result["eligible"] is False
    assert "AUTHORIZATION_RECORD_INVALID:purpose" in result["failures"]

    # The old bare-examples calling convention is gone on purpose; a list is
    # not a dataset document and is refused outright, plan status regardless.
    bare = evaluate_promotion([], _approved_acceptance(tmp_path))  # type: ignore[arg-type]
    assert bare["eligible"] is False
    assert bare["failures"] == ["DATASET_NOT_AN_OBJECT"]


def test_dataset_validator_matches_the_published_schema() -> None:
    from character_evidence.validation import (
        AUTHORIZATION_REQUIRED_FIELDS,
        DATASET_REQUIRED_FIELDS,
        EXAMPLE_REQUIRED_FIELDS,
    )

    schema = json.loads(
        (SERVICE_ROOT / "validation" / "dataset.schema.json").read_text(encoding="utf-8")
    )
    assert list(DATASET_REQUIRED_FIELDS) == schema["required"]
    assert (
        list(AUTHORIZATION_REQUIRED_FIELDS)
        == schema["properties"]["authorization_record"]["required"]
    )
    assert list(EXAMPLE_REQUIRED_FIELDS) == schema["$defs"]["example"]["required"]


def _evidence_sample(track_id: int, *, face: float | None, appearance: float, time: float) -> object:
    from character_evidence.pipeline import EvidenceSample, ReferenceEmbedding

    return EvidenceSample(
        track_id=track_id,
        sample_time=time,
        face_similarity=face,
        appearance_similarity=appearance,
        face_visibility=0.9,
        detection_confidence=0.9,
        track_confidence=0.9,
        pose_yaw=0.0,
        blur_score=0.1,
        reference=ReferenceEmbedding("reference-1", "sha256:v1", "FRONT", None, object()),
    )


def test_track_selection_counts_id_switches_against_the_zero_budget() -> None:
    from character_evidence.pipeline import resolve_track_selection

    thresholds = json.loads(
        (ROOT / "config/character-evidence/thresholds-v1.json").read_text(encoding="utf-8")
    )
    assert thresholds["tracking"]["maximum_id_switches_for_decision"] == 0

    # One person, one track: no switch, unambiguous.
    single = {1: [_evidence_sample(1, face=0.8, appearance=0.9, time=0.0)]}
    chosen, ambiguous, switches = resolve_track_selection(single, thresholds)
    assert chosen is not None and chosen[0] == 1
    assert not ambiguous
    assert switches == 0

    # The person leaves frame and re-enters as a new track ID. The two tracks
    # score far enough apart that the old ambiguity margin never fired — this
    # is exactly the case that used to PASS on half the evidence.
    reappearing = {
        1: [_evidence_sample(1, face=0.9, appearance=0.95, time=0.0)],
        2: [_evidence_sample(2, face=0.55, appearance=0.60, time=3.0)],
    }
    chosen, ambiguous, switches = resolve_track_selection(reappearing, thresholds)
    assert chosen is not None and chosen[0] == 1
    assert not ambiguous
    assert switches == 1

    # A second, different person in frame (identity clearly failing, appearance
    # unremarkable) is not an identity switch of the tracked character.
    bystander = {
        1: [_evidence_sample(1, face=0.9, appearance=0.95, time=0.0)],
        2: [_evidence_sample(2, face=0.05, appearance=0.40, time=1.0)],
    }
    _, _, switches = resolve_track_selection(bystander, thresholds)
    assert switches == 0

    # No usable face on the second track, but a whole-body appearance match
    # above the appearance pass bar still attributes it: fail closed.
    faceless_match = {
        1: [_evidence_sample(1, face=0.9, appearance=0.95, time=0.0)],
        2: [_evidence_sample(2, face=None, appearance=0.85, time=2.0)],
    }
    _, _, switches = resolve_track_selection(faceless_match, thresholds)
    assert switches == 1


def test_modal_source_enforces_single_t4_worker_and_no_provider_fallback() -> None:
    source = (SERVICE_ROOT / "modal_app.py").read_text()
    assert source.count("modal.App(") == 1
    assert source.count("@modal.asgi_app()") == 1
    assert 'gpu="T4"' in source
    assert "min_containers=0" in source
    assert "max_containers=1" in source
    assert "scaledown_window=60" in source
    lowered = source.lower()
    assert "openrouter" not in lowered
    assert "volcano" not in lowered
    assert "aliyun" not in lowered
