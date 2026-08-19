from __future__ import annotations

import hashlib
import json
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
from provider_sdk import ProviderError
from sqlalchemy import select

from .providers import ProviderRouter
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
    ):
        self.database = database
        self.providers = providers
        self.media = media
        self.scheduler = scheduler
        self.continuity = continuity
        self.retry_policy = retry_policy or RetryPolicy()

    @staticmethod
    def _event(session, job_id: str, event_type: str, **detail: Any) -> None:  # type: ignore[no-untyped-def]
        session.add(GenerationEvent(generation_job_id=job_id, event_type=event_type, detail=detail))

    def create(self, request: GenerationRequest) -> tuple[GenerationJob, bool]:
        payload = request.model_dump(mode="json", exclude={"idempotency_key", "candidate_id"})
        request_hash = canonical_hash(payload)
        with self.database.session() as session:
            if session.get(Project, request.project_id) is None:
                raise LookupError(f"project not found: {request.project_id}")
            existing = session.scalar(
                select(GenerationIdempotency).where(GenerationIdempotency.key == request.idempotency_key)
            )
            if existing:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict("idempotency key already belongs to a different request")
                return session.get(GenerationJob, existing.generation_job_id), True
            job = GenerationJob(
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
            session.add(job)
            session.flush()
            session.add(
                GenerationIdempotency(
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
            if request.shot_id:
                shot = session.get(Shot, request.shot_id)
                if not shot or shot.scene.episode.project_id != request.project_id:
                    raise LookupError("shot does not belong to project")
                shot.generation_job_id = job.id
                shot.status = ShotStatus.QUEUED.value
            if request.candidate_id:
                candidate = session.get(GenerationCandidate, request.candidate_id)
                if not candidate or candidate.shot_id != request.shot_id:
                    raise LookupError("candidate does not belong to shot")
                candidate.generation_job_id = job.id
                candidate.status = CandidateStatus.GENERATING.value
            session.flush()
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
            self._event(session, job.id, "JOB_RETRY_REQUESTED")
            session.flush()
            return job

    def cancel(self, job_id: str) -> GenerationJob:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                raise LookupError("generation job not found")
            if job.status not in {JobStatus.COMPLETED.value, JobStatus.FAILED.value}:
                job.status = JobStatus.CANCELLED.value
                self._event(session, job.id, "JOB_CANCELLED")
            session.flush()
            return job

    def reconcile(self, job_id: str) -> GenerationJob:
        """Recover a late browser response without issuing another paid request."""
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                raise LookupError("generation job not found")
            if job.provider_job_id:
                job.status = JobStatus.SUBMITTED.value
                job.submission_state = "CONFIRMED"
                job.safe_to_retry = False
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
            idem = session.scalar(
                select(GenerationIdempotency).where(GenerationIdempotency.generation_job_id == job.id)
            )
            if idem:
                idem.provider_job_id = provider_job_id
            self._event(session, job.id, "ORPHAN_RESPONSE_RECOVERED", provider_job_id=provider_job_id)
            session.flush()
            return job

    async def _resolve_assets(self, job: GenerationJob, request: dict[str, Any], provider) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        result = dict(request)
        pairs = [
            ("start_frame_asset_id", "start_frame_provider_media_id"),
            ("end_frame_asset_id", "end_frame_provider_media_id"),
        ]
        for source, target in pairs:
            if request.get(source):
                media_id, reused = await self.media.resolve_provider_media(
                    request[source], provider, account_id=job.account_id, worker_id=job.worker_id
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
                asset_id, provider, account_id=job.account_id, worker_id=job.worker_id
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
        if status == JobStatus.RETRY_WAIT.value and job.provider_job_id:
            return await self._poll(job_id)
        if status in {JobStatus.NEW.value, JobStatus.QUEUED.value, JobStatus.RETRY_WAIT.value}:
            return await self._submit(job_id)
        if status in {JobStatus.SUBMITTED.value, JobStatus.RUNNING.value}:
            return await self._poll(job_id)
        return self.get(job_id)

    async def _submit(self, job_id: str) -> GenerationJob:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            next_retry_at = _aware(job.next_retry_at)
            if job.status == JobStatus.RETRY_WAIT.value and next_retry_at and next_retry_at > utcnow():
                return job
            if job.attempt_count >= job.max_attempts:
                job.status = JobStatus.FAILED.value
                return job
            request = dict(job.request_json)
            capability = job.generation_type
            provider = self.providers.get(job.provider)
        try:
            account, worker = self.scheduler.select_account(job.provider, capability, job.model, job.priority)
        except NoAccountAvailable as exc:
            return self._schedule_error(
                job_id, RetryCategory.PROVIDER_BUSY, "NO_ACCOUNT", str(exc), submitted=False
            )
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            job.account_id = account.id
            job.worker_id = worker.id
            job.status = JobStatus.QUEUED.value
            job.started_at = job.started_at or utcnow()
            job.reserved_at = utcnow()
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
            self._event(session, job.id, "ACCOUNT_SELECTED", account_id=account.id, credits=account.credits)
            self._event(session, job.id, "WORKER_SELECTED", worker_id=worker.id)
            session.flush()
        try:
            request["_provider_project_id"] = provider_project_id
            request = await self._resolve_assets(self.get(job_id), request, provider)
            with self.database.session() as session:
                job = session.get(GenerationJob, job_id)
                job.provider_request_json = request
                job.submission_state = "SENT_UNCONFIRMED"
                job.safe_to_retry = False
                self._event(session, job.id, "REQUEST_SUBMITTED", provider=job.provider)
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
            if not exc.submitted:
                self.scheduler.release(account.id, worker.id, capability, success=False, error=str(exc))
            return self._schedule_error(job_id, exc.category, exc.code, str(exc), submitted=exc.submitted)
        except Exception as exc:
            self.scheduler.release(account.id, worker.id, capability, success=False, error=str(exc))
            return self._schedule_error(
                job_id, RetryCategory.PERMANENT_ERROR, "INTERNAL_ERROR", str(exc), submitted=False
            )

    async def _poll(self, job_id: str) -> GenerationJob:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            provider = self.providers.get(job.provider)
            account_id, worker_id = job.account_id, job.worker_id
            provider_job_id = job.provider_job_id
            capability = job.generation_type
            if not all([account_id, worker_id, provider_job_id]):
                return self._schedule_error(
                    job_id,
                    RetryCategory.PERMANENT_ERROR,
                    "JOB_STATE_INVALID",
                    "submitted job is missing provider routing data",
                    submitted=True,
                )
        try:
            result = await provider.get_job(
                provider_job_id,
                account_id=account_id,
                worker_id=worker_id,
                generation_type=capability,
            )
            with self.database.session() as session:
                self._event(
                    session, job_id, "PROVIDER_JOB_POLL", status=result.status, progress=result.progress
                )
            if result.status == "FAILED":
                self.scheduler.release(account_id, worker_id, capability, success=False, error=result.error)
                return self._schedule_error(
                    job_id,
                    RetryCategory.PERMANENT_ERROR,
                    "PROVIDER_JOB_FAILED",
                    result.error or "provider job failed",
                    submitted=True,
                )
            if result.status != "COMPLETED":
                with self.database.session() as session:
                    job = session.get(GenerationJob, job_id)
                    job.status = JobStatus.RUNNING.value
                    session.flush()
                    return job
            if not result.output_url:
                raise ProviderError(
                    "completed provider job has no output URL",
                    RetryCategory.TRANSIENT_NETWORK,
                    code="OUTPUT_URL_MISSING",
                    submitted=True,
                )
            with self.database.session() as session:
                job = session.get(GenerationJob, job_id)
                project_id, shot_id, candidate_id = job.project_id, job.shot_id, job.candidate_id
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
            with self.database.session() as session:
                job = session.get(GenerationJob, job_id)
                job.output_asset_id = asset.id
                job.status = JobStatus.COMPLETED.value
                job.completed_at = utcnow()
                if candidate_id:
                    candidate = session.get(GenerationCandidate, candidate_id)
                    if candidate:
                        candidate.output_asset_id = asset.id
                        candidate.status = CandidateStatus.VALIDATING.value
                    asset_record = session.get(type(asset), asset.id)
                    if asset_record:
                        asset_record.generation_candidate_id = candidate_id
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
                self._event(session, job.id, "PROVIDER_JOB_COMPLETED", provider_job_id=provider_job_id)
                self._event(session, job.id, "JOB_COMPLETED", output_asset_id=asset.id)
            self.scheduler.release(account_id, worker_id, capability, success=True)
            if shot_id and not candidate_id and capability == "video" and self.continuity:
                try:
                    end_frame = self.continuity.extract_and_chain(shot_id, asset.id)
                    with self.database.session() as session:
                        self._event(session, job_id, "END_FRAME_EXTRACTED", asset_id=end_frame.id)
                except Exception as exc:
                    with self.database.session() as session:
                        self._event(session, job_id, "MEDIA_ERROR", stage="end_frame", error=str(exc))
            return self.get(job_id)
        except ProviderError as exc:
            if exc.category in {
                RetryCategory.INVALID_REQUEST,
                RetryCategory.CONTENT_REJECTED,
                RetryCategory.PERMANENT_ERROR,
            }:
                self.scheduler.release(account_id, worker_id, capability, success=False, error=str(exc))
            return self._schedule_error(job_id, exc.category, exc.code, str(exc), submitted=True)

    def _schedule_error(
        self, job_id: str, category: RetryCategory, code: str, message: str, *, submitted: bool
    ) -> GenerationJob:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
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
            if decision.requires_user_action:
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
            session.flush()
            return job

    def recover_after_restart(self) -> int:
        recovered = 0
        with self.database.session() as session:
            jobs = session.scalars(
                select(GenerationJob).where(
                    GenerationJob.status.in_(
                        [
                            JobStatus.QUEUED.value,
                            JobStatus.SUBMITTED.value,
                            JobStatus.RUNNING.value,
                        ]
                    )
                )
            ).all()
            for job in jobs:
                if job.provider_job_id:
                    job.status = JobStatus.SUBMITTED.value
                    job.safe_to_retry = False
                elif job.submission_state == "SENT_UNCONFIRMED":
                    job.status = JobStatus.WORKER_NEEDS_USER_ACTION.value
                    job.safe_to_retry = False
                else:
                    job.status = JobStatus.RETRY_WAIT.value
                    job.safe_to_retry = True
                    job.next_retry_at = utcnow()
                self._event(session, job.id, "JOB_RESUMED", status=job.status)
                recovered += 1
        return recovered
