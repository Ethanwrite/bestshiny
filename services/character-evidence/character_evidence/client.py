from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from media_service import MediaRegistry
from platform_database import Database
from production_domain.models import Episode, GenerationCandidate, MediaAsset, Scene, Shot
from qa_core import (
    CanonicalIdentityReference,
    CharacterEvidence,
    CharacterEvidenceAggregate,
    CharacterEvidenceReport,
    CharacterEvidenceSubmission,
    QAThresholdProfile,
)

from .schemas import AnalyzeRequest, CallbackEnvelope, CharacterInput, ReferenceAsset

ANALYZE_PATH = "/v1/character-evidence/analyze"


class CharacterEvidenceRemoteError(RuntimeError):
    pass


class CharacterEvidenceCallbackAuthenticationError(ValueError):
    pass


class CharacterEvidenceCallbackPayloadError(ValueError):
    pass


def _https_url(value: str, name: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{name} must not contain user info")
    return value.strip()


class ModalCharacterEvidenceProducer:
    """Infrastructure adapter; no Modal SDK crosses into the BestShiny domain."""

    version = "modal-character-evidence-producer-2026-08-27-v1"

    def __init__(
        self,
        database: Database,
        media: MediaRegistry,
        *,
        base_url: str,
        api_key: str,
        threshold_version: str,
        timeout_seconds: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.database = database
        self.media = media
        self.base_url = _https_url(base_url, "CHARACTER_EVIDENCE_BASE_URL").rstrip("/")
        if len(api_key.encode("utf-8")) < 32:
            raise ValueError("CHARACTER_EVIDENCE_API_KEY must contain at least 32 bytes")
        if not threshold_version.strip():
            raise ValueError("character evidence threshold version is required")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("character evidence HTTP timeout must be positive")
        self.api_key = api_key
        self.threshold_version = threshold_version.strip()
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def _submission_context(self, candidate_id: str) -> tuple[str, str, MediaAsset, Path]:
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if candidate is None or candidate.output_asset_id is None:
                raise LookupError("candidate output is not available")
            shot = session.get(Shot, candidate.shot_id)
            scene = session.get(Scene, shot.scene_id) if shot else None
            episode = session.get(Episode, scene.episode_id) if scene else None
            asset = session.get(MediaAsset, candidate.output_asset_id)
            if shot is None or episode is None or asset is None:
                raise LookupError("candidate production context is incomplete")
            video_path = Path(asset.local_path) if asset.local_path else Path(asset.storage_key)
            return episode.project_id, shot.id, asset, video_path

    def _reference_url(self, reference: CanonicalIdentityReference, project_id: str) -> tuple[str, str]:
        if reference.source_url:
            url = _https_url(reference.source_url, "character reference URL")
            version = reference.reference_asset_version.strip()
            if not version or version == "UNVERSIONED":
                raise ValueError("URL-backed character references require an explicit asset version")
            return url, version
        with self.database.session() as session:
            asset = session.get(MediaAsset, reference.reference_asset_id)
            if asset is None or asset.project_id != project_id:
                raise LookupError("character reference asset is not in the candidate project")
            immutable_version = (
                reference.reference_asset_version.strip()
                if reference.reference_asset_version.strip() not in {"", "UNVERSIONED"}
                else asset.sha256
            )
        return (
            self.media.reference_url(
                reference.reference_asset_id,
                project_id=project_id,
                provider="modal_character_evidence",
                require_https=True,
            ),
            immutable_version,
        )

    def submit(
        self,
        video_path: Path,
        *,
        candidate_id: str,
        character_id: str,
        references: Sequence[CanonicalIdentityReference],
        shot_type: str = "DIALOGUE",
        sample_positions: tuple[float, ...] | None = None,
    ) -> CharacterEvidenceSubmission:
        if not references:
            raise ValueError("canonical identity references are required")
        project_id, shot_id, output_asset, registered_path = self._submission_context(candidate_id)
        # A mismatched local path is a caller/data-lineage bug. It is never an
        # excuse to upload arbitrary bytes or run a local inference fallback.
        if Path(video_path) != registered_path:
            raise ValueError("candidate evidence path does not match its registered output asset")
        video_url = self.media.reference_url(
            output_asset.id,
            project_id=project_id,
            provider="modal_character_evidence",
            require_https=True,
        )
        reference_assets: list[ReferenceAsset] = []
        for reference in references:
            url, version = self._reference_url(reference, project_id)
            reference_assets.append(
                ReferenceAsset.model_validate(
                    {
                        "asset_id": reference.reference_asset_id,
                        "asset_version": version,
                        "url": url,
                        "view": reference.view,
                    }
                )
            )
        request = AnalyzeRequest(
            # Stable idempotency identity and direct callback lookup. A repeated
            # submission for one candidate replaces no evidence and cannot make
            # a 202 response look like a completed result.
            job_id=candidate_id,
            project_id=project_id,
            shot_id=shot_id,
            video_url=video_url,
            characters=[
                CharacterInput(character_id=character_id, reference_assets=reference_assets)
            ],
            threshold_version=self.threshold_version,
            shot_type=shot_type,
            sample_positions=list(sample_positions) if sample_positions else None,
        )
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    f"{self.base_url}{ANALYZE_PATH}",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    content=request.model_dump_json(),
                )
        except httpx.HTTPError as exc:
            raise CharacterEvidenceRemoteError("Modal character evidence is unavailable") from exc
        if response.status_code != 202:
            raise CharacterEvidenceRemoteError(
                f"Modal character evidence rejected the job with HTTP {response.status_code}"
            )
        try:
            accepted = response.json()
        except json.JSONDecodeError as exc:
            raise CharacterEvidenceRemoteError("Modal returned an invalid acceptance response") from exc
        if accepted.get("job_id") != candidate_id or accepted.get("status") != "ACCEPTED":
            raise CharacterEvidenceRemoteError("Modal returned a mismatched acceptance response")
        return CharacterEvidenceSubmission(
            job_id=candidate_id,
            candidate_id=candidate_id,
            status="ACCEPTED",
            submitted_at=datetime.now(UTC).isoformat(),
        )


def callback_signature(raw_body: bytes, timestamp: str, signing_key: str) -> str:
    digest = hmac.new(
        signing_key.encode("utf-8"),
        timestamp.encode("ascii") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


def verify_callback(
    raw_body: bytes,
    *,
    timestamp: str | None,
    signature: str | None,
    signing_key: str,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> CallbackEnvelope:
    if not signing_key:
        raise CharacterEvidenceCallbackAuthenticationError("callback signing key is not configured")
    try:
        issued_at = int(timestamp or "")
    except ValueError as exc:
        raise CharacterEvidenceCallbackAuthenticationError("callback timestamp is invalid") from exc
    current = int(time.time()) if now is None else now
    if abs(current - issued_at) > tolerance_seconds:
        raise CharacterEvidenceCallbackAuthenticationError("callback timestamp is outside tolerance")
    expected = callback_signature(raw_body, str(issued_at), signing_key)
    if not signature or not hmac.compare_digest(signature, expected):
        raise CharacterEvidenceCallbackAuthenticationError("callback signature is invalid")
    try:
        return CallbackEnvelope.model_validate_json(raw_body)
    except ValueError as exc:
        raise CharacterEvidenceCallbackPayloadError("callback payload is invalid") from exc


def report_from_payload(payload: dict[str, Any]) -> CharacterEvidenceReport:
    try:
        if payload["decision"] not in {"PASS", "FAIL", "ABSTAIN"}:
            raise ValueError("unsupported character evidence decision")
        if payload["operating_mode"] != "SHADOW":
            raise ValueError("only shadow evidence is currently accepted")
        aggregate = CharacterEvidenceAggregate(**payload["aggregate"])
        threshold_payload = dict(payload["threshold_profile"])
        threshold_payload["visibility_range"] = tuple(threshold_payload["visibility_range"])
        threshold = QAThresholdProfile(**threshold_payload)
        samples = tuple(CharacterEvidence(**item) for item in payload["samples"])
        if any(
            sample.hair_similarity != "UNAVAILABLE"
            or sample.costume_similarity != "UNAVAILABLE"
            or sample.reference_asset_version in {"", "UNVERSIONED"}
            or sample.threshold_version != threshold.version
            for sample in samples
        ):
            raise ValueError("sample provenance or unavailable dimensions are invalid")
        provenance = dict(payload["model_provenance"])
        required_roles = {
            "person_detection",
            "multi_object_tracking",
            "face_detection",
            "face_identity",
            "appearance_encoding",
        }
        if set(provenance) != required_roles:
            raise ValueError("model provenance roles are incomplete")
        for entry in provenance.values():
            digest = str(entry.get("sha256", ""))
            if (
                len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or not entry.get("source_revision")
                or entry.get("threshold_version") != threshold.version
            ):
                raise ValueError("model provenance entry is invalid")
        return CharacterEvidenceReport(
            producer_run_id=payload["producer_run_id"],
            producer_version=payload["producer_version"],
            candidate_id=payload["candidate_id"],
            character_id=payload["character_id"],
            tracking_status=payload["tracking_status"],
            tracking_reason_codes=tuple(payload.get("tracking_reason_codes", [])),
            review_requirements=tuple(payload.get("review_requirements", [])),
            samples=samples,
            aggregate=aggregate,
            threshold_profile=threshold,
            pipeline_versions=dict(payload["pipeline_versions"]),
            decision=payload["decision"],
            operating_mode=payload["operating_mode"],
            model_manifest_version=payload["model_manifest_version"],
            model_provenance=provenance,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CharacterEvidenceCallbackPayloadError("character evidence report is invalid") from exc


__all__ = [
    "ANALYZE_PATH",
    "CharacterEvidenceCallbackAuthenticationError",
    "CharacterEvidenceCallbackPayloadError",
    "CharacterEvidenceRemoteError",
    "ModalCharacterEvidenceProducer",
    "callback_signature",
    "report_from_payload",
    "verify_callback",
]
