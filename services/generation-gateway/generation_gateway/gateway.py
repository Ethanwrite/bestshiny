from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from media_service import MediaRegistry
from platform_contracts import GenerationRequest
from platform_database import Database
from production_domain.models import (
    AssetType,
    BrowserWorker,
    CandidateStatus,
    GenerationCandidate,
    GenerationEvent,
    GenerationIdempotency,
    GenerationJob,
    JobStatus,
    MediaAsset,
    Project,
    ProviderAccount,
    ProviderProjectBinding,
    RetryCategory,
    Shot,
    ShotStatus,
    WorkerCommand,
    WorkerStatus,
    utcnow,
)
from production_engine import ShotContinuityService
from provider_sdk import GenerationProvider, ProviderError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from .providers import GenerationTargetError, ProviderRouter
from .retry import RetryPolicy
from .scheduler import AccountScheduler, NoAccountAvailable


class IdempotencyConflict(RuntimeError):
    pass


class UnsafeRetry(RuntimeError):
    pass


def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class GenerationGateway:
    def __init__(
        self,
        database: Database,
        providers: ProviderRouter,
        media: MediaRegistry,
        scheduler: AccountScheduler,
        continuity: ShotContinuityService | None = None,
        retry_policy: RetryPolicy | None = None,
        claim_lease_seconds: int = 300,
        poll_interval_seconds: float = 2.0,
    ):
        if claim_lease_seconds < 30:
            raise ValueError("claim_lease_seconds must be at least 30")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        self.database = database
        self.providers = providers
        self.media = media
        self.scheduler = scheduler
        self.continuity = continuity
        self.retry_policy = retry_policy or RetryPolicy()
        self.claim_lease_seconds = claim_lease_seconds
        self.poll_interval_seconds = poll_interval_seconds

    def _next_poll_at(self) -> datetime:
        return utcnow() + timedelta(seconds=self.poll_interval_seconds)

    @staticmethod
    def _event(session, job_id: str, event_type: str, **detail: Any) -> None:  # type: ignore[no-untyped-def]
        session.add(GenerationEvent(generation_job_id=job_id, event_type=event_type, detail=detail))

    @staticmethod
    def _updated_one_row(result: Any) -> bool:
        return int(getattr(result, "rowcount", 0)) == 1

    def create(
        self,
        request: GenerationRequest,
        *,
        on_create: Callable[[Any, GenerationJob, bool], None] | None = None,
    ) -> tuple[GenerationJob, bool]:
        attempts = 6 if self.database.engine.dialect.name == "sqlite" else 1
        for attempt in range(attempts):
            try:
                return self._create_once(request, on_create=on_create)
            except OperationalError as exc:
                locked = "database is locked" in str(exc).lower()
                if not locked or attempt == attempts - 1:
                    raise
                time.sleep(min(0.02 * (2**attempt), 0.2))
        raise RuntimeError("generation create retry loop exhausted")

    def _create_once(
        self,
        request: GenerationRequest,
        *,
        on_create: Callable[[Any, GenerationJob, bool], None] | None = None,
    ) -> tuple[GenerationJob, bool]:
        self.providers.validate_target(request.provider, request.model, request.type)
        payload = request.model_dump(mode="json", exclude={"idempotency_key", "candidate_id"})
        request_hash = canonical_hash(payload)
        with self.database.session() as session:

            def replay(existing: GenerationIdempotency) -> tuple[GenerationJob, bool]:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict("idempotency key already belongs to a different request")
                replayed_job = session.get(GenerationJob, existing.generation_job_id)
                if not replayed_job:
                    raise LookupError("idempotency record points to a missing generation job")
                if on_create:
                    on_create(session, replayed_job, True)
                session.flush()
                return replayed_job, True

            if session.get(Project, request.project_id) is None:
                raise LookupError(f"project not found: {request.project_id}")
            requested_asset_ids = list(
                dict.fromkeys(
                    asset_id
                    for asset_id in (
                        request.start_frame_asset_id,
                        request.end_frame_asset_id,
                        *request.reference_asset_ids,
                    )
                    if asset_id
                )
            )
            for asset_id in requested_asset_ids:
                asset = session.get(MediaAsset, asset_id)
                if asset is None:
                    raise LookupError(f"media asset not found: {asset_id}")
                if asset.project_id != request.project_id:
                    raise LookupError("media asset does not belong to the generation project")
            existing = session.scalar(
                select(GenerationIdempotency).where(
                    GenerationIdempotency.project_id == request.project_id,
                    GenerationIdempotency.key == request.idempotency_key,
                )
            )
            if existing:
                return replay(existing)
            try:
                with session.begin_nested():
                    job = GenerationJob(
                        id=str(uuid.uuid4()),
                        project_id=request.project_id,
                        shot_id=request.shot_id,
                        candidate_id=request.candidate_id,
                        generation_type=request.type,
                        provider=request.provider,
                        model=request.model,
                        priority=request.priority,
                        request_json=payload,
                        request_hash=request_hash,
                        policy=request.generation_policy,
                        cost_estimate=request.cost_estimate,
                    )
                    shot = None
                    if request.shot_id:
                        shot = session.get(Shot, request.shot_id)
                        if not shot or shot.scene.episode.project_id != request.project_id:
                            raise LookupError("shot does not belong to project")
                    if on_create:
                        on_create(session, job, False)
                    candidate = None
                    if request.candidate_id:
                        with session.no_autoflush:
                            candidate = session.get(GenerationCandidate, request.candidate_id)
                        if not candidate or candidate.shot_id != request.shot_id:
                            raise LookupError("candidate does not belong to shot")
                    session.add(job)
                    session.flush([job])
                    session.add(
                        GenerationIdempotency(
                            project_id=request.project_id,
                            key=request.idempotency_key,
                            request_hash=request_hash,
                            generation_job_id=job.id,
                        )
                    )
                    self._event(
                        session,
                        job.id,
                        "JOB_CREATED",
                        idempotency_key=request.idempotency_key,
                        request_hash=request_hash,
                    )
                    if shot:
                        shot.generation_job_id = job.id
                        shot.status = ShotStatus.QUEUED.value
                    if candidate:
                        candidate.generation_job_id = job.id
                        candidate.status = CandidateStatus.GENERATING.value
                    session.flush()
            except IntegrityError:
                concurrent = session.scalar(
                    select(GenerationIdempotency).where(
                        GenerationIdempotency.project_id == request.project_id,
                        GenerationIdempotency.key == request.idempotency_key,
                    )
                )
                if concurrent:
                    return replay(concurrent)
                raise
            return job, False

    def get(self, job_id: str) -> GenerationJob | None:
        with self.database.session() as session:
            return session.get(GenerationJob, job_id)

    def events(self, job_id: str) -> list[GenerationEvent]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(GenerationEvent)
                    .where(GenerationEvent.generation_job_id == job_id)
                    .order_by(GenerationEvent.created_at)
                )
            )

    def retry(self, job_id: str) -> GenerationJob:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                raise LookupError("generation job not found")
            if job.status == JobStatus.COMPLETED.value:
                return job
            if not job.safe_to_retry or job.submission_state == "SENT_UNCONFIRMED":
                raise UnsafeRetry(
                    "provider submission may already have consumed credits; reconcile it before retrying"
                )
            job.status = JobStatus.RETRY_WAIT.value
            job.next_retry_at = utcnow()
            job.error_code = None
            job.error_message = None
            job.claim_token = None
            job.claim_expires_at = None
            job.submission_state = "NOT_SENT"
            self._event(session, job.id, "JOB_RETRY_REQUESTED")
            session.flush()
            return job

    async def cancel(self, job_id: str) -> GenerationJob:
        """Cancel safely without dropping capacity while a remote job may still run."""

        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                raise LookupError("generation job not found")
            if job.status in {
                JobStatus.COMPLETED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
            }:
                return job
            has_confirmed_remote_job = bool(job.provider_job_id) and job.submission_state == "CONFIRMED"
            if not has_confirmed_remote_job:
                if job.submission_state == "SENT_UNCONFIRMED":
                    # The provider may have consumed credits even though its job id was lost.
                    # Reconciliation is required before cancellation can be confirmed safely.
                    return job
                job.status = JobStatus.CANCELLED.value
                job.next_retry_at = None
                job.claim_token = None
                job.claim_expires_at = None
                self.scheduler.release_job_in_session(
                    session,
                    job.id,
                    success=None,
                    clear_routing=True,
                )
                self._event(session, job.id, "JOB_CANCELLED", remote_job=False)
                session.flush()
                return job

        claim_token = self._claim_for_cancellation(job_id)
        if claim_token is None:
            current = self.get(job_id)
            if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                raise LookupError("generation job not found")
            return current

        with self.database.session() as session:
            claimed = session.scalar(
                select(GenerationJob).where(
                    GenerationJob.id == job_id,
                    GenerationJob.status == JobStatus.RESERVED.value,
                    GenerationJob.claim_token == claim_token,
                )
            )
            if claimed is None:
                current = None
            else:
                provider_name = claimed.provider
                model = claimed.model
                capability = claimed.generation_type
                provider_job_id = claimed.provider_job_id
                account_id = claimed.account_id
                worker_id = claimed.worker_id
                current = claimed
        if current is None:
            latest = self.get(job_id)
            if latest is None:  # pragma: no cover - deleted concurrently by an administrator.
                raise LookupError("generation job not found")
            return latest
        if not all([provider_job_id, account_id, worker_id]):
            return self._restore_cancel_tracking(
                job_id,
                claim_token,
                "confirmed provider job is missing routing data",
            )
        try:
            provider = self.providers.validate_target(provider_name, model, capability)
            cancelled = await provider.cancel_job(
                provider_job_id,
                account_id=account_id,
                worker_id=worker_id,
            )
        except Exception as exc:
            return self._restore_cancel_tracking(job_id, claim_token, str(exc))
        if not cancelled:
            return self._restore_cancel_tracking(
                job_id,
                claim_token,
                "provider did not confirm cancellation",
            )

        with self.database.session() as session:
            job = session.scalar(
                select(GenerationJob).where(
                    GenerationJob.id == job_id,
                    GenerationJob.status == JobStatus.RESERVED.value,
                    GenerationJob.claim_token == claim_token,
                )
            )
            if job is None:
                current = None
            else:
                job.status = JobStatus.CANCELLED.value
                job.next_retry_at = None
                job.claim_token = None
                job.claim_expires_at = None
                job.error_code = None
                job.error_message = None
                self.scheduler.release_job_in_session(session, job.id, success=None)
                self._event(
                    session,
                    job.id,
                    "JOB_CANCELLED",
                    remote_job=True,
                    provider_job_id=provider_job_id,
                )
                session.flush()
                current = job
        if current is not None:
            return current
        latest = self.get(job_id)
        if latest is None:  # pragma: no cover - deleted concurrently by an administrator.
            raise LookupError("generation job not found")
        return latest

    def _claim_for_cancellation(self, job_id: str) -> str | None:
        """Fence cancellation against provider polling and completion finalization."""

        now = utcnow()
        token = uuid.uuid4().hex
        expires_at = now + timedelta(seconds=self.claim_lease_seconds)
        stale_reservation = and_(
            GenerationJob.status == JobStatus.RESERVED.value,
            or_(
                GenerationJob.claim_expires_at.is_(None),
                GenerationJob.claim_expires_at <= now,
            ),
        )
        claimable = or_(
            GenerationJob.status.in_(
                [
                    JobStatus.SUBMITTED.value,
                    JobStatus.RUNNING.value,
                    JobStatus.RETRY_WAIT.value,
                    JobStatus.WORKER_NEEDS_USER_ACTION.value,
                ]
            ),
            stale_reservation,
        )
        with self.database.session() as session:
            result = session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job_id,
                    claimable,
                    GenerationJob.provider_job_id.is_not(None),
                    GenerationJob.submission_state == "CONFIRMED",
                    GenerationJob.output_asset_id.is_(None),
                )
                .values(
                    status=JobStatus.RESERVED.value,
                    claim_token=token,
                    claim_expires_at=expires_at,
                )
            )
            if not self._updated_one_row(result):
                return None
            self._event(
                session,
                job_id,
                "JOB_CANCEL_CLAIMED",
                lease_expires_at=expires_at.isoformat(),
            )
        return token

    def _restore_cancel_tracking(
        self,
        job_id: str,
        claim_token: str,
        error: str,
    ) -> GenerationJob:
        with self.database.session() as session:
            result = session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job_id,
                    GenerationJob.status == JobStatus.RESERVED.value,
                    GenerationJob.claim_token == claim_token,
                )
                .values(
                    status=JobStatus.SUBMITTED.value,
                    claim_token=None,
                    claim_expires_at=None,
                    error_code="PROVIDER_CANCEL_UNCONFIRMED",
                    error_message=error[:4000],
                )
            )
            if self._updated_one_row(result):
                self._event(session, job_id, "PROVIDER_CANCEL_UNCONFIRMED", error=error)
        current = self.get(job_id)
        if current is None:  # pragma: no cover - deleted concurrently by an administrator.
            raise LookupError("generation job not found")
        return current

    def reconcile(self, job_id: str) -> GenerationJob:
        """Recover a late browser response without issuing another paid request."""
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                raise LookupError("generation job not found")
            if not (
                job.status == JobStatus.WORKER_NEEDS_USER_ACTION.value
                and job.submission_state == "SENT_UNCONFIRMED"
                and job.output_asset_id is None
            ):
                return job
            if job.provider_job_id:
                job.status = JobStatus.SUBMITTED.value
                job.submission_state = "CONFIRMED"
                job.safe_to_retry = False
                job.next_retry_at = self._next_poll_at()
                job.claim_token = None
                job.claim_expires_at = None
                return job
            command = session.scalar(
                select(WorkerCommand)
                .where(
                    WorkerCommand.generation_job_id == job.id,
                    WorkerCommand.message_type == "provider.request",
                    WorkerCommand.status == "COMPLETED",
                )
                .order_by(WorkerCommand.completed_at.desc())
            )
            response = command.response if command else None
            data = response.get("data", {}) if isinstance(response, dict) else {}
            media = data.get("media", []) if isinstance(data, dict) else []
            provider_job_id = next(
                (
                    str(item.get("name") or item.get("mediaId"))
                    for item in media
                    if item.get("name") or item.get("mediaId")
                ),
                None,
            )
            if not provider_job_id:
                return job
            job.provider_job_id = provider_job_id
            job.submission_state = "CONFIRMED"
            job.status = JobStatus.SUBMITTED.value
            job.safe_to_retry = False
            job.next_retry_at = self._next_poll_at()
            job.claim_token = None
            job.claim_expires_at = None
            idem = session.scalar(
                select(GenerationIdempotency).where(GenerationIdempotency.generation_job_id == job.id)
            )
            if idem:
                idem.provider_job_id = provider_job_id
            self._event(session, job.id, "ORPHAN_RESPONSE_RECOVERED", provider_job_id=provider_job_id)
            session.flush()
            return job

    async def _resolve_assets(
        self,
        job: GenerationJob,
        request: dict[str, Any],
        provider: GenerationProvider,
        *,
        provider_project_id: str | None,
    ) -> dict[str, Any]:
        result = dict(request)
        pairs = [
            ("start_frame_asset_id", "start_frame_provider_media_id"),
            ("end_frame_asset_id", "end_frame_provider_media_id"),
        ]
        for source, target in pairs:
            if request.get(source):
                media_id, reused = await self.media.resolve_provider_media(
                    request[source],
                    provider,
                    project_id=job.project_id,
                    account_id=job.account_id,
                    worker_id=job.worker_id,
                    provider_project_id=provider_project_id,
                )
                result[target] = media_id
                with self.database.session() as session:
                    self._event(
                        session,
                        job.id,
                        "ASSET_RESOLVED" if reused else "ASSET_UPLOADED",
                        asset_id=request[source],
                        provider_media_id=media_id,
                        reused=reused,
                    )
        provider_references = []
        for asset_id in request.get("reference_asset_ids") or []:
            media_id, reused = await self.media.resolve_provider_media(
                asset_id,
                provider,
                project_id=job.project_id,
                account_id=job.account_id,
                worker_id=job.worker_id,
                provider_project_id=provider_project_id,
            )
            provider_references.append(media_id)
            with self.database.session() as session:
                self._event(
                    session,
                    job.id,
                    "ASSET_RESOLVED" if reused else "ASSET_UPLOADED",
                    asset_id=asset_id,
                    provider_media_id=media_id,
                    reused=reused,
                )
        result["reference_provider_media_ids"] = provider_references
        result["_generation_job_id"] = job.id
        return result

    async def process(self, job_id: str) -> GenerationJob:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                raise LookupError("generation job not found")
            if job.status in {JobStatus.COMPLETED.value, JobStatus.CANCELLED.value}:
                return job
            status = job.status
        if job.provider_job_id and status in {
            JobStatus.RESERVED.value,
            JobStatus.SUBMITTED.value,
            JobStatus.RUNNING.value,
            JobStatus.RETRY_WAIT.value,
        }:
            claim_token = self._claim_for_polling(job_id)
            if claim_token is None:
                current = self.get(job_id)
                if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                    raise LookupError("generation job not found")
                return current
            return await self._poll(job_id, claim_token)
        if status in {
            JobStatus.NEW.value,
            JobStatus.RESERVED.value,
            JobStatus.QUEUED.value,
            JobStatus.RETRY_WAIT.value,
        }:
            claim_token = self._claim_for_submission(job_id)
            if claim_token is None:
                quarantined = self._quarantine_expired_uncertain_claim(job_id)
                if quarantined is not None:
                    return quarantined
                current = self.get(job_id)
                if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                    raise LookupError("generation job not found")
                return current
            return await self._submit(job_id, claim_token)
        return self.get(job_id)

    def _claim_for_submission(self, job_id: str) -> str | None:
        """Atomically acquire the only lease allowed to reach a paid provider call."""

        now = utcnow()
        token = uuid.uuid4().hex
        expires_at = now + timedelta(seconds=self.claim_lease_seconds)
        retry_due = or_(GenerationJob.next_retry_at.is_(None), GenerationJob.next_retry_at <= now)
        stale_reservation = and_(
            GenerationJob.status == JobStatus.RESERVED.value,
            or_(
                GenerationJob.claim_expires_at.is_(None),
                GenerationJob.claim_expires_at <= now,
            ),
        )
        claimable = or_(
            GenerationJob.status.in_([JobStatus.NEW.value, JobStatus.QUEUED.value]),
            and_(GenerationJob.status == JobStatus.RETRY_WAIT.value, retry_due),
            stale_reservation,
        )
        with self.database.session() as session:
            result = session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job_id,
                    claimable,
                    GenerationJob.provider_job_id.is_(None),
                    GenerationJob.submission_state == "NOT_SENT",
                    GenerationJob.safe_to_retry.is_(True),
                )
                .values(
                    status=JobStatus.RESERVED.value,
                    reserved_at=now,
                    claim_token=token,
                    claim_expires_at=expires_at,
                )
            )
            if not self._updated_one_row(result):
                return None
            self._event(
                session,
                job_id,
                "JOB_CLAIMED",
                lease_expires_at=expires_at.isoformat(),
            )
        return token

    def _quarantine_expired_uncertain_claim(self, job_id: str) -> GenerationJob | None:
        """Fail closed when a submitter disappears after crossing the paid-call boundary."""

        now = utcnow()
        with self.database.session() as session:
            result = session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job_id,
                    GenerationJob.status == JobStatus.RESERVED.value,
                    GenerationJob.claim_expires_at <= now,
                    GenerationJob.submission_state == "SENT_UNCONFIRMED",
                    GenerationJob.provider_job_id.is_(None),
                )
                .values(
                    status=JobStatus.WORKER_NEEDS_USER_ACTION.value,
                    safe_to_retry=False,
                    claim_token=None,
                    claim_expires_at=None,
                    error_code="SUBMISSION_CLAIM_EXPIRED",
                    error_message=(
                        "generation submitter disappeared after the paid-call boundary; "
                        "reconcile provider state before retrying"
                    ),
                )
            )
            if not self._updated_one_row(result):
                return None
            self._event(session, job_id, "SUBMISSION_CLAIM_EXPIRED", submitted=True)
        return self.get(job_id)

    def _claim_for_polling(self, job_id: str) -> str | None:
        """Atomically fence provider polling and completion finalization."""

        now = utcnow()
        token = uuid.uuid4().hex
        expires_at = now + timedelta(seconds=self.claim_lease_seconds)
        poll_due = or_(GenerationJob.next_retry_at.is_(None), GenerationJob.next_retry_at <= now)
        stale_reservation = and_(
            GenerationJob.status == JobStatus.RESERVED.value,
            or_(
                GenerationJob.claim_expires_at.is_(None),
                GenerationJob.claim_expires_at <= now,
            ),
        )
        claimable = or_(
            and_(
                GenerationJob.status.in_([JobStatus.SUBMITTED.value, JobStatus.RUNNING.value]),
                poll_due,
            ),
            and_(GenerationJob.status == JobStatus.RETRY_WAIT.value, poll_due),
            stale_reservation,
        )
        with self.database.session() as session:
            result = session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job_id,
                    claimable,
                    GenerationJob.provider_job_id.is_not(None),
                    GenerationJob.submission_state == "CONFIRMED",
                    GenerationJob.output_asset_id.is_(None),
                )
                .values(
                    status=JobStatus.RESERVED.value,
                    reserved_at=now,
                    claim_token=token,
                    claim_expires_at=expires_at,
                )
            )
            if not self._updated_one_row(result):
                return None
            self._event(
                session,
                job_id,
                "JOB_POLL_CLAIMED",
                lease_expires_at=expires_at.isoformat(),
            )
        return token

    def _renew_claim(self, job_id: str, claim_token: str) -> bool:
        now = utcnow()
        with self.database.session() as session:
            result = session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job_id,
                    GenerationJob.status == JobStatus.RESERVED.value,
                    GenerationJob.claim_token == claim_token,
                    GenerationJob.claim_expires_at > now,
                )
                .values(
                    claim_expires_at=now + timedelta(seconds=self.claim_lease_seconds),
                )
            )
            return self._updated_one_row(result)

    def _begin_provider_submission(
        self,
        job_id: str,
        claim_token: str,
        provider_request: dict[str, Any],
        provider: str,
    ) -> bool:
        """Close the retry window durably before invoking a paid provider API."""

        now = utcnow()
        with self.database.session() as session:
            result = session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job_id,
                    GenerationJob.status == JobStatus.RESERVED.value,
                    GenerationJob.claim_token == claim_token,
                    GenerationJob.claim_expires_at > now,
                    GenerationJob.submission_state == "NOT_SENT",
                )
                .values(
                    provider_request_json=provider_request,
                    submission_state="SENT_UNCONFIRMED",
                    safe_to_retry=False,
                )
            )
            if not self._updated_one_row(result):
                return False
            self._event(session, job_id, "REQUEST_SUBMITTED", provider=provider)
        return True

    async def _submit(self, job_id: str, claim_token: str) -> GenerationJob:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job or job.status != JobStatus.RESERVED.value or job.claim_token != claim_token:
                current = job
                if current is None:
                    raise LookupError("generation job not found")
                return current
            next_retry_at = _aware(job.next_retry_at)
            if job.status == JobStatus.RETRY_WAIT.value and next_retry_at and next_retry_at > utcnow():
                return job
            if job.attempt_count >= job.max_attempts:
                job.status = JobStatus.FAILED.value
                job.claim_token = None
                job.claim_expires_at = None
                return job
            request = dict(job.request_json)
            capability = job.generation_type
            provider_name = job.provider
            model = job.model
        try:
            provider = self.providers.validate_target(provider_name, model, capability)
        except GenerationTargetError as exc:
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                exc.code,
                str(exc),
                submitted=False,
                claim_token=claim_token,
            )
        submission_boundary_crossed = False
        try:
            account, worker = self.scheduler.select_account(
                job.provider,
                capability,
                job.model,
                job.priority,
                project_id=job.project_id,
                generation_job_id=job_id,
                claim_token=claim_token,
            )
        except NoAccountAvailable as exc:
            return self._schedule_error(
                job_id,
                RetryCategory.PROVIDER_BUSY,
                "NO_ACCOUNT",
                str(exc),
                submitted=False,
                claim_token=claim_token,
            )
        claim_lost = False
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if (
                not job
                or job.status != JobStatus.RESERVED.value
                or job.claim_token != claim_token
                or job.submission_state != "NOT_SENT"
            ):
                claim_lost = True
                provider_project_id = None
            else:
                job.account_id = account.id
                job.worker_id = worker.id
                job.started_at = job.started_at or utcnow()
                job.reserved_at = utcnow()
                job.claim_expires_at = utcnow() + timedelta(seconds=self.claim_lease_seconds)
                job.attempt_count += 1
                project_binding = session.scalar(
                    select(ProviderProjectBinding).where(
                        ProviderProjectBinding.local_project_id == job.project_id,
                        ProviderProjectBinding.provider == job.provider,
                        ProviderProjectBinding.provider_account_id == account.id,
                        ProviderProjectBinding.status == "READY",
                    )
                )
                provider_project_id = (
                    project_binding.provider_project_id
                    if project_binding
                    else account.metadata_json.get("project_id")
                )
                self._event(
                    session,
                    job.id,
                    "ACCOUNT_SELECTED",
                    account_id=account.id,
                    credits=account.credits,
                )
                self._event(session, job.id, "WORKER_SELECTED", worker_id=worker.id)
                session.flush()
        if claim_lost:
            self.scheduler.release_job(
                job_id,
                success=False,
                error="claim lost",
                clear_routing=True,
            )
            current = self.get(job_id)
            if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                raise LookupError("generation job not found")
            return current
        try:
            request["_provider_project_id"] = provider_project_id
            current_job = self.get(job_id)
            if current_job is None:
                raise LookupError("generation job not found during asset resolution")
            request = await self._resolve_assets(
                current_job,
                request,
                provider,
                provider_project_id=provider_project_id,
            )
            if not self._begin_provider_submission(job_id, claim_token, request, provider.name):
                self.scheduler.release_job(
                    job_id,
                    success=False,
                    error="submission claim expired or was superseded",
                    clear_routing=True,
                )
                current = self.get(job_id)
                if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                    raise LookupError("generation job not found")
                return current
            submission_boundary_crossed = True
            if capability == "image":
                submission = await provider.generate_image(
                    request, account_id=account.id, worker_id=worker.id
                )
            else:
                submission = await provider.generate_video(
                    request, account_id=account.id, worker_id=worker.id
                )
            with self.database.session() as session:
                job = session.get(GenerationJob, job_id)
                job.provider_job_id = submission.provider_job_id
                job.submission_state = "CONFIRMED"
                job.status = JobStatus.SUBMITTED.value
                job.next_retry_at = self._next_poll_at()
                job.claim_token = None
                job.claim_expires_at = None
                job.submitted_at = utcnow()
                remaining_credits = submission.raw.get("remainingCredits")
                if remaining_credits is not None:
                    provider_account = session.get(ProviderAccount, account.id)
                    if provider_account:
                        provider_account.credits = int(remaining_credits)
                idem = session.scalar(
                    select(GenerationIdempotency).where(GenerationIdempotency.generation_job_id == job.id)
                )
                idem.provider_job_id = submission.provider_job_id
                self._event(
                    session, job.id, "PROVIDER_JOB_STARTED", provider_job_id=submission.provider_job_id
                )
                session.flush()
                return job
        except ProviderError as exc:
            return self._schedule_error(
                job_id,
                exc.category,
                exc.code,
                str(exc),
                submitted=exc.submitted,
                claim_token=claim_token,
                release_reservation=not exc.submitted,
                release_error=str(exc),
                clear_routing=not exc.submitted,
            )
        except Exception as exc:
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                "INTERNAL_ERROR",
                str(exc),
                submitted=submission_boundary_crossed,
                claim_token=claim_token,
                release_reservation=not submission_boundary_crossed,
                release_error=str(exc),
                clear_routing=not submission_boundary_crossed,
            )

    async def _poll(self, job_id: str, claim_token: str) -> GenerationJob:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job or job.status != JobStatus.RESERVED.value or job.claim_token != claim_token:
                if job is None:
                    raise LookupError("generation job not found")
                return job
            provider_name = job.provider
            model = job.model
            account_id, worker_id = job.account_id, job.worker_id
            provider_job_id = job.provider_job_id
            capability = job.generation_type
        try:
            provider = self.providers.validate_target(provider_name, model, capability)
        except GenerationTargetError as exc:
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                exc.code,
                str(exc),
                submitted=True,
                claim_token=claim_token,
                release_reservation=True,
                release_error=str(exc),
            )
        if not all([account_id, worker_id, provider_job_id]):
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                "JOB_STATE_INVALID",
                "submitted job is missing provider routing data",
                submitted=True,
                claim_token=claim_token,
                force_user_action=True,
            )
        try:
            result = await provider.get_job(
                provider_job_id,
                account_id=account_id,
                worker_id=worker_id,
                generation_type=capability,
            )
            if not self._renew_claim(job_id, claim_token):
                current = self.get(job_id)
                if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                    raise LookupError("generation job not found")
                return current
            with self.database.session() as session:
                self._event(
                    session, job_id, "PROVIDER_JOB_POLL", status=result.status, progress=result.progress
                )
            if result.status == "FAILED":
                return self._schedule_error(
                    job_id,
                    RetryCategory.PERMANENT_ERROR,
                    "PROVIDER_JOB_FAILED",
                    result.error or "provider job failed",
                    submitted=True,
                    claim_token=claim_token,
                    release_reservation=True,
                    release_error=result.error,
                )
            if result.status != "COMPLETED":
                with self.database.session() as session:
                    job = session.scalar(
                        select(GenerationJob).where(
                            GenerationJob.id == job_id,
                            GenerationJob.status == JobStatus.RESERVED.value,
                            GenerationJob.claim_token == claim_token,
                        )
                    )
                    if job:
                        job.status = JobStatus.RUNNING.value
                        job.next_retry_at = self._next_poll_at()
                        job.claim_token = None
                        job.claim_expires_at = None
                        session.flush()
                        return job
                current = self.get(job_id)
                if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                    raise LookupError("generation job not found")
                return current
            if not result.output_url:
                raise ProviderError(
                    "completed provider job has no output URL",
                    RetryCategory.TRANSIENT_NETWORK,
                    code="OUTPUT_URL_MISSING",
                    submitted=True,
                )
            if not self._renew_claim(job_id, claim_token):
                current = self.get(job_id)
                if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                    raise LookupError("generation job not found")
                return current
            with self.database.session() as session:
                job = session.scalar(
                    select(GenerationJob).where(
                        GenerationJob.id == job_id,
                        GenerationJob.status == JobStatus.RESERVED.value,
                        GenerationJob.claim_token == claim_token,
                    )
                )
                if not job:
                    current = None
                else:
                    project_id, shot_id, candidate_id = (
                        job.project_id,
                        job.shot_id,
                        job.candidate_id,
                    )
                    current = job
            if current is None:
                latest = self.get(job_id)
                if latest is None:  # pragma: no cover - deleted concurrently by an administrator.
                    raise LookupError("generation job not found")
                return latest
            asset_type = AssetType.VIDEO.value if capability == "video" else AssetType.IMAGE.value
            suffix = "mp4" if capability == "video" else "png"
            asset = await self.media.download_and_register(
                project_id,
                asset_type,
                result.output_url,
                filename=f"{job_id}.{suffix}",
                provider=provider.name,
                provider_media_id=provider_job_id,
                shot_id=shot_id,
                generation_candidate_id=candidate_id,
            )
            finalized = False
            with self.database.session() as session:
                job = session.scalar(
                    select(GenerationJob).where(
                        GenerationJob.id == job_id,
                        GenerationJob.status == JobStatus.RESERVED.value,
                        GenerationJob.claim_token == claim_token,
                    )
                )
                if job:
                    finalized = True
                    job.output_asset_id = asset.id
                    job.status = JobStatus.COMPLETED.value
                    job.next_retry_at = None
                    job.claim_token = None
                    job.claim_expires_at = None
                    job.completed_at = utcnow()
                    if candidate_id:
                        candidate = session.get(GenerationCandidate, candidate_id)
                        if candidate:
                            candidate.output_asset_id = asset.id
                            candidate.status = CandidateStatus.VALIDATING.value
                        if shot_id:
                            shot = session.get(Shot, shot_id)
                            if shot:
                                shot.status = ShotStatus.VALIDATING.value
                    idem = session.scalar(
                        select(GenerationIdempotency).where(GenerationIdempotency.generation_job_id == job.id)
                    )
                    idem.status = "SUCCEEDED"
                    idem.result_asset_id = asset.id
                    self._event(session, job.id, "MEDIA_DOWNLOADED", asset_id=asset.id)
                    self._event(session, job.id, "VIDEO_GENERATED", candidate_id=candidate_id)
                    self._event(session, job.id, "DYNAMIC_QA_STARTED", candidate_id=candidate_id)
                    self._event(
                        session,
                        job.id,
                        "PROVIDER_JOB_COMPLETED",
                        provider_job_id=provider_job_id,
                    )
                    self._event(session, job.id, "JOB_COMPLETED", output_asset_id=asset.id)
                    self.scheduler.release_job_in_session(session, job.id, success=True)
            if not finalized:
                current = self.get(job_id)
                if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                    raise LookupError("generation job not found")
                return current
            if shot_id and not candidate_id and capability == "video" and self.continuity:
                try:
                    end_frame = self.continuity.extract_and_chain(shot_id, asset.id)
                    with self.database.session() as session:
                        self._event(session, job_id, "END_FRAME_EXTRACTED", asset_id=end_frame.id)
                except Exception as exc:
                    with self.database.session() as session:
                        self._event(session, job_id, "MEDIA_ERROR", stage="end_frame", error=str(exc))
            current = self.get(job_id)
            if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                raise LookupError("generation job not found")
            return current
        except ProviderError as exc:
            release_reservation = exc.category in {
                RetryCategory.INVALID_REQUEST,
                RetryCategory.CONTENT_REJECTED,
                RetryCategory.PERMANENT_ERROR,
            }
            return self._schedule_error(
                job_id,
                exc.category,
                exc.code,
                str(exc),
                submitted=True,
                claim_token=claim_token,
                release_reservation=release_reservation,
                release_error=str(exc),
            )
        except Exception as exc:
            return self._schedule_error(
                job_id,
                RetryCategory.TRANSIENT_NETWORK,
                "POLL_PROCESSING_ERROR",
                str(exc),
                submitted=True,
                claim_token=claim_token,
            )

    def _schedule_error(
        self,
        job_id: str,
        category: RetryCategory,
        code: str,
        message: str,
        *,
        submitted: bool,
        claim_token: str | None = None,
        release_reservation: bool = False,
        release_error: str | None = None,
        clear_routing: bool = False,
        force_user_action: bool = False,
    ) -> GenerationJob:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if claim_token is not None and (
                not job or job.claim_token != claim_token or job.status != JobStatus.RESERVED.value
            ):
                if job is None:
                    raise LookupError("generation job not found")
                return job
            uncertain_paid_submission = submitted and not job.provider_job_id
            decision = self.retry_policy.decide(
                category,
                job.attempt_count,
                job.max_attempts,
                submitted=uncertain_paid_submission,
            )
            job.retry_category = category.value
            job.error_code = code
            job.error_message = message[:4000]
            job.safe_to_retry = not submitted
            job.claim_token = None
            job.claim_expires_at = None
            if not submitted:
                job.submission_state = "NOT_SENT"
            if force_user_action or decision.requires_user_action:
                job.status = JobStatus.WORKER_NEEDS_USER_ACTION.value
                worker = session.get(BrowserWorker, job.worker_id) if job.worker_id else None
                if worker:
                    worker.status = WorkerStatus.NEEDS_USER_ACTION.value
            elif decision.retry:
                job.status = JobStatus.RETRY_WAIT.value
                job.next_retry_at = utcnow() + timedelta(seconds=decision.delay_seconds)
            else:
                job.status = JobStatus.FAILED.value
            self._event(
                session,
                job.id,
                code,
                category=category.value,
                message=message,
                automatic_retry=decision.retry,
                submitted=submitted,
            )
            idem = session.scalar(
                select(GenerationIdempotency).where(GenerationIdempotency.generation_job_id == job.id)
            )
            if idem and job.status == JobStatus.FAILED.value:
                idem.status = "FAILED"
            if release_reservation:
                self.scheduler.release_job_in_session(
                    session,
                    job.id,
                    success=False,
                    error=release_error or message,
                    clear_routing=clear_routing,
                )
            session.flush()
            return job

    def fail_processing(self, job_id: str, error: Exception) -> GenerationJob:
        """Quarantine one unexpected job failure without terminating the worker loop."""

        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                raise LookupError("generation job not found")
            if job.status in {
                JobStatus.COMPLETED.value,
                JobStatus.CANCELLED.value,
                JobStatus.FAILED.value,
                JobStatus.WORKER_NEEDS_USER_ACTION.value,
            }:
                return job
            submitted = bool(job.provider_job_id) or job.submission_state == "SENT_UNCONFIRMED"
        return self._schedule_error(
            job_id,
            RetryCategory.PERMANENT_ERROR,
            "WORKER_PROCESSING_ERROR",
            f"{type(error).__name__}: {error}",
            submitted=submitted,
        )

    def recover_after_restart(self) -> int:
        recovered = 0
        now = utcnow()
        with self.database.session() as session:
            jobs = session.scalars(
                select(GenerationJob).where(
                    or_(
                        GenerationJob.status.in_(
                            [
                                JobStatus.QUEUED.value,
                                JobStatus.SUBMITTED.value,
                                JobStatus.RUNNING.value,
                            ]
                        ),
                        and_(
                            GenerationJob.status == JobStatus.RESERVED.value,
                            or_(
                                GenerationJob.claim_expires_at.is_(None),
                                GenerationJob.claim_expires_at <= now,
                            ),
                        ),
                    )
                )
            ).all()
            for job in jobs:
                if job.provider_job_id:
                    job.status = JobStatus.SUBMITTED.value
                    job.safe_to_retry = False
                    job.next_retry_at = now
                elif job.submission_state == "SENT_UNCONFIRMED":
                    job.status = JobStatus.WORKER_NEEDS_USER_ACTION.value
                    job.safe_to_retry = False
                else:
                    self.scheduler.release_job_in_session(
                        session,
                        job.id,
                        success=None,
                        clear_routing=True,
                    )
                    job.status = JobStatus.RETRY_WAIT.value
                    job.safe_to_retry = True
                    job.next_retry_at = utcnow()
                    job.submission_state = "NOT_SENT"
                job.claim_token = None
                job.claim_expires_at = None
                self._event(session, job.id, "JOB_RESUMED", status=job.status)
                recovered += 1
        return recovered
