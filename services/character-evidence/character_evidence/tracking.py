"""Durable lifecycle for shadow Character Evidence jobs.

Three loops close the gap between "the producer exists" and "production uses
it, and notices when it goes quiet":

1. ``enqueue_ready_candidates`` — the explicit SHADOW submit event. After a
   candidate's video output is registered, a submission row is created
   (idempotently, one per candidate) and a ``GenerationEvent`` records that
   shadow evidence was requested.
2. ``dispatch_pending`` — performs the actual authenticated POST through the
   configured producer, with a bounded retry budget. A 202 moves the row to
   ACCEPTED; acceptance is never evidence.
3. ``scan_accepted_timeouts`` — an ACCEPTED job whose signed callback never
   arrived within the deadline becomes RECONCILIATION_REQUIRED and waits for
   an operator; it is never silently forgotten and never retried into a
   duplicate GPU job.

Everything here is shadow-only observation: no path reads or changes a
candidate's QA gate, and the table's check constraint forbids recording any
other operating mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from platform_database import Database
from production_domain.models import (
    Character,
    CharacterEvidenceSubmission,
    CharacterIdentityVersion,
    GenerationCandidate,
    GenerationEvent,
    MediaAsset,
    Shot,
    utcnow,
)
from qa_core import CanonicalIdentityReference
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .client import CharacterEvidenceRemoteError

_IDENTITY_VIEWS: tuple[tuple[str, str], ...] = (
    ("front_asset_id", "FRONT"),
    ("three_quarter_left_asset_id", "THREE_QUARTER_LEFT"),
    ("three_quarter_right_asset_id", "THREE_QUARTER_RIGHT"),
    ("left_profile_asset_id", "LEFT_PROFILE"),
    ("right_profile_asset_id", "RIGHT_PROFILE"),
)


@dataclass(frozen=True)
class CharacterEvidenceSweepResult:
    enqueued: int = 0
    dispatched: int = 0
    skipped: int = 0
    retried: int = 0
    failed: int = 0
    timed_out: int = 0
    dispatcher_absent: bool = False
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enqueued": self.enqueued,
            "dispatched": self.dispatched,
            "skipped": self.skipped,
            "retried": self.retried,
            "failed": self.failed,
            "timed_out": self.timed_out,
            "dispatcher_absent": self.dispatcher_absent,
            "details": self.details,
        }


class CharacterEvidenceTracker:
    """Owns character_evidence_submissions; the QA pipeline performs the POST."""

    version = "character-evidence-tracker-v1"

    def __init__(
        self,
        database: Database,
        qa,  # type: ignore[no-untyped-def]  # QAPipeline; hinted loosely to avoid a package cycle
        *,
        threshold_version: str,
        callback_timeout_seconds: int = 1800,
        max_submission_attempts: int = 5,
        backfill_hours: int = 72,
    ):
        self.database = database
        self.qa = qa
        self.threshold_version = threshold_version
        self.callback_timeout_seconds = max(60, int(callback_timeout_seconds))
        self.max_submission_attempts = max(1, int(max_submission_attempts))
        self.backfill_hours = max(1, int(backfill_hours))

    # ---------------------------------------------------------------- enqueue
    def enqueue_ready_candidates(self, *, limit: int = 50) -> int:
        """One PENDING submission per candidate whose video output is registered.

        Pull-based on purpose: a row written in the completion hot path could
        be lost with the process, a scan cannot miss. The unique candidate key
        makes racing sweeps converge on one row.
        """

        cutoff = utcnow() - timedelta(hours=self.backfill_hours)
        enqueued = 0
        with self.database.session() as session:
            candidates = list(
                session.execute(
                    select(GenerationCandidate.id, MediaAsset.project_id, GenerationCandidate.shot_id)
                    .join(MediaAsset, MediaAsset.id == GenerationCandidate.output_asset_id)
                    .outerjoin(
                        CharacterEvidenceSubmission,
                        CharacterEvidenceSubmission.candidate_id == GenerationCandidate.id,
                    )
                    .where(
                        GenerationCandidate.output_asset_id.is_not(None),
                        GenerationCandidate.created_at >= cutoff,
                        MediaAsset.mime_type.like("video/%"),
                        CharacterEvidenceSubmission.id.is_(None),
                    )
                    .order_by(GenerationCandidate.created_at)
                    .limit(max(1, limit))
                ).all()
            )
        for candidate_id, project_id, shot_id in candidates:
            try:
                with self.database.session() as session:
                    session.add(
                        CharacterEvidenceSubmission(
                            project_id=project_id,
                            candidate_id=candidate_id,
                            shot_id=shot_id,
                            status="PENDING",
                            threshold_version=self.threshold_version,
                        )
                    )
                    candidate = session.get(GenerationCandidate, candidate_id)
                    if candidate is not None and candidate.generation_job_id:
                        session.add(
                            GenerationEvent(
                                generation_job_id=candidate.generation_job_id,
                                event_type="CHARACTER_EVIDENCE_SHADOW_ENQUEUED",
                                detail={
                                    "candidate_id": candidate_id,
                                    "operating_mode": "SHADOW",
                                },
                            )
                        )
                    session.flush()
                enqueued += 1
            except IntegrityError:
                # A concurrent sweep created the row first. That is the unique
                # key doing its job, not an error.
                continue
        return enqueued

    # --------------------------------------------------------------- dispatch
    def _identity_references(
        self, session, character_id: str  # type: ignore[no-untyped-def]
    ) -> list[CanonicalIdentityReference]:
        character = session.get(Character, character_id)
        if character is None or not character.current_identity_version_id:
            return []
        identity = session.get(CharacterIdentityVersion, character.current_identity_version_id)
        if identity is None:
            return []
        references: list[CanonicalIdentityReference] = []
        seen: set[str] = set()
        for column, view in _IDENTITY_VIEWS:
            asset_id = getattr(identity, column)
            if not asset_id or asset_id in seen:
                continue
            asset = session.get(MediaAsset, asset_id)
            if asset is None:
                continue
            seen.add(asset_id)
            references.append(
                CanonicalIdentityReference(
                    reference_asset_id=asset_id,
                    view=view,
                    image_bytes=b"",
                    reference_asset_version=asset.sha256,
                )
            )
        if not references and identity.master_asset_id:
            asset = session.get(MediaAsset, identity.master_asset_id)
            if asset is not None:
                references.append(
                    CanonicalIdentityReference(
                        reference_asset_id=identity.master_asset_id,
                        view="FRONT",
                        image_bytes=b"",
                        reference_asset_version=asset.sha256,
                    )
                )
        return references

    def _dispatch_target(
        self, session, submission: CharacterEvidenceSubmission  # type: ignore[no-untyped-def]
    ) -> tuple[str, list[CanonicalIdentityReference], list[str], str] | str:
        """The character, references and profile for one submission, or a skip reason."""

        candidate = session.get(GenerationCandidate, submission.candidate_id)
        if candidate is None or candidate.output_asset_id is None:
            return "CANDIDATE_OUTPUT_MISSING"
        bound_character_ids = [
            str(entry.get("character_id"))
            for entry in candidate.metadata_json.get("character_state_context", [])
            if entry.get("character_id")
        ]
        if not bound_character_ids:
            return "NO_CHARACTER_BINDINGS"
        shot = session.get(Shot, candidate.shot_id)
        profile = shot.shot_type if shot and shot.shot_type in {"DIALOGUE", "ACTION"} else "DIALOGUE"
        for character_id in bound_character_ids:
            references = self._identity_references(session, character_id)
            if references:
                uncovered = [item for item in bound_character_ids if item != character_id]
                return character_id, references, uncovered, profile
        return "NO_CONFIRMED_IDENTITY_REFERENCES"

    def dispatch_pending(self, *, limit: int = 20) -> CharacterEvidenceSweepResult:
        """POST each PENDING submission once, through the configured producer.

        Only PENDING rows dispatch, so an ACCEPTED job is never re-posted —
        the same candidate_id cannot start a second GPU job from this side.
        Remote failures burn one attempt from a bounded budget and stay
        PENDING until the budget is spent, then become FAILED loudly.
        """

        if getattr(self.qa.evidence_producer, "submit", None) is None:
            # No producer configured (CHARACTER_EVIDENCE_ENABLED off, or a
            # non-production environment). PENDING rows stay visible instead
            # of being silently skipped: an operator can see the backlog.
            return CharacterEvidenceSweepResult(dispatcher_absent=True)
        dispatched = skipped = retried = failed = 0
        details: list[dict[str, Any]] = []
        with self.database.session() as session:
            pending_ids = [
                row_id
                for row_id in session.scalars(
                    select(CharacterEvidenceSubmission.id)
                    .where(CharacterEvidenceSubmission.status == "PENDING")
                    .order_by(CharacterEvidenceSubmission.created_at)
                    .limit(max(1, limit))
                )
            ]
        for submission_id in pending_ids:
            with self.database.session() as session:
                submission = session.get(CharacterEvidenceSubmission, submission_id)
                if submission is None or submission.status != "PENDING":
                    continue
                target = self._dispatch_target(session, submission)
                if isinstance(target, str):
                    submission.status = "SKIPPED"
                    submission.skip_reason = target
                    session.flush()
                    skipped += 1
                    details.append({"submission_id": submission_id, "skipped": target})
                    continue
                character_id, references, uncovered, profile = target
                candidate_id = submission.candidate_id
            try:
                self.qa.submit_character_evidence(
                    candidate_id,
                    character_id=character_id,
                    references=references,
                    profile=profile,
                )
            except (CharacterEvidenceRemoteError, LookupError, ValueError) as exc:
                with self.database.session() as session:
                    submission = session.get(CharacterEvidenceSubmission, submission_id)
                    if submission is None:
                        continue
                    submission.submission_count += 1
                    submission.last_submitted_at = utcnow()
                    if submission.first_submitted_at is None:
                        submission.first_submitted_at = submission.last_submitted_at
                    submission.error_code = type(exc).__name__[:120]
                    submission.error_message = str(exc)[:500]
                    if submission.submission_count >= self.max_submission_attempts:
                        submission.status = "FAILED"
                        failed += 1
                    else:
                        retried += 1
                    session.flush()
                details.append({"submission_id": submission_id, "error": type(exc).__name__})
                continue
            with self.database.session() as session:
                submission = session.get(CharacterEvidenceSubmission, submission_id)
                if submission is None:
                    continue
                now = utcnow()
                submission.status = "ACCEPTED"
                submission.character_id = character_id
                submission.submission_count += 1
                submission.last_submitted_at = now
                if submission.first_submitted_at is None:
                    submission.first_submitted_at = now
                submission.accepted_at = now
                submission.error_code = None
                submission.error_message = None
                submission.metadata_json = {
                    **submission.metadata_json,
                    "covered_character_id": character_id,
                    "uncovered_character_ids": uncovered,
                    "profile": profile,
                }
                candidate = session.get(GenerationCandidate, submission.candidate_id)
                if candidate is not None and candidate.generation_job_id:
                    session.add(
                        GenerationEvent(
                            generation_job_id=candidate.generation_job_id,
                            event_type="CHARACTER_EVIDENCE_SHADOW_SUBMITTED",
                            detail={
                                "candidate_id": submission.candidate_id,
                                "character_id": character_id,
                                "operating_mode": "SHADOW",
                                "submission_count": submission.submission_count,
                            },
                        )
                    )
                session.flush()
            dispatched += 1
        return CharacterEvidenceSweepResult(
            dispatched=dispatched,
            skipped=skipped,
            retried=retried,
            failed=failed,
            details=details,
        )

    # ---------------------------------------------------------------- timeout
    def scan_accepted_timeouts(self, *, limit: int = 50) -> int:
        """ACCEPTED with no callback past the deadline → RECONCILIATION_REQUIRED."""

        deadline = utcnow() - timedelta(seconds=self.callback_timeout_seconds)
        timed_out = 0
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(CharacterEvidenceSubmission)
                    .where(
                        CharacterEvidenceSubmission.status == "ACCEPTED",
                        CharacterEvidenceSubmission.accepted_at <= deadline,
                    )
                    .order_by(CharacterEvidenceSubmission.accepted_at)
                    .limit(max(1, limit))
                )
            )
            for submission in rows:
                submission.status = "RECONCILIATION_REQUIRED"
                submission.reconciliation_note = (
                    f"accepted at {submission.accepted_at.isoformat()} and no signed "
                    f"callback arrived within {self.callback_timeout_seconds}s"
                )
                timed_out += 1
            session.flush()
        return timed_out

    # --------------------------------------------------------------- callback
    def record_callback(self, candidate_id: str, *, status: str, error_code: str | None = None) -> None:
        """Reflect a verified callback onto the submission row.

        Tolerates an absent row (a submission made before this table existed);
        never creates one, because a callback for a job this table never
        dispatched is already rejected upstream by the lineage checks.
        """

        with self.database.session() as session:
            submission = session.scalar(
                select(CharacterEvidenceSubmission).where(
                    CharacterEvidenceSubmission.candidate_id == candidate_id
                )
            )
            if submission is None:
                return
            now = utcnow()
            submission.last_callback_at = now
            if status == "SUCCEEDED":
                submission.status = "REPORTED"
                submission.reported_at = now
                submission.error_code = None
                submission.error_message = None
            else:
                submission.status = "FAILED"
                submission.error_code = (error_code or "REMOTE_FAILURE")[:120]
            session.flush()

    # ----------------------------------------------------------- reconcile
    def resolve_reconciliation(
        self,
        submission_id: str,
        *,
        action: str,
        note: str,
        resolved_by: str,
    ) -> CharacterEvidenceSubmission:
        """Operator resolution of a timed-out acceptance.

        ``RESUBMIT`` re-queues exactly one new dispatch attempt (the remote
        side deduplicates by candidate_id, so a still-running job is not
        doubled); ``MARK_FAILED`` closes it. Nothing here is automatic.
        """

        if action not in {"RESUBMIT", "MARK_FAILED"}:
            raise ValueError("reconciliation action must be RESUBMIT or MARK_FAILED")
        if not note.strip():
            raise ValueError("a reconciliation note is required")
        with self.database.session() as session:
            submission = session.get(CharacterEvidenceSubmission, submission_id)
            if submission is None:
                raise LookupError("character evidence submission not found")
            if submission.status != "RECONCILIATION_REQUIRED":
                raise ValueError(
                    f"submission is {submission.status}, not RECONCILIATION_REQUIRED"
                )
            submission.status = "PENDING" if action == "RESUBMIT" else "FAILED"
            if action == "MARK_FAILED":
                submission.error_code = "RECONCILED_FAILED"
            submission.reconciliation_note = note.strip()[:2000]
            submission.reconciled_at = utcnow()
            submission.reconciled_by = resolved_by[:120]
            session.flush()
            session.refresh(submission)
            return submission

    # ------------------------------------------------------------------ views
    def list_submissions(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            query = select(CharacterEvidenceSubmission).order_by(
                CharacterEvidenceSubmission.updated_at.desc()
            )
            if status:
                query = query.where(CharacterEvidenceSubmission.status == status)
            return [
                {
                    "id": row.id,
                    "candidate_id": row.candidate_id,
                    "project_id": row.project_id,
                    "shot_id": row.shot_id,
                    "character_id": row.character_id,
                    "status": row.status,
                    "operating_mode": row.operating_mode,
                    "threshold_version": row.threshold_version,
                    "submission_count": row.submission_count,
                    "first_submitted_at": _iso(row.first_submitted_at),
                    "accepted_at": _iso(row.accepted_at),
                    "last_callback_at": _iso(row.last_callback_at),
                    "reported_at": _iso(row.reported_at),
                    "error_code": row.error_code,
                    "skip_reason": row.skip_reason,
                    "reconciliation_note": row.reconciliation_note,
                    "reconciled_by": row.reconciled_by,
                    "metadata": row.metadata_json,
                }
                for row in session.scalars(query.limit(max(1, limit)))
            ]

    def sweep(self, *, limit: int = 50) -> CharacterEvidenceSweepResult:
        """Enqueue, dispatch and timeout-scan in one maintenance pass."""

        enqueued = self.enqueue_ready_candidates(limit=limit)
        dispatch = self.dispatch_pending(limit=limit)
        timed_out = self.scan_accepted_timeouts(limit=limit)
        return CharacterEvidenceSweepResult(
            enqueued=enqueued,
            dispatched=dispatch.dispatched,
            skipped=dispatch.skipped,
            retried=dispatch.retried,
            failed=dispatch.failed,
            timed_out=timed_out,
            dispatcher_absent=dispatch.dispatcher_absent,
            details=dispatch.details,
        )


def _iso(value) -> str | None:  # type: ignore[no-untyped-def]
    return value.isoformat() if value is not None else None


__all__ = ["CharacterEvidenceSweepResult", "CharacterEvidenceTracker"]
