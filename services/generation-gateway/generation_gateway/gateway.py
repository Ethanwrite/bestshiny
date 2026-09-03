from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from entitlement_core import (
    CanaryReservation,
    LiveCanaryConflict,
    LiveCanaryDenied,
    LiveCanaryPermitService,
    LiveSpendDenied,
    WorkspaceCreditConflict,
    WorkspaceCreditService,
    WorkspaceCreditTransition,
)
from entitlement_core.production_budget import (
    FENCE_CANARY,
    FENCE_PRODUCTION,
    SOURCE_ESTIMATED_QUOTE,
    SOURCE_VERIFIED_PROVIDER,
    ProductionBudgetExceeded,
    ProductionBudgetService,
    SpendAuthorizationConflict,
    SpendAuthorizationView,
)
from media_service import (
    MediaRegistry,
    ProviderReferenceUrlUnavailable,
    RemoteMediaSecurityError,
    StagedProviderOutput,
    generation_staging_prefix,
)
from model_registry_core import (
    CanaryLoop,
    ModelInfrastructureService,
    RuntimeModelState,
    production_serviceable,
    record_canary_outcome,
)
from platform_contracts import (
    TIMELINE_FENCE_METADATA_KEY,
    AuthoritativeTimelineFence,
    GenerationRequest,
    authoritative_timeline_state_hash,
)
from platform_database import Database
from production_domain.models import (
    AssetType,
    BillingEvidenceSource,
    BrowserWorker,
    CandidateStatus,
    CostRecord,
    DecisionRecord,
    GenerationCandidate,
    GenerationEvent,
    GenerationIdempotency,
    GenerationJob,
    JobStatus,
    LiveCanaryUsage,
    MediaAsset,
    ModelCapabilityProfile,
    ModelDefinition,
    Project,
    ProviderAccount,
    ProviderBillingEvidence,
    ProviderProjectBinding,
    ProviderProjectBindingStatus,
    ProviderSynchronousResult,
    ProviderSynchronousResultOutput,
    RetryCategory,
    Shot,
    ShotStatus,
    TimelineState,
    WorkerCommand,
    WorkerStatus,
    WorkspaceCreditEntry,
    utcnow,
)
from production_engine import ShotContinuityService
from provider_sdk import (
    GenerationProvider,
    ProviderError,
    ProviderInlineOutput,
    ProviderJob,
    ProviderMode,
    ProviderPollIdentity,
    ProviderReferenceMode,
)
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .affinity import (
    FLOW_PROVIDER,
    FlowAffinityConflict,
    FlowAffinityUnavailable,
    FlowProjectAllocator,
)
from .providers import GenerationTargetError, ProviderRouter
from .retry import RetryPolicy
from .scheduler import AccountScheduler, NoAccountAvailable


class LiveCanaryResubmissionForbidden(LiveCanaryDenied):
    """The operation's canary is already UNCERTAIN/SETTLED; only an operator resolves it."""


@dataclass(frozen=True)
class LiveGenerationFence:
    """What fenced one live generation at the paid boundary.

    ``canary`` is the operator's permit usage — present while the model has
    not earned ``VERIFIED_LIVE``. ``authorization`` is the job's quote-bound
    spend authorization under the platform breaker — present for every job
    created while the production budget is enabled, on either side of the
    verification line. A verified model runs on the authorization alone.
    """

    canary: CanaryReservation | None
    authorization: SpendAuthorizationView | None


class IdempotencyConflict(RuntimeError):
    pass


class UnsafeRetry(RuntimeError):
    pass


class TimelineGenerationPlanStale(IdempotencyConflict):
    """A 409 conflict: SQL timeline changed after Autopilot prepared a request."""


_TIMELINE_PREQUEUE_STATUSES = frozenset(
    {
        ShotStatus.DRAFT.value,
        ShotStatus.PLANNED.value,
        ShotStatus.READY.value,
    }
)


def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _billing_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed.quantize(Decimal("0.000001"))


def _nested_value(payload: dict[str, Any], *path: str) -> Any:
    current: Any = payload
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _provider_billing_facts(
    raw: dict[str, Any],
) -> tuple[Decimal | None, Decimal | None, str | None, str | None, str]:
    actual_candidates = (
        ("usage.actual_cost_usd", ("usage", "actual_cost_usd")),
        ("usage.total_cost", ("usage", "total_cost")),
        ("usage.cost", ("usage", "cost")),
        ("billing.actual_cost_usd", ("billing", "actual_cost_usd")),
        ("billing.total_cost", ("billing", "total_cost")),
        ("actual_cost_usd", ("actual_cost_usd",)),
        ("actual_cost", ("actual_cost",)),
        ("cost_usd", ("cost_usd",)),
    )
    credit_candidates = (
        ("usage.credits_used", ("usage", "credits_used")),
        ("usage.credits", ("usage", "credits")),
        ("billing.credits_used", ("billing", "credits_used")),
        ("credits_used", ("credits_used",)),
        ("creditsUsed", ("creditsUsed",)),
        ("billed_credits", ("billed_credits",)),
    )
    actual: Decimal | None = None
    actual_field: str | None = None
    for label, path in actual_candidates:
        actual = _billing_decimal(_nested_value(raw, *path))
        if actual is not None:
            actual_field = label
            break
    credits: Decimal | None = None
    credits_field: str | None = None
    for label, path in credit_candidates:
        credits = _billing_decimal(_nested_value(raw, *path))
        if credits is not None:
            credits_field = label
            break
    raw_hash = hashlib.sha256(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return actual, credits, actual_field, credits_field, raw_hash


def _billing_request_facts(job: GenerationJob) -> dict[str, Any]:
    """The request parameters that decide the bill, as actually dispatched.

    Read from `provider_request_json` rather than the original request, because
    that is the payload the provider was given -- a parameter dropped on the way
    to the wire is exactly the failure this is here to make visible, and the
    original request would hide it by still carrying the value.
    """

    sent = job.provider_request_json if isinstance(job.provider_request_json, dict) else {}
    metadata = sent.get("metadata") if isinstance(sent.get("metadata"), dict) else {}
    return {
        "requested_duration_seconds": sent.get("duration"),
        "requested_resolution": sent.get("resolution") or (metadata or {}).get("resolution"),
        "requested_resolution_on_wire": sent.get("resolution") is not None,
        "requested_generate_audio": sent.get("generate_audio"),
        "requested_aspect_ratio": sent.get("aspect_ratio"),
    }


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
        workspace_credits: WorkspaceCreditService | None = None,
        model_infrastructure: ModelInfrastructureService | None = None,
        provider_mode: ProviderMode | str = ProviderMode.MOCK,
        flow_affinity: FlowProjectAllocator | None = None,
        live_canary: LiveCanaryPermitService | None = None,
        production_budget: ProductionBudgetService | None = None,
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
        self.workspace_credits = workspace_credits
        self.model_infrastructure = model_infrastructure
        self.flow_affinity = flow_affinity or FlowProjectAllocator(database, scheduler)
        self.live_canary = live_canary
        self.production_budget = production_budget
        # A synchronous result is not held in this process. It goes into the
        # `provider_synchronous_results` inbox in the same transaction that
        # confirms the submission, because a paid artefact must not depend on
        # this object staying alive. See `_hold_synchronous_result`.
        try:
            self.provider_mode = ProviderMode(provider_mode)
        except ValueError as exc:
            raise ValueError("provider_mode must be mock, recorded, or live") from exc

    def _validate_persisted_generation_target(
        self,
        provider: str,
        model: str,
        media_type: str,
        *,
        workspace_scoped: bool,
        session: Any | None = None,
        lock_for_update: bool = False,
    ) -> RuntimeModelState | None:
        """Apply the server-owned model switch immediately before execution.

        A live transport never receives a legacy bypass.  Non-live, unscoped
        projects retain compatibility only when the execution target has not
        yet been migrated into the persistent registry; an explicitly disabled
        definition remains authoritative in every mode.
        """

        definition = None
        if self.model_infrastructure is not None:
            if session is None:
                definition = self.model_infrastructure.runtime_model_for_target(
                    provider,
                    model,
                    media_type,
                )
            else:
                definition = self.model_infrastructure.runtime_model_for_target_in_session(
                    session,
                    provider,
                    model,
                    media_type,
                    for_update=lock_for_update,
                )
        if definition is None:
            if self.provider_mode is not ProviderMode.LIVE and not workspace_scoped:
                return None
            code = (
                "MODEL_DEFINITION_NOT_FOUND"
                if self.model_infrastructure is not None
                else "MODEL_REGISTRY_REQUIRED"
            )
            raise GenerationTargetError(
                code,
                f"server model definition is required for {media_type} target: {provider}:{model}",
            )
        if not definition.enabled:
            raise GenerationTargetError(
                "MODEL_DISABLED",
                f"server model definition is disabled: {provider}:{model}",
            )
        if definition.lifecycle_status in {"DISABLED", "BLOCKED"}:
            raise GenerationTargetError(
                "MODEL_LIFECYCLE_BLOCKED",
                f"model lifecycle denies new generation traffic: {provider}:{model}",
            )
        required_operation = f"{media_type}_generation"
        if required_operation not in definition.supported_operations:
            raise GenerationTargetError(
                "MODEL_CAPABILITY_DENIED",
                f"authoritative model profile does not support {required_operation}: {provider}:{model}",
            )
        if self.provider_mode is ProviderMode.LIVE and not definition.live_enabled:
            raise GenerationTargetError(
                "MODEL_LIVE_DISABLED",
                f"live generation is disabled for server model definition: {provider}:{model}",
            )
        return definition

    def _next_poll_at(self) -> datetime:
        return utcnow() + timedelta(seconds=self.poll_interval_seconds)

    @staticmethod
    def _live_canary_operation_key(job_id: str) -> str:
        return f"generation:{job_id}"

    def _reserve_live_generation_fence(
        self,
        *,
        job_id: str,
        provider: str,
        model: str,
        media_type: str,
    ) -> LiveGenerationFence | None:
        """Take every hold this live generation needs, then mark them UNCERTAIN.

        The job's spend authorization (created with its credit reservation)
        is the automatic fence; it is enough on its own for every serviceable
        model — enabled, live-enabled, not blocked — while the budget is
        enabled. The operator's ``LiveCanaryPermit`` is required only where the
        budget does not reach (budget disabled, or no authorization was
        created for the job), holding its whole remaining budget for the one
        call exactly as before. Every hold is taken before any of them is
        marked UNCERTAIN, so a refused permit or a tripped breaker leaves
        nothing held.
        """

        if self.provider_mode is not ProviderMode.LIVE:
            return None
        key = self._live_canary_operation_key(job_id)
        authorization: SpendAuthorizationView | None = None
        if self.production_budget is not None:
            authorization = self.production_budget.find_operation(key)
        if authorization is not None and authorization.status in {"UNCERTAIN", "SETTLED"}:
            raise LiveCanaryResubmissionForbidden(
                "live generation spend authorization is already uncertain or settled; "
                "automatic resubmission is forbidden"
            )
        serviceable = False
        if (
            authorization is not None
            and self.production_budget is not None
            and self.production_budget.enabled
            and self.model_infrastructure is not None
        ):
            state = self.model_infrastructure.runtime_model_for_target(provider, model, media_type)
            serviceable = state is not None and production_serviceable(
                enabled=state.enabled,
                live_enabled=state.live_enabled,
                lifecycle_status=state.lifecycle_status,
            )
        canary: CanaryReservation | None = None
        if not serviceable:
            if self.live_canary is None:
                raise LiveCanaryDenied("live media generation requires a durable LiveCanaryPermit")
            reservation = self.live_canary.reserve_matching(
                provider=provider,
                model=model,
                # GenerationRequest.cost_estimate is not a trusted billing quote.
                # Hold the permit's entire remaining budget for the one operation.
                estimated_cost_usd=None,
                idempotency_key=key,
            )
            if reservation.replayed and reservation.status in {"UNCERTAIN", "SETTLED"}:
                raise LiveCanaryResubmissionForbidden(
                    "live generation canary outcome is already uncertain or settled; "
                    "automatic resubmission is forbidden"
                )
            canary = reservation
        evidence = f"generation-boundary-prepared:{job_id}"
        try:
            if authorization is not None:
                if self.production_budget is None:  # pragma: no cover - guarded above.
                    raise RuntimeError("production budget service disappeared")
                # First, because a RELEASED authorization is re-reserved here and
                # the breaker may refuse it; the permit is still only RESERVED.
                authorization = self.production_budget.prepare_boundary(
                    authorization.id,
                    provider=provider,
                    model=model,
                    fence=FENCE_CANARY if canary is not None else FENCE_PRODUCTION,
                    evidence_reference=evidence,
                )
            if canary is not None:
                if self.live_canary is None:  # pragma: no cover - guarded above.
                    raise RuntimeError("live canary service disappeared")
                canary = self.live_canary.mark_uncertain(canary.usage_id, evidence_reference=evidence)
        except BaseException:
            if canary is not None and canary.status == "RESERVED" and self.live_canary is not None:
                try:
                    self.live_canary.release(canary.usage_id)
                except Exception:
                    pass
            if (
                authorization is not None
                and authorization.status == "UNCERTAIN"
                and self.production_budget is not None
            ):
                try:
                    self.production_budget.release_pre_boundary(
                        authorization.id,
                        evidence_reference=f"generation-boundary-abandoned:{job_id}",
                    )
                except Exception:
                    pass
            raise
        return LiveGenerationFence(canary=canary, authorization=authorization)

    def _release_live_generation_fence_before_boundary(
        self,
        fence: LiveGenerationFence | None,
        *,
        job_id: str,
        boundary_crossed: bool,
    ) -> None:
        if fence is None or boundary_crossed:
            return
        evidence = f"generation-pre-boundary-release:{job_id}"
        if fence.canary is not None and self.live_canary is not None:
            self.live_canary.release_pre_boundary(fence.canary.usage_id, evidence_reference=evidence)
        if fence.authorization is not None and self.production_budget is not None:
            self.production_budget.release_pre_boundary(fence.authorization.id, evidence_reference=evidence)

    def _require_live_generation_fence_boundary(
        self,
        *,
        job_id: str,
        provider: str,
        model: str,
        session: Session | None = None,
        allow_settled: bool = False,
    ) -> LiveGenerationFence | None:
        """Assert the operation owns the durable hold(s) its fence kind requires."""

        if self.provider_mode is not ProviderMode.LIVE:
            return None
        key = self._live_canary_operation_key(job_id)
        allowed = frozenset({"UNCERTAIN", "SETTLED"}) if allow_settled else frozenset({"UNCERTAIN"})
        authorization: SpendAuthorizationView | None = None
        if self.production_budget is not None:
            authorization = self.production_budget.find_operation(key, session=session)
        canary: CanaryReservation | None = None
        if authorization is None or authorization.fence != FENCE_PRODUCTION:
            if self.live_canary is None:
                raise LiveCanaryDenied("live media generation requires a durable LiveCanaryPermit")
            canary = self.live_canary.require_operation_boundary(
                provider=provider,
                model=model,
                idempotency_key=key,
                allowed_statuses=allowed,
                # A fresh paid upload/submit must still be inside its explicit
                # authorization window. Poll/cancel/settle may safely manage an
                # operation whose paid boundary was already crossed.
                require_unexpired=not allow_settled,
                session=session,
            )
        if authorization is not None:
            if self.production_budget is None:  # pragma: no cover - guarded above.
                raise RuntimeError("production budget service disappeared")
            authorization = self.production_budget.require_operation(
                operation_key=key,
                provider=provider,
                model=model,
                allowed_statuses=allowed,
                session=session,
            )
        return LiveGenerationFence(canary=canary, authorization=authorization)

    def _settle_live_generation_fence(
        self,
        *,
        job_id: str,
        provider: str,
        model: str,
        provider_job_id: str,
        raw: dict[str, Any],
    ) -> None:
        if self.provider_mode is not ProviderMode.LIVE:
            return
        actual, _, _, _, _ = _provider_billing_facts(raw)
        key = self._live_canary_operation_key(job_id)
        authorization = (
            self.production_budget.find_operation(key) if self.production_budget is not None else None
        )
        if actual is None and authorization is None:
            # The permit keeps its whole hold until an operator reconciles the
            # usage, exactly as before: an unpriced settlement is not evidence.
            return
        fence = self._require_live_generation_fence_boundary(
            job_id=job_id,
            provider=provider,
            model=model,
            allow_settled=True,
        )
        if fence is None:  # pragma: no cover - live mode invariant.
            raise RuntimeError("live generation fence disappeared")
        evidence = f"provider-job:{provider}:{provider_job_id}"
        if fence.canary is not None and actual is not None and self.live_canary is not None:
            self.live_canary.settle(
                fence.canary.usage_id,
                actual_cost_usd=actual,
                evidence_reference=evidence,
            )
        if fence.authorization is not None and self.production_budget is not None:
            settled = self.production_budget.settle(
                fence.authorization.id,
                actual_cost_usd=actual,
                evidence_reference=evidence,
                source=SOURCE_VERIFIED_PROVIDER if actual is not None else SOURCE_ESTIMATED_QUOTE,
            )
            if settled.overran_quote and not settled.replayed:
                # The fence held; the price table did not. Loud, never silent.
                with self.database.session() as session:
                    self._event(
                        session,
                        job_id,
                        "SPEND_QUOTE_OVERRUN",
                        max_cost_usd=str(settled.max_cost_usd),
                        actual_cost_usd=str(settled.actual_cost_usd),
                        authorization_id=settled.id,
                    )

    def _artifact_in_storage(self, asset: MediaAsset | None) -> bool:
        if asset is None or asset.size_bytes <= 0:
            return False
        storage = getattr(self.media, "storage", None)
        stat = getattr(storage, "stat", None)
        if not callable(stat):
            return True
        try:
            found = stat(asset.storage_key)
        except Exception:
            return False
        return found is not None and int(getattr(found, "size", -1)) == int(asset.size_bytes)

    def _record_live_generation_canary_verdict(
        self,
        *,
        job_id: str,
        provider: str,
        model: str,
        provider_job_id: str,
    ) -> None:
        """A live generation that closed its loop earns the model VERIFIED_LIVE.

        The verdict rule lives in `model_registry_core.live_canary` and is not
        re-decided here: every link — reached the provider, COMPLETED, artifact
        registered and readable, credits settled for exactly what was held —
        or nothing is written. Both fences count: a permit-fenced canary and a
        user's generation on the automatic budget are the same evidence about
        the model. The verdict is evidence for lifecycle and routing, never a
        gate on paying traffic.
        """

        if self.provider_mode is not ProviderMode.LIVE:
            return
        key = self._live_canary_operation_key(job_id)
        with self.database.session() as session:
            usage = session.scalar(select(LiveCanaryUsage).where(LiveCanaryUsage.idempotency_key == key))
            authorization = (
                self.production_budget.find_operation(key, session=session)
                if self.production_budget is not None
                else None
            )
            if usage is None and authorization is None:
                return
            job = session.get(GenerationJob, job_id)
            if job is None:
                return
            asset = session.get(MediaAsset, job.output_asset_id) if job.output_asset_id else None
            credit = (
                self.workspace_credits.entry_for_job_in_session(session, job_id)
                if self.workspace_credits is not None
                else None
            )
            loop = CanaryLoop(
                provider=provider,
                model=model,
                job_id=job_id,
                submission_state=job.submission_state,
                terminal_status=job.status,
                output_asset_id=job.output_asset_id,
                artifact_bytes=int(asset.size_bytes) if asset is not None else 0,
                artifact_in_storage=self._artifact_in_storage(asset),
                credit_status=credit.status if credit is not None else "",
                credits_reserved=int(credit.credits) if credit is not None else 0,
                credits_settled=int(credit.settled_credits) if credit is not None else 0,
                error_code=job.error_code,
                provider_task_id=provider_job_id,
            )
        record = record_canary_outcome(self.database, loop)
        if record is None:
            return
        with self.database.session() as session:
            self._event(
                session,
                job_id,
                "LIVE_CANARY_VERDICT_RECORDED",
                logical_name=record.logical_name,
                previous_status=record.previous_status,
                status=record.status,
                detail=record.detail,
            )

    @staticmethod
    def _event(session, job_id: str, event_type: str, **detail: Any) -> None:  # type: ignore[no-untyped-def]
        session.add(GenerationEvent(generation_job_id=job_id, event_type=event_type, detail=detail))

    @staticmethod
    def _updated_one_row(result: Any) -> bool:
        return int(getattr(result, "rowcount", 0)) == 1

    @staticmethod
    def _timeline_state_for_update(session: Any, state_id: str) -> TimelineState | None:
        return session.scalar(
            select(TimelineState)
            .where(TimelineState.id == state_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    def _validate_timeline_fence_in_session(
        self,
        session: Any,
        request: GenerationRequest,
        shot: Shot,
        fence: AuthoritativeTimelineFence,
    ) -> None:
        """Lock and compare the exact SQL state used to prepare Autopilot."""

        if request.shot_id != fence.shot_id or shot.id != fence.shot_id:
            raise TimelineGenerationPlanStale(
                "generation timeline fence belongs to a different shot; plan the shot again"
            )
        if (
            shot.status != fence.shot_status
            or shot.status not in _TIMELINE_PREQUEUE_STATUSES
            or shot.input_state_id != fence.input_state_id
            or shot.output_state_id != fence.output_state_id
        ):
            raise TimelineGenerationPlanStale(
                "shot or authoritative timeline binding changed; plan the shot again"
            )
        input_state = self._timeline_state_for_update(session, fence.input_state_id)
        output_state = self._timeline_state_for_update(session, fence.output_state_id)
        if input_state is None or output_state is None:
            raise TimelineGenerationPlanStale("authoritative timeline state disappeared; plan the shot again")
        if (
            input_state.project_id != request.project_id
            or input_state.state_kind != "SHOT_INPUT"
            or input_state.shot_id not in {None, shot.id}
            or output_state.project_id != request.project_id
            or output_state.state_kind != "SHOT_OUTPUT"
            or output_state.shot_id not in {None, shot.id}
        ):
            raise TimelineGenerationPlanStale("authoritative timeline ownership changed; plan the shot again")
        input_hash = authoritative_timeline_state_hash(
            input_state.state_json,
            previous_state_id=input_state.previous_state_id,
        )
        output_hash = authoritative_timeline_state_hash(
            output_state.state_json,
            previous_state_id=output_state.previous_state_id,
        )
        if input_hash != fence.input_state_hash or output_hash != fence.output_state_hash:
            raise TimelineGenerationPlanStale(
                "authoritative timeline changed after generation planning; plan the shot again"
            )

    def create(
        self,
        request: GenerationRequest,
        *,
        on_create: Callable[[Any, GenerationJob, bool], None] | None = None,
        estimated_credits: int | None = None,
        pricing_version: str = "",
        quoted_cost_usd: float | Decimal | None = None,
        resolution: str = "720p",
        timeline_fence: AuthoritativeTimelineFence | None = None,
    ) -> tuple[GenerationJob, bool]:
        attempts = 6 if self.database.engine.dialect.name == "sqlite" else 1
        for attempt in range(attempts):
            try:
                return self._create_once(
                    request,
                    on_create=on_create,
                    estimated_credits=estimated_credits,
                    pricing_version=pricing_version,
                    quoted_cost_usd=quoted_cost_usd,
                    resolution=resolution,
                    timeline_fence=timeline_fence,
                )
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
        estimated_credits: int | None = None,
        pricing_version: str = "",
        quoted_cost_usd: float | Decimal | None = None,
        resolution: str = "720p",
        timeline_fence: AuthoritativeTimelineFence | None = None,
    ) -> tuple[GenerationJob, bool]:
        self.providers.validate_target(
            request.provider,
            request.model,
            request.type,
            asset_criticality=request.asset_criticality,
        )
        payload = request.model_dump(mode="json", exclude={"idempotency_key", "candidate_id"})
        metadata = dict(payload.get("metadata") or {})
        # This namespace is exclusively server-owned. Public request metadata
        # cannot forge or suppress the authoritative timeline fence.
        metadata.pop(TIMELINE_FENCE_METADATA_KEY, None)
        payload["metadata"] = metadata
        # Idempotency describes the caller-visible generation request. The
        # server fence is execution metadata: persisting it is required for
        # audit, but including it here would turn a normal post-QUEUE replay
        # into a false conflict merely because the Shot status advanced.
        request_hash = canonical_hash(payload)
        if timeline_fence is not None:
            payload["metadata"] = {
                **metadata,
                TIMELINE_FENCE_METADATA_KEY: timeline_fence.model_dump(mode="json"),
            }
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

            project = session.get(Project, request.project_id)
            if project is None:
                raise LookupError(f"project not found: {request.project_id}")
            workspace_credit_required = False
            if self.workspace_credits is not None:
                credit_context = self.workspace_credits.balance_in_session(session, request.project_id)
                # One definition of who pays, on the balance itself: every plan
                # does, and the development bypass and pre-commercial projects
                # do not.
                workspace_credit_required = credit_context.billable
                if workspace_credit_required and estimated_credits is None:
                    raise WorkspaceCreditConflict("workspace generation requires a server-owned credit quote")
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

            def claimed_key() -> GenerationIdempotency | None:
                return session.scalar(
                    select(GenerationIdempotency).where(
                        GenerationIdempotency.project_id == request.project_id,
                        GenerationIdempotency.key == request.idempotency_key,
                    )
                )

            existing = claimed_key()
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
                        workspace_credit_required=workspace_credit_required,
                        quoted_credits=estimated_credits or 0,
                    )
                    shot = None
                    if request.shot_id:
                        shot = session.scalar(
                            select(Shot)
                            .where(Shot.id == request.shot_id)
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                        if not shot or shot.scene.episode.project_id != request.project_id:
                            raise LookupError("shot does not belong to project")
                        if timeline_fence is not None:
                            self._validate_timeline_fence_in_session(
                                session,
                                request,
                                shot,
                                timeline_fence,
                            )
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
                    if estimated_credits is not None and self.workspace_credits is not None:
                        credit_reservation = self.workspace_credits.reserve_generation(
                            session,
                            job,
                            idempotency_key=request.idempotency_key,
                            credits=estimated_credits,
                            metadata={"pricing_version": pricing_version},
                        )
                        if credit_reservation.applied and not credit_reservation.replayed:
                            self._event(
                                session,
                                job.id,
                                "CREDIT_RESERVED",
                                credits=credit_reservation.credits,
                                balance_after=credit_reservation.balance_after,
                            )
                    if (
                        self.provider_mode is ProviderMode.LIVE
                        and self.production_budget is not None
                        and self.production_budget.enabled
                    ):
                        # Same transaction as the credit reservation: one
                        # single-use USD authorization bound to workspace, job,
                        # provider and model, capped at the server quote, and
                        # counted against the platform and provider breakers
                        # before any of it can be spent. A tripped breaker
                        # rolls the job and its credits back together.
                        authorization = self.production_budget.authorize_generation_in_session(
                            session,
                            operation_key=self._live_canary_operation_key(job.id),
                            generation_job_id=job.id,
                            workspace_id=project.workspace_id,
                            project_id=job.project_id,
                            provider=job.provider,
                            model=job.model,
                            max_cost_usd=(
                                Decimal(str(quoted_cost_usd)) if quoted_cost_usd is not None else Decimal("0")
                            ),
                            quoted_credits=estimated_credits or 0,
                            pricing_version=pricing_version,
                        )
                        if not authorization.replayed:
                            self._event(
                                session,
                                job.id,
                                "SPEND_AUTHORIZED",
                                authorization_id=authorization.id,
                                max_cost_usd=str(authorization.max_cost_usd),
                                quoted_credits=authorization.quoted_credits,
                            )
                    session.add(
                        CostRecord(
                            project_id=job.project_id,
                            shot_id=job.shot_id,
                            candidate_id=job.candidate_id,
                            generation_job_id=job.id,
                            provider=job.provider,
                            model=job.model,
                            duration=float(request.duration or 0),
                            resolution=resolution,
                            credits=float(estimated_credits or 0),
                            estimated_cost=request.cost_estimate,
                        )
                    )
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
                # A competitor claimed the key between the lookup above and this
                # insert. Both requests are the same request; one job answers both.
                concurrent = claimed_key()
                if concurrent:
                    return replay(concurrent)
                raise
            except TimelineGenerationPlanStale:
                # The fence is evaluated under the Shot's row lock, so a
                # competitor holding that lock commits *before* this request can
                # read the Shot — and what it reads is the Shot the competitor
                # just moved to QUEUED. The fence is then stale against a change
                # this very request caused, and the request would be told to plan
                # again for work that is already running.
                #
                # A stale fence is therefore not conclusive on its own. It is
                # conclusive only once the key is known to be unclaimed: if a
                # claim exists it is a claim for this same request — `replay`
                # still refuses a key whose request hash differs — and the
                # idempotent answer is the competitor's job, not a 409.
                #
                # Ordering the claim ahead of the fence would reach the same
                # place through `IntegrityError` above, but it would mean writing
                # a job row before the plan behind it has been validated.
                concurrent = claimed_key()
                if concurrent is None:
                    raise
                return replay(concurrent)
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

    def credit_status(self, job_id: str) -> str | None:
        if self.workspace_credits is None:
            return None
        with self.database.session() as session:
            entry = self.workspace_credits.entry_for_job_in_session(session, job_id)
            return entry.status if entry is not None else None

    def _record_provider_billing_evidence(
        self,
        session: Session,
        job: GenerationJob,
        *,
        provider_job_id: str,
        raw: dict[str, Any],
    ) -> ProviderBillingEvidence:
        reported_actual, reported_credits, actual_field, credits_field, raw_hash = _provider_billing_facts(
            raw
        )
        trusted_provider_evidence = self.provider_mode is ProviderMode.LIVE
        actual = reported_actual if trusted_provider_evidence else None
        provider_credits = reported_credits if trusted_provider_evidence else None
        source = (
            BillingEvidenceSource.VERIFIED_PROVIDER
            if actual is not None or provider_credits is not None
            else BillingEvidenceSource.UNKNOWN
        )
        evidence_key = f"provider-completion:{hashlib.sha256(provider_job_id.encode()).hexdigest()[:32]}"
        existing = session.scalar(
            select(ProviderBillingEvidence).where(
                ProviderBillingEvidence.generation_job_id == job.id,
                ProviderBillingEvidence.evidence_key == evidence_key,
            )
        )
        if existing is not None:
            return existing
        cost_record = session.scalar(select(CostRecord).where(CostRecord.generation_job_id == job.id))
        estimated = _billing_decimal(job.cost_estimate) if job.cost_estimate > 0 else None
        evidence = ProviderBillingEvidence(
            generation_job_id=job.id,
            cost_record_id=cost_record.id if cost_record is not None else None,
            evidence_key=evidence_key,
            provider=job.provider,
            model=job.model,
            source=source.value,
            provider_reference=provider_job_id,
            actual_cost_usd=actual,
            estimated_cost_usd=estimated,
            provider_credits=provider_credits,
            metadata_json={
                "actual_field": actual_field,
                "credits_field": credits_field,
                "provider_response_sha256": raw_hash,
                "provider_mode": self.provider_mode.value,
                # What we asked for, recorded beside what we were charged.
                # OpenRouter returns `usage: {cost, is_byok}` and nothing else --
                # no billable duration, no resolution echo, no audio flag -- so
                # the cost is authoritative and the reason for it is not
                # recoverable from the provider at all. Without these, an
                # estimate that misses can only be explained by arithmetic on
                # two numbers, which is how a 2-second 480p clip charged at
                # USD 0.2125 against a USD 0.101 estimate ended up with two
                # rival explanations and no way to choose between them.
                **_billing_request_facts(job),
                "reported_actual_cost_ignored": (
                    reported_actual is not None and not trusted_provider_evidence
                ),
                "reported_provider_credits_ignored": (
                    reported_credits is not None and not trusted_provider_evidence
                ),
            },
            verified_at=utcnow() if source is BillingEvidenceSource.VERIFIED_PROVIDER else None,
        )
        session.add(evidence)
        if actual is not None:
            job.actual_cost = float(actual)
            if cost_record is not None:
                cost_record.actual_cost = float(actual)
        session.flush([evidence])
        self._event(
            session,
            job.id,
            "PROVIDER_BILLING_EVIDENCE",
            evidence_id=evidence.id,
            source=source.value,
            has_actual_cost=actual is not None,
            has_provider_credits=provider_credits is not None,
        )
        return evidence

    def retry(self, job_id: str) -> GenerationJob:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                raise LookupError("generation job not found")
            if job.deleted_at is not None:
                # A deleted creation has no future. Reads as absent rather
                # than unsafe: to its owner it no longer exists.
                raise LookupError("generation job not found")
            if job.status == JobStatus.COMPLETED.value:
                return job
            if job.status in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
                raise UnsafeRetry(
                    "terminal generation jobs cannot be reused; submit a new idempotent generation"
                )
            if self.workspace_credits is not None:
                credit = self.workspace_credits.entry_for_job_in_session(session, job.id)
                if credit and credit.status in {"REFUNDED", "SETTLED", "RECONCILIATION_REQUIRED"}:
                    raise UnsafeRetry(
                        f"credit reservation is {credit.status}; submit a new generation or reconcile it"
                    )
            if not job.safe_to_retry or job.submission_state == "SENT_UNCONFIRMED":
                raise UnsafeRetry(
                    "provider submission may already have consumed credits; reconcile it before retrying"
                )
            if job.provider_job_id or job.submission_state != "NOT_SENT":
                raise UnsafeRetry("only a generation that has not reached the provider may be retried")
            observed_status = job.status
            conditions: list[Any] = [
                GenerationJob.id == job.id,
                GenerationJob.status == observed_status,
                GenerationJob.safe_to_retry.is_(True),
                GenerationJob.submission_state == "NOT_SENT",
                GenerationJob.provider_job_id.is_(None),
                GenerationJob.output_asset_id.is_(None),
            ]
            if self.workspace_credits is not None and credit is not None:
                conditions.append(
                    select(WorkspaceCreditEntry.id)
                    .where(
                        WorkspaceCreditEntry.id == credit.id,
                        WorkspaceCreditEntry.status == "RESERVED",
                    )
                    .exists()
                )
            retried = session.execute(
                update(GenerationJob)
                .where(*conditions)
                .values(
                    status=JobStatus.RETRY_WAIT.value,
                    next_retry_at=utcnow(),
                    error_code=None,
                    error_message=None,
                    claim_token=None,
                    claim_expires_at=None,
                    submission_state="NOT_SENT",
                )
            )
            if not self._updated_one_row(retried):
                session.expire_all()
                current = session.get(GenerationJob, job_id)
                if current is None:  # pragma: no cover - deleted by an administrator.
                    raise LookupError("generation job not found")
                if current.status == JobStatus.COMPLETED.value:
                    return current
                if current.status in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
                    raise UnsafeRetry(
                        "terminal generation jobs cannot be reused; submit a new idempotent generation"
                    )
                raise UnsafeRetry("generation state changed while retry was being requested")
            session.expire(job)
            session.refresh(job)
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
                    if self.workspace_credits is not None:
                        credit_transition = self.workspace_credits.require_reconciliation(
                            session,
                            job,
                            reason="CANCEL_REQUEST_DURING_UNCONFIRMED_SUBMISSION",
                        )
                        if credit_transition.applied and not credit_transition.replayed:
                            self._event(session, job.id, "CREDIT_RECONCILIATION_REQUIRED")
                    return job
                local_cancelled = session.execute(
                    update(GenerationJob)
                    .where(
                        GenerationJob.id == job.id,
                        ~GenerationJob.status.in_(
                            [
                                JobStatus.COMPLETED.value,
                                JobStatus.FAILED.value,
                                JobStatus.CANCELLED.value,
                            ]
                        ),
                        GenerationJob.provider_job_id.is_(None),
                        GenerationJob.submission_state == "NOT_SENT",
                        GenerationJob.output_asset_id.is_(None),
                    )
                    .values(
                        status=JobStatus.CANCELLED.value,
                        safe_to_retry=False,
                        next_retry_at=None,
                        claim_token=None,
                        claim_expires_at=None,
                    )
                )
                if self._updated_one_row(local_cancelled):
                    session.expire(job)
                    session.refresh(job)
                    if self.workspace_credits is not None:
                        credit_refund = self.workspace_credits.refund_generation(
                            session,
                            job,
                            reason="GENERATION_CANCELLED_BEFORE_SUBMISSION",
                        )
                        if credit_refund.applied and not credit_refund.replayed:
                            self._event(
                                session,
                                job.id,
                                "CREDIT_REFUNDED",
                                credits=credit_refund.refunded_credits,
                                reason="GENERATION_CANCELLED_BEFORE_SUBMISSION",
                            )
                    self.scheduler.release_job_in_session(
                        session,
                        job.id,
                        success=None,
                        clear_routing=True,
                    )
                    self._event(session, job.id, "JOB_CANCELLED", remote_job=False)
                    session.flush()
                    return job

                # A submit/complete transaction won the race. Re-read its
                # durable facts before deciding whether cancellation is local,
                # remote, or financially ambiguous.
                session.expire_all()
                job = session.get(GenerationJob, job_id)
                if job is None:  # pragma: no cover - deleted by an administrator.
                    raise LookupError("generation job not found")
                if job.status in {
                    JobStatus.COMPLETED.value,
                    JobStatus.FAILED.value,
                    JobStatus.CANCELLED.value,
                }:
                    return job
                if job.submission_state == "SENT_UNCONFIRMED":
                    if self.workspace_credits is not None:
                        credit_transition = self.workspace_credits.require_reconciliation(
                            session,
                            job,
                            reason="CANCEL_RACED_WITH_PROVIDER_SUBMISSION",
                        )
                        if credit_transition.applied and not credit_transition.replayed:
                            self._event(session, job.id, "CREDIT_RECONCILIATION_REQUIRED")
                    return job
                if not (job.provider_job_id and job.submission_state == "CONFIRMED"):
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
            self._require_live_generation_fence_boundary(
                job_id=job_id,
                provider=provider_name,
                model=model,
                allow_settled=True,
            )
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
            terminalized = session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job_id,
                    GenerationJob.status == JobStatus.RESERVED.value,
                    GenerationJob.claim_token == claim_token,
                    GenerationJob.provider_job_id == provider_job_id,
                    GenerationJob.submission_state == "CONFIRMED",
                    GenerationJob.output_asset_id.is_(None),
                )
                .values(
                    status=JobStatus.CANCELLED.value,
                    safe_to_retry=False,
                    next_retry_at=None,
                    claim_token=None,
                    claim_expires_at=None,
                    error_code=None,
                    error_message=None,
                )
            )
            if not self._updated_one_row(terminalized):
                current = None
            else:
                job = session.get(GenerationJob, job_id)
                if job is None:  # pragma: no cover - guarded by the conditional update.
                    raise LookupError("generation job not found")
                session.refresh(job)
                if self.workspace_credits is not None:
                    credit_transition = self.workspace_credits.require_reconciliation(
                        session,
                        job,
                        reason="PROVIDER_CANCELLED_WITH_BILLING_UNKNOWN",
                    )
                    if credit_transition.applied and not credit_transition.replayed:
                        self._event(
                            session,
                            job.id,
                            "CREDIT_RECONCILIATION_REQUIRED",
                            reason="PROVIDER_CANCELLED_WITH_BILLING_UNKNOWN",
                        )
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
                job = session.get(GenerationJob, job_id)
                if job is not None and self.workspace_credits is not None:
                    self.workspace_credits.require_reconciliation(
                        session,
                        job,
                        reason="PROVIDER_CANCEL_UNCONFIRMED",
                    )
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
            provider_job_id = job.provider_job_id
            if not provider_job_id:
                command = session.scalar(
                    select(WorkerCommand)
                    .where(
                        WorkerCommand.generation_job_id == job.id,
                        WorkerCommand.message_type == "provider.request",
                        WorkerCommand.status == "COMPLETED",
                    )
                    .order_by(WorkerCommand.completed_at.desc())
                )
                if command and job.worker_id and command.worker_id != job.worker_id:
                    return job
                response = command.response if command else None
                response_status = response.get("status") if isinstance(response, dict) else None
                if not isinstance(response_status, int) or not 200 <= response_status < 300:
                    return job
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
            recovery_identity_conditions: list[Any] = []
            if job.provider == FLOW_PROVIDER:
                if not job.account_id or not job.provider_project_id:
                    job.error_code = "FLOW_POLL_IDENTITY_MISSING"
                    job.error_message = (
                        "late Google Flow response cannot be confirmed until its account/project "
                        "identity is manually reconciled"
                    )
                    self._event(
                        session,
                        job.id,
                        "FLOW_POLL_IDENTITY_RECONCILIATION_REQUIRED",
                        provider_job_id=provider_job_id,
                    )
                    return job
                recovery_identity_conditions = [
                    GenerationJob.account_id == job.account_id,
                    GenerationJob.provider_project_id == job.provider_project_id,
                ]
            recovered = session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job.id,
                    GenerationJob.status == JobStatus.WORKER_NEEDS_USER_ACTION.value,
                    GenerationJob.submission_state == "SENT_UNCONFIRMED",
                    GenerationJob.output_asset_id.is_(None),
                    or_(
                        GenerationJob.provider_job_id.is_(None),
                        GenerationJob.provider_job_id == provider_job_id,
                    ),
                    *recovery_identity_conditions,
                )
                .values(
                    provider_job_id=provider_job_id,
                    submission_state="CONFIRMED",
                    status=JobStatus.SUBMITTED.value,
                    safe_to_retry=False,
                    next_retry_at=self._next_poll_at(),
                    claim_token=None,
                    claim_expires_at=None,
                )
            )
            if not self._updated_one_row(recovered):
                session.expire_all()
                current = session.get(GenerationJob, job_id)
                if current is None:  # pragma: no cover - deleted by an administrator.
                    raise LookupError("generation job not found")
                return current
            session.expire(job)
            session.refresh(job)
            idem = session.scalar(
                select(GenerationIdempotency).where(GenerationIdempotency.generation_job_id == job.id)
            )
            if idem:
                idem.provider_job_id = provider_job_id
            if self.workspace_credits is not None:
                self.workspace_credits.record_submission_confirmed(
                    session,
                    job,
                    attempt=max(job.attempt_count, 1),
                    provider_job_id=provider_job_id,
                )
            self._event(session, job.id, "ORPHAN_RESPONSE_RECOVERED", provider_job_id=provider_job_id)
            session.flush()
            return job

    def reconcile_credits(
        self,
        job_id: str,
        *,
        action: str,
        idempotency_key: str,
        reason: str,
        evidence_reference: str | None = None,
    ) -> WorkspaceCreditTransition:
        """Resolve one ambiguous reservation through the internal service boundary."""

        if self.workspace_credits is None:
            raise RuntimeError("workspace credit service is not configured")
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                raise LookupError("generation job not found")
            if action == "REFUND_RESERVED":
                terminalized = session.execute(
                    update(GenerationJob)
                    .where(
                        GenerationJob.id == job.id,
                        GenerationJob.status.in_(
                            [
                                JobStatus.FAILED.value,
                                JobStatus.CANCELLED.value,
                                JobStatus.WORKER_NEEDS_USER_ACTION.value,
                            ]
                        ),
                        GenerationJob.claim_token.is_(None),
                        GenerationJob.output_asset_id.is_(None),
                    )
                    .values(
                        status=case(
                            (
                                GenerationJob.status == JobStatus.WORKER_NEEDS_USER_ACTION.value,
                                JobStatus.FAILED.value,
                            ),
                            else_=GenerationJob.status,
                        ),
                        safe_to_retry=False,
                        next_retry_at=None,
                        claim_token=None,
                        claim_expires_at=None,
                    )
                )
                if not self._updated_one_row(terminalized):
                    raise WorkspaceCreditConflict(
                        "refund reconciliation requires an inactive terminal or isolated generation"
                    )
                session.expire(job)
                session.refresh(job)
                if not job.provider_job_id:
                    job.submission_state = "NOT_SENT"
                self.scheduler.release_job_in_session(
                    session,
                    job.id,
                    success=False,
                    error="credit reconciliation confirmed provider job was not billable",
                    clear_routing=True,
                )
            elif action == "SETTLE_RESERVED":
                settlement_fenced = session.execute(
                    update(GenerationJob)
                    .where(
                        GenerationJob.id == job.id,
                        GenerationJob.status.in_(
                            [
                                JobStatus.SUBMITTED.value,
                                JobStatus.RUNNING.value,
                                JobStatus.WORKER_NEEDS_USER_ACTION.value,
                                JobStatus.FAILED.value,
                                JobStatus.CANCELLED.value,
                                JobStatus.COMPLETED.value,
                            ]
                        ),
                        GenerationJob.submission_state.in_(["SENT_UNCONFIRMED", "CONFIRMED"]),
                        GenerationJob.claim_token.is_(None),
                    )
                    .values(safe_to_retry=False)
                )
                if not self._updated_one_row(settlement_fenced):
                    raise WorkspaceCreditConflict(
                        "settlement reconciliation requires an inactive submitted or terminal generation"
                    )
                session.expire(job)
                session.refresh(job)
            transition = self.workspace_credits.reconcile_generation(
                session,
                job,
                action=action,  # type: ignore[arg-type]
                idempotency_key=idempotency_key,
                reason=reason,
                evidence_reference=evidence_reference,
            )
            if transition.applied and not transition.replayed:
                job.safe_to_retry = False
                session.add(
                    DecisionRecord(
                        project_id=job.project_id,
                        shot_id=job.shot_id,
                        decision_type="WORKSPACE_CREDIT_RECONCILIATION",
                        input_features={
                            "generation_job_id": job.id,
                            "previous_credit_status": transition.previous_status,
                            "reserved_credits": transition.reserved_credits,
                            "evidence_reference": evidence_reference,
                            "server_actor": "PLATFORM_API_KEY",
                            "idempotency_key": idempotency_key,
                        },
                        selected_action=action,
                        reason_codes=[reason[:240]],
                        model_version="manual-credit-reconcile-v1",
                        policy_version=self.workspace_credits.pricing_version,
                    )
                )
                self._event(
                    session,
                    job.id,
                    "CREDIT_RECONCILED",
                    action=action,
                    previous_status=transition.previous_status,
                    status=transition.status,
                    reserved_credits=transition.reserved_credits,
                )
            session.flush()
            return transition

    async def _provider_reference_url(
        self,
        job: GenerationJob,
        asset_id: str,
        provider: GenerationProvider,
    ) -> str:
        """Resolve a fetchable URL for a provider that never ingests uploads.

        Resolution may derive a rendition on the spot — for video that is an
        ffmpeg transcode taking real wall time — so it runs in a worker thread
        rather than on the event loop, where it would stall every other job
        this gateway is processing.
        """

        try:
            return await asyncio.to_thread(
                self.media.reference_url,
                asset_id,
                project_id=job.project_id,
                provider=provider.name,
                require_https=self.provider_mode is ProviderMode.LIVE,
                # The provider's own declared limits decide which encoding it is
                # given. Nothing here re-encodes the user's original.
                constraints=getattr(provider, "reference_constraints", None),
            )
        except ProviderReferenceUrlUnavailable as exc:
            raise ProviderError(
                str(exc),
                RetryCategory.INVALID_REQUEST,
                code="PROVIDER_REFERENCE_URL_UNAVAILABLE",
                submitted=False,
            ) from exc

    async def _resolve_assets(
        self,
        job: GenerationJob,
        request: dict[str, Any],
        provider: GenerationProvider,
        *,
        provider_project_id: str | None,
    ) -> dict[str, Any]:
        result = dict(request)
        provider_payload = result.pop("provider_payload", None)
        if not isinstance(provider_payload, dict):
            provider_payload = {}
        if not provider_payload:
            # Compatibility for jobs created before provider_payload became a
            # first-class generation-request field.
            metadata = request.get("metadata")
            legacy_payload = metadata.get("adapter_payload") if isinstance(metadata, dict) else None
            if isinstance(legacy_payload, dict):
                provider_payload = legacy_payload
        resolved_asset_ids: dict[str, str] = {}

        def on_paid_boundary(session: Session, binding_id: str, asset_id: str) -> None:
            self._begin_asset_upload_boundary(
                session,
                job_id=job.id,
                claim_token=job.claim_token,
                binding_id=binding_id,
                asset_id=asset_id,
                provider=provider.name,
            )

        # A provider either ingests uploads and returns durable media IDs, or it
        # fetches references itself and therefore needs a real URL. Sending one
        # kind where the other is expected submits an unresolvable reference, so
        # the mode decides how every asset in this request is resolved.
        url_mode = (
            getattr(provider, "reference_mode", ProviderReferenceMode.PROVIDER_MEDIA_ID)
            is ProviderReferenceMode.FETCHABLE_URL
        )
        pairs = [
            ("start_frame_asset_id", "start_frame_provider_media_id", "start_frame_url"),
            ("end_frame_asset_id", "end_frame_provider_media_id", "end_frame_url"),
        ]
        for source, media_target, url_target in pairs:
            if not request.get(source):
                continue
            asset_id = str(request[source])
            if url_mode:
                reference = await self._provider_reference_url(job, asset_id, provider)
                result[url_target] = reference
                resolved_asset_ids[asset_id] = reference
                with self.database.session() as session:
                    self._event(
                        session,
                        job.id,
                        "ASSET_REFERENCE_URL_RESOLVED",
                        asset_id=asset_id,
                        provider=provider.name,
                    )
                continue
            media_id, reused = await self.media.resolve_provider_media(
                asset_id,
                provider,
                project_id=job.project_id,
                account_id=job.account_id,
                worker_id=job.worker_id,
                provider_project_id=provider_project_id,
                on_paid_boundary=on_paid_boundary,
            )
            result[media_target] = media_id
            resolved_asset_ids[asset_id] = media_id
            with self.database.session() as session:
                self._event(
                    session,
                    job.id,
                    "ASSET_RESOLVED" if reused else "ASSET_UPLOADED",
                    asset_id=asset_id,
                    provider_media_id=media_id,
                    reused=reused,
                )
        provider_references = []
        for raw_asset_id in request.get("reference_asset_ids") or []:
            asset_id = str(raw_asset_id)
            if url_mode:
                reference = await self._provider_reference_url(job, asset_id, provider)
                provider_references.append(reference)
                resolved_asset_ids[asset_id] = reference
                with self.database.session() as session:
                    self._event(
                        session,
                        job.id,
                        "ASSET_REFERENCE_URL_RESOLVED",
                        asset_id=asset_id,
                        provider=provider.name,
                    )
                continue
            media_id, reused = await self.media.resolve_provider_media(
                asset_id,
                provider,
                project_id=job.project_id,
                account_id=job.account_id,
                worker_id=job.worker_id,
                provider_project_id=provider_project_id,
                on_paid_boundary=on_paid_boundary,
            )
            provider_references.append(media_id)
            resolved_asset_ids[asset_id] = media_id
            with self.database.session() as session:
                self._event(
                    session,
                    job.id,
                    "ASSET_RESOLVED" if reused else "ASSET_UPLOADED",
                    asset_id=asset_id,
                    provider_media_id=media_id,
                    reused=reused,
                )
        if url_mode:
            result["reference_urls"] = provider_references
        else:
            result["reference_provider_media_ids"] = provider_references

        def resolve_payload_assets(value: Any) -> Any:
            if isinstance(value, str):
                return resolved_asset_ids.get(value, value)
            if isinstance(value, list):
                return [resolve_payload_assets(item) for item in value]
            if isinstance(value, tuple):
                return [resolve_payload_assets(item) for item in value]
            if isinstance(value, dict):
                return {key: resolve_payload_assets(item) for key, item in value.items()}
            return value

        # Routing, billing, ownership, and canonical prompt fields remain
        # Gateway-authoritative. The adapter may only add provider transport
        # parameters such as first-frame aliases, references, resolution, or
        # audio controls.
        protected_fields = {
            "project_id",
            "shot_id",
            "candidate_id",
            "type",
            "provider",
            "model",
            "prompt",
            "negative_prompt",
            "duration",
            "aspect_ratio",
            "start_frame_asset_id",
            "end_frame_asset_id",
            "reference_asset_ids",
            "start_frame_provider_media_id",
            "end_frame_provider_media_id",
            "reference_provider_media_ids",
            "start_frame_url",
            "end_frame_url",
            "reference_urls",
            "idempotency_key",
            "priority",
            "generation_policy",
            "asset_criticality",
            "cost_estimate",
            "metadata",
        }
        resolved_provider_payload = resolve_payload_assets(provider_payload)
        for key, value in resolved_provider_payload.items():
            if key not in protected_fields and not key.startswith("_"):
                result[key] = value
        result["_generation_job_id"] = job.id
        return result

    def _begin_asset_upload_boundary(
        self,
        session: Session,
        *,
        job_id: str,
        claim_token: str | None,
        binding_id: str,
        asset_id: str,
        provider: str,
    ) -> None:
        """Atomically fence wallet retry/refund before a provider asset upload."""

        now = utcnow()
        job = session.scalar(
            select(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.status == JobStatus.RESERVED.value,
                GenerationJob.claim_token == claim_token,
                GenerationJob.claim_expires_at > now,
                GenerationJob.submission_state.in_(["NOT_SENT", "SENT_UNCONFIRMED"]),
                GenerationJob.provider_job_id.is_(None),
            )
            .with_for_update()
        )
        if job is None:
            raise ProviderError(
                "generation claim expired before provider asset upload",
                RetryCategory.PERMANENT_ERROR,
                code="ASSET_UPLOAD_CLAIM_LOST",
            )
        if job.provider != provider:
            raise ProviderError(
                "provider asset upload does not match the server-selected generation target",
                RetryCategory.PERMANENT_ERROR,
                code="PROVIDER_TARGET_CHANGED",
            )
        self._require_live_generation_fence_boundary(
            job_id=job.id,
            provider=job.provider,
            model=job.model,
            session=session,
        )
        if provider == FLOW_PROVIDER:
            if not job.account_id or not job.provider_project_id:
                raise ProviderError(
                    "Google Flow asset upload is missing its persisted account/project affinity",
                    RetryCategory.PERMANENT_ERROR,
                    code="FLOW_AFFINITY_IDENTITY_MISSING",
                )
            ready_binding = session.scalar(
                select(ProviderProjectBinding.id).where(
                    ProviderProjectBinding.local_project_id == job.project_id,
                    ProviderProjectBinding.provider == FLOW_PROVIDER,
                    ProviderProjectBinding.provider_account_id == job.account_id,
                    ProviderProjectBinding.provider_project_id == job.provider_project_id,
                    ProviderProjectBinding.status == ProviderProjectBindingStatus.READY.value,
                )
            )
            if ready_binding is None:
                raise ProviderError(
                    "Google Flow affinity changed before provider asset upload",
                    RetryCategory.PERMANENT_ERROR,
                    code="FLOW_AFFINITY_CHANGED",
                    submitted=job.submission_state != "NOT_SENT",
                )
        first_boundary = job.submission_state == "NOT_SENT"
        if first_boundary:
            job.submission_state = "SENT_UNCONFIRMED"
            job.safe_to_retry = False
            if self.workspace_credits is not None:
                self.workspace_credits.record_submission_boundary(
                    session,
                    job,
                    attempt=max(job.attempt_count, 1),
                )
        self._event(
            session,
            job.id,
            "ASSET_UPLOAD_SUBMISSION_STARTED",
            provider=provider,
            asset_id=asset_id,
            binding_id=binding_id,
            first_paid_boundary=first_boundary,
        )
        session.flush()

    @staticmethod
    def _allocate_sibling_candidates_in(
        session: Session, shot_id: str, count: int
    ) -> list[tuple[str, int]]:
        """Create one candidate row per extra image, inside the caller's transaction.

        The workspace asked for N images and paid for N, so it gets N things it
        can choose between. The rows are created in the completion transaction
        beside the media they own — never ahead of it: a pre-created empty
        CREATED row was a promise the process might die before keeping, and an
        orphaned promise is indistinguishable from a live one.

        The Shot row is locked first, the same lock order candidate creation
        uses, so attempt numbers stay unique against a concurrent generation
        request on the same shot.
        """

        with session.no_autoflush:
            session.execute(update(Shot).where(Shot.id == shot_id).values(updated_at=Shot.updated_at))
            highest = int(
                session.scalar(
                    select(func.coalesce(func.max(GenerationCandidate.attempt_number), 0)).where(
                        GenerationCandidate.shot_id == shot_id
                    )
                )
                or 0
            )
        allocated: list[tuple[str, int]] = []
        for offset in range(1, count + 1):
            candidate = GenerationCandidate(
                id=str(uuid.uuid4()),
                shot_id=shot_id,
                attempt_number=highest + offset,
                status=CandidateStatus.VALIDATING.value,
                metadata_json={"batch_index": offset},
            )
            session.add(candidate)
            allocated.append((candidate.id, offset))
        session.flush()
        return allocated

    async def _stage_provider_outputs(
        self,
        job_id: str,
        *,
        provider_name: str,
        provider_job_id: str,
        capability: str,
        asset_type: str,
        result: ProviderJob,
    ) -> tuple[StagedProviderOutput, list[tuple[int, StagedProviderOutput]]]:
        """Validate every provider artefact and write it to its staging slot.

        Nothing here touches the database beyond append-only events. The slot
        keys are a pure function of the job and its provider attempt, so a
        crashed attempt that re-runs overwrites its own slots instead of
        leaving new orphans, and the completion transaction can be replayed
        against the same staged bytes.

        The job's own artefact (output 1) must stage or the poll fails as it
        always did; a rejected extra image is recorded and skipped, never
        allowed to fail the generation that already paid for its siblings.
        """

        key_prefix = generation_staging_prefix(job_id, provider_job_id)
        if result.outputs:
            primary = self.media.stage_inline_provider_output(
                result.outputs[0].content,
                key_prefix=key_prefix,
                index=0,
                stem=job_id,
                mime_type=result.outputs[0].mime_type,
                asset_type=asset_type,
            )
            extras: list[tuple[int, StagedProviderOutput]] = []
            for index, output in enumerate(result.outputs[1:], start=1):
                try:
                    staged = self.media.stage_inline_provider_output(
                        output.content,
                        key_prefix=key_prefix,
                        index=index,
                        stem=f"{job_id}-{index}",
                        mime_type=output.mime_type,
                        asset_type=asset_type,
                    )
                except Exception as exc:
                    with self.database.session() as session:
                        self._event(
                            session,
                            job_id,
                            "MEDIA_ERROR",
                            stage="batch_sibling",
                            batch_index=index,
                            error=str(exc),
                        )
                    continue
                extras.append((index, staged))
        else:
            assert result.output_url is not None
            suffix = "mp4" if capability == "video" else "png"
            primary = await self.media.download_provider_output_to_staging(
                result.output_url,
                key_prefix=key_prefix,
                index=0,
                filename=f"{job_id}.{suffix}",
                provider=provider_name,
                asset_type=asset_type,
            )
            extras = []
        with self.database.session() as session:
            self._event(
                session,
                job_id,
                "MEDIA_STAGED",
                key_prefix=key_prefix,
                staged_count=1 + len(extras),
            )
        return primary, extras

    def _finalize_completed_generation(
        self,
        job_id: str,
        *,
        claim_token: str,
        provider_job_id: str,
        poll_fence_conditions: list[Any],
        provider_name: str,
        project_id: str,
        shot_id: str | None,
        candidate_id: str | None,
        asset_type: str,
        primary: StagedProviderOutput,
        extras: list[tuple[int, StagedProviderOutput]],
        raw: dict[str, Any] | None,
    ) -> GenerationJob | None:
        """Adopt the staged artefacts and complete the job — one transaction.

        Media rows, sibling candidate rows, every job binding, billing
        evidence, credit settlement and the terminal status commit or roll
        back together. The claim fence at the top is what makes repeated
        execution safe: a second finalize of the same job matches zero rows
        and returns ``None`` without creating anything, and a rolled-back
        attempt leaves only unreferenced staging objects for the TTL sweeper.
        """

        with self.database.session() as session:
            completed = session.execute(
                update(GenerationJob)
                .where(
                    GenerationJob.id == job_id,
                    GenerationJob.status == JobStatus.RESERVED.value,
                    GenerationJob.claim_token == claim_token,
                    GenerationJob.provider_job_id == provider_job_id,
                    GenerationJob.submission_state == "CONFIRMED",
                    GenerationJob.output_asset_id.is_(None),
                    *poll_fence_conditions,
                )
                .values(
                    status=JobStatus.COMPLETED.value,
                    safe_to_retry=False,
                    next_retry_at=None,
                    claim_token=None,
                    claim_expires_at=None,
                    completed_at=utcnow(),
                )
            )
            if not self._updated_one_row(completed):
                return None
            job = session.get(GenerationJob, job_id)
            if job is None:  # pragma: no cover - guarded by the conditional update.
                raise LookupError("generation job not found")
            session.refresh(job)
            asset, _reused = self.media.adopt_staged_output_in(
                session,
                project_id,
                asset_type,
                primary,
                provider=provider_name,
                provider_media_id=provider_job_id,
                shot_id=shot_id,
                generation_candidate_id=candidate_id,
            )
            job.output_asset_id = asset.id
            # The artefact is in the media plane now, so the inbox copy has
            # done its job. Same transaction as the completion, so the bytes
            # are never dropped by a completion that rolls back.
            self._discard_synchronous_result(session, job_id)
            self._record_provider_billing_evidence(
                session,
                job,
                provider_job_id=provider_job_id,
                raw=raw,
            )
            if self.workspace_credits is not None:
                credit_settlement = self.workspace_credits.settle_generation(
                    session,
                    job,
                    reason="GENERATION_COMPLETED",
                )
                if credit_settlement.applied and not credit_settlement.replayed:
                    self._event(
                        session,
                        job.id,
                        "CREDIT_SETTLED",
                        credits=credit_settlement.settled_credits,
                    )
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
            sibling_summary: list[tuple[str, str]] = []
            if extras:
                reserved: list[tuple[str, int]] = []
                if shot_id and candidate_id:
                    reserved = self._allocate_sibling_candidates_in(session, shot_id, len(extras))
                for position, (batch_index, staged) in enumerate(extras):
                    sibling_candidate_id = reserved[position][0] if position < len(reserved) else None
                    sibling_asset, _sibling_reused = self.media.adopt_staged_output_in(
                        session,
                        project_id,
                        asset_type,
                        staged,
                        provider=provider_name,
                        provider_media_id=f"{provider_job_id}#{batch_index}",
                        shot_id=shot_id,
                        generation_candidate_id=sibling_candidate_id,
                        metadata={"batch_index": batch_index, "generation_job_id": job.id},
                    )
                    if sibling_candidate_id is not None:
                        sibling = session.get(GenerationCandidate, sibling_candidate_id)
                        if sibling is not None:
                            sibling.generation_job_id = job.id
                            sibling.output_asset_id = sibling_asset.id
                    sibling_summary.append((sibling_candidate_id or "", sibling_asset.id))
            self._event(session, job.id, "MEDIA_DOWNLOADED", asset_id=asset.id)
            if sibling_summary:
                self._event(
                    session,
                    job.id,
                    "MEDIA_BATCH_SIBLINGS_REGISTERED",
                    asset_ids=[asset_id for _candidate, asset_id in sibling_summary],
                    candidate_ids=[candidate for candidate, _asset in sibling_summary if candidate],
                )
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
            session.flush()
            return job

    def _require_job(self, job_id: str) -> GenerationJob:
        job = self.get(job_id)
        if job is None:  # pragma: no cover - deleted concurrently by an administrator.
            raise LookupError("generation job not found")
        return job

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
            job = session.get(GenerationJob, job_id)
            if job is not None and self.workspace_credits is not None:
                self.workspace_credits.require_reconciliation(
                    session,
                    job,
                    reason="SUBMISSION_CLAIM_EXPIRED",
                )
            self._event(session, job_id, "SUBMISSION_CLAIM_EXPIRED", submitted=True)
        return self.get(job_id)

    def _hold_synchronous_result(
        self,
        session: Session,
        job: GenerationJob,
        *,
        provider_job_id: str,
        result: ProviderJob,
    ) -> None:
        """Persist a synchronous provider's result inside the caller's transaction.

        The caller is the transaction that confirms the submission, which is
        what makes this durable at exactly the moment the workspace becomes
        liable for the call.
        """

        # A retry of the same job reaches here with a new provider job id. The
        # previous row is a result for a submission this attempt did not make,
        # so it is replaced rather than kept alongside.
        stale = session.scalar(
            select(ProviderSynchronousResult).where(ProviderSynchronousResult.generation_job_id == job.id)
        )
        if stale is not None:
            session.delete(stale)
            session.flush()
        held = ProviderSynchronousResult(
            generation_job_id=job.id,
            provider_job_id=provider_job_id,
            attempt_number=max(job.attempt_count, 1),
            status=result.status,
            progress=float(result.progress or 0),
            output_url=result.output_url,
            output_mime_type=result.output_mime_type,
            error=result.error,
            raw_json=dict(result.raw or {}),
        )
        session.add(held)
        session.flush()
        for ordinal, output in enumerate(result.outputs):
            session.add(
                ProviderSynchronousResultOutput(
                    result_id=held.id,
                    ordinal=ordinal,
                    mime_type=output.mime_type,
                    content=output.content,
                    content_sha256=hashlib.sha256(output.content).hexdigest(),
                )
            )
        session.flush()

    def _read_synchronous_result(self, job_id: str, provider_job_id: str) -> ProviderJob | None:
        """Read the held result for one provider job, leaving the row in place.

        Reading must not consume it. The row is what makes the window between a
        confirmed submission and a committed completion survivable, and that
        window does not close until the completion commits — so it is
        `_discard_synchronous_result`, inside the transaction that marks the job
        terminal, that removes it. A poll that reads the bytes and then dies
        finds them still there on the next attempt.

        A row whose `provider_job_id` does not match belongs to an earlier
        attempt. It is discarded, never returned — a result for a submission
        this poll did not make is not a result this poll may complete.
        """

        with self.database.session() as session:
            held = session.scalar(
                select(ProviderSynchronousResult).where(ProviderSynchronousResult.generation_job_id == job_id)
            )
            if held is None:
                return None
            outputs: list[ProviderInlineOutput] = []
            if held.provider_job_id == provider_job_id:
                rows = session.scalars(
                    select(ProviderSynchronousResultOutput)
                    .where(ProviderSynchronousResultOutput.result_id == held.id)
                    .order_by(ProviderSynchronousResultOutput.ordinal)
                ).all()
                for row in rows:
                    content = bytes(row.content)
                    if hashlib.sha256(content).hexdigest() != row.content_sha256:
                        # Storage handed back bytes that are not what was
                        # stored. Completing on them would publish a corrupt
                        # artefact as a paid result; failing here sends the
                        # credit to reconciliation instead.
                        raise ProviderError(
                            f"held synchronous output {row.ordinal} failed its digest check",
                            RetryCategory.PERMANENT_ERROR,
                            code="SYNCHRONOUS_RESULT_CORRUPT",
                            # The provider was called and the workspace billed;
                            # the credit must go to reconciliation, not refund.
                            submitted=True,
                        )
                    outputs.append(ProviderInlineOutput(content=content, mime_type=row.mime_type))
                result = ProviderJob(
                    provider_job_id=held.provider_job_id,
                    status=held.status,
                    progress=held.progress,
                    output_url=held.output_url,
                    output_mime_type=held.output_mime_type,
                    error=held.error,
                    raw=dict(held.raw_json or {}),
                    outputs=outputs,
                )
                return result
            # Stale: an earlier attempt's result, which no poll may ever use.
            session.delete(held)
            return None

    def _discard_synchronous_result(self, session: Session, job_id: str) -> None:
        """Drop a held result inside the transaction that ends the job.

        Called from every terminal transition, not only the successful one: a
        job that failed or was cancelled will never consume its held bytes, and
        several megabytes per job is not something to leave to the job row's
        eventual cascade.
        """

        held = session.scalar(
            select(ProviderSynchronousResult).where(ProviderSynchronousResult.generation_job_id == job_id)
        )
        if held is not None:
            session.delete(held)

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
        target_fence_lost = False
        flow_affinity_fence_lost = False
        flow_boundary_submitted = False
        with self.database.session() as session:
            boundary_conditions = [
                GenerationJob.id == job_id,
                GenerationJob.status == JobStatus.RESERVED.value,
                GenerationJob.claim_token == claim_token,
                GenerationJob.claim_expires_at > now,
                GenerationJob.submission_state.in_(["NOT_SENT", "SENT_UNCONFIRMED"]),
            ]
            boundary_job = session.scalar(select(GenerationJob).where(*boundary_conditions).with_for_update())
            if boundary_job is None:
                return False
            project = session.get(Project, boundary_job.project_id)
            workspace_scoped = bool(project and project.workspace_id)
            provider_name = boundary_job.provider
            model = boundary_job.model
            media_type = boundary_job.generation_type
            if provider_name != provider:
                raise GenerationTargetError(
                    "PROVIDER_TARGET_CHANGED",
                    "provider implementation no longer matches the server-selected generation target",
                )
            self._require_live_generation_fence_boundary(
                job_id=boundary_job.id,
                provider=provider_name,
                model=model,
                session=session,
            )
            flow_binding_predicate = None
            if provider_name == FLOW_PROVIDER:
                expected_project_id = str(provider_request.get("_provider_project_id") or "").strip()
                if (
                    not boundary_job.account_id
                    or not boundary_job.provider_project_id
                    or boundary_job.provider_project_id != expected_project_id
                ):
                    raise ProviderError(
                        "Google Flow submission is missing its persisted account/project affinity",
                        RetryCategory.PERMANENT_ERROR,
                        code="FLOW_AFFINITY_IDENTITY_MISSING",
                        submitted=boundary_job.submission_state != "NOT_SENT",
                    )
                flow_boundary_submitted = boundary_job.submission_state != "NOT_SENT"
                flow_binding_predicate = select(ProviderProjectBinding.id).where(
                    ProviderProjectBinding.local_project_id == boundary_job.project_id,
                    ProviderProjectBinding.provider == FLOW_PROVIDER,
                    ProviderProjectBinding.provider_account_id == boundary_job.account_id,
                    ProviderProjectBinding.provider_project_id == boundary_job.provider_project_id,
                    ProviderProjectBinding.status == ProviderProjectBindingStatus.READY.value,
                )
                locked_binding = session.scalar(flow_binding_predicate.with_for_update())
                if locked_binding is None:
                    raise ProviderError(
                        "Google Flow affinity changed before provider generation submission",
                        RetryCategory.PERMANENT_ERROR,
                        code="FLOW_AFFINITY_CHANGED",
                        submitted=flow_boundary_submitted,
                    )
            # Validate and lock the persistent model switch in the same
            # transaction that closes the durable paid-call boundary. The
            # EXISTS predicate also fences databases where row locks are not
            # available (notably SQLite tests).
            definition = self._validate_persisted_generation_target(
                provider_name,
                model,
                media_type,
                workspace_scoped=workspace_scoped,
                session=session,
                lock_for_update=True,
            )
            update_conditions: list[Any] = list(boundary_conditions)
            if definition is not None:
                capability_field = getattr(
                    ModelCapabilityProfile,
                    f"supports_{media_type}_generation",
                )
                eligible_definition = select(ModelDefinition.id).where(
                    ModelDefinition.id == definition.definition_id,
                    ModelDefinition.provider == definition.provider,
                    ModelDefinition.provider_model_id == definition.provider_model_id,
                    ModelDefinition.modality == definition.modality,
                    ModelDefinition.enabled.is_(True),
                    ModelDefinition.lifecycle_status.not_in(("DISABLED", "BLOCKED")),
                )
                if self.provider_mode is ProviderMode.LIVE:
                    eligible_definition = eligible_definition.where(ModelDefinition.live_enabled.is_(True))
                update_conditions.append(eligible_definition.exists())
                update_conditions.append(
                    select(ModelCapabilityProfile.model_definition_id)
                    .where(
                        ModelCapabilityProfile.model_definition_id == definition.definition_id,
                        ModelCapabilityProfile.profile_version == definition.capability_profile_version,
                        capability_field.is_(True),
                    )
                    .exists()
                )
            if flow_binding_predicate is not None:
                update_conditions.append(flow_binding_predicate.exists())
            result = session.execute(
                update(GenerationJob)
                .where(*update_conditions)
                .values(
                    provider_request_json=provider_request,
                    submission_state="SENT_UNCONFIRMED",
                    safe_to_retry=False,
                )
                .execution_options(synchronize_session=False)
            )
            if not self._updated_one_row(result):
                boundary_still_owned = (
                    session.scalar(select(GenerationJob.id).where(*boundary_conditions)) is not None
                )
                if flow_binding_predicate is not None and boundary_still_owned:
                    flow_affinity_fence_lost = session.scalar(flow_binding_predicate) is None
                target_fence_lost = (
                    not flow_affinity_fence_lost and definition is not None and boundary_still_owned
                )
            else:
                session.expire(boundary_job)
                session.refresh(boundary_job)
                job = boundary_job
                if self.workspace_credits is not None:
                    self.workspace_credits.record_submission_boundary(
                        session,
                        job,
                        attempt=max(job.attempt_count, 1),
                    )
                self._event(session, job_id, "REQUEST_SUBMITTED", provider=provider)
        if flow_affinity_fence_lost:
            raise ProviderError(
                "Google Flow affinity changed at the paid-call boundary",
                RetryCategory.PERMANENT_ERROR,
                code="FLOW_AFFINITY_CHANGED",
                submitted=flow_boundary_submitted,
            )
        if target_fence_lost:
            # Resolve again after the failed atomic predicate so callers receive
            # the authoritative MODEL_DISABLED / MODEL_LIVE_DISABLED failure.
            self._validate_persisted_generation_target(
                provider_name,
                model,
                media_type,
                workspace_scoped=workspace_scoped,
            )
            raise GenerationTargetError(
                "MODEL_TARGET_CHANGED",
                f"server model definition changed at the submission boundary: {provider_name}:{model}",
            )
        if not self._updated_one_row(result):
            return False
        return True

    async def _submit(self, job_id: str, claim_token: str) -> GenerationJob:
        attempts_exhausted = False
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
                attempts_exhausted = True
            else:
                request = dict(job.request_json)
                request_metadata = dict(request.get("metadata") or {})
                request_metadata.pop(TIMELINE_FENCE_METADATA_KEY, None)
                request["metadata"] = request_metadata
                # State the resolution this job was priced at. It is carried in
                # `metadata`, and every video adapter reads it from the top
                # level -- openrouter filters on VIDEO_REQUEST_FIELDS, wan reads
                # `request.get("resolution")`, seedance maps the same key -- so
                # it reached none of them and each fell back to a provider
                # default. That is a quote for one request and a bill for
                # another: a 2s alibaba/wan-3.0 clip priced at 480p (USD 0.05/s)
                # came back 1920x1080 with audio and cost USD 0.85, 8.5x the
                # estimate. The standing rule is that a parameter which decides
                # the bill is stated, never inherited.
                # Video only: an image is priced per image and quality level,
                # and the images APIs either reject the field (OpenRouter,
                # 2026-09-02: `invalid_value` on `resolution`) or ignore it.
                if job.generation_type == "video" and not request.get("resolution"):
                    priced_resolution = str(request_metadata.get("resolution") or "").strip()
                    if priced_resolution:
                        request["resolution"] = priced_resolution
                capability = job.generation_type
                provider_name = job.provider
                model = job.model
                project_id = job.project_id
                priority = job.priority
                project = session.get(Project, job.project_id)
                workspace_scoped = bool(project and project.workspace_id)
        if attempts_exhausted:
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                "MAX_ATTEMPTS_EXHAUSTED",
                "generation exhausted its maximum pre-submit attempts",
                submitted=False,
                claim_token=claim_token,
            )
        try:
            provider = self.providers.validate_target(provider_name, model, capability)
            self._validate_persisted_generation_target(
                provider_name,
                model,
                capability,
                workspace_scoped=workspace_scoped,
            )
        except GenerationTargetError as exc:
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                exc.code,
                str(exc),
                submitted=False,
                claim_token=claim_token,
            )
        live_fence: LiveGenerationFence | None = None
        try:
            live_fence = self._reserve_live_generation_fence(
                job_id=job_id,
                provider=provider_name,
                model=model,
                media_type=capability,
            )
        except LiveCanaryResubmissionForbidden as exc:
            # This operation's usage is already UNCERTAIN or SETTLED: a paid
            # boundary may have been crossed. Only an operator can resolve it;
            # automatic retry is exactly what the refusal forbids.
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                "LIVE_CANARY_DENIED",
                str(exc),
                submitted=False,
                claim_token=claim_token,
            )
        except ProductionBudgetExceeded as exc:
            # The platform or provider breaker is out of room for this
            # window. Same shape as a refused permit: nothing was spent, and
            # the job waits for the window to roll or the ceiling to move.
            return self._schedule_error(
                job_id,
                RetryCategory.RATE_LIMIT,
                "PRODUCTION_BUDGET_EXCEEDED",
                str(exc),
                submitted=False,
                claim_token=claim_token,
            )
        except LiveSpendDenied as exc:
            # A refused reservation is a spending fence doing its job before
            # any boundary is crossed — the same shape as "no ready account".
            # It resolves when an operator mints a permit or a settlement
            # frees held budget, so the job waits instead of dying: RETRY_WAIT
            # with backoff, never a refunded terminal FAILED that forces the
            # user to rebuild the whole action (2026-08-30 audit, H6).
            return self._schedule_error(
                job_id,
                RetryCategory.RATE_LIMIT,
                "LIVE_CANARY_DENIED",
                str(exc),
                submitted=False,
                claim_token=claim_token,
            )
        except (LiveCanaryConflict, SpendAuthorizationConflict) as exc:
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                "LIVE_CANARY_CONFLICT",
                str(exc),
                submitted=False,
                claim_token=claim_token,
            )
        canary_boundary_crossed = False
        submission_boundary_crossed = False
        flow_affinity_existed = True
        try:
            flow_binding_id = None
            if provider_name == FLOW_PROVIDER:
                flow_affinity_existed = self.flow_affinity.binding_for_project(project_id) is not None
                # With no existing affinity, acquisition may call the remote
                # create-project provisioner. The canary is already UNCERTAIN.
                canary_boundary_crossed = not flow_affinity_existed
                affinity = await self.flow_affinity.acquire_for_generation(
                    local_project_id=project_id,
                    capability=capability,
                    model=model,
                    priority=priority,
                    generation_job_id=job_id,
                    claim_token=claim_token,
                )
                account = affinity.provider_account
                worker = affinity.worker
                provider_project_id = affinity.provider_project_id
                flow_binding_id = affinity.binding_id
            else:
                account, worker = self.scheduler.select_account(
                    provider_name,
                    capability,
                    model,
                    priority,
                    project_id=project_id,
                    generation_job_id=job_id,
                    claim_token=claim_token,
                )
                provider_project_id = None
        except FlowAffinityUnavailable as exc:
            if flow_affinity_existed or exc.code in {
                "FLOW_PROVISIONING_ACCOUNT_UNAVAILABLE",
                "FLOW_PROVISIONING_UNAVAILABLE",
            }:
                canary_boundary_crossed = False
            self._release_live_generation_fence_before_boundary(
                live_fence,
                job_id=job_id,
                boundary_crossed=canary_boundary_crossed,
            )
            return self._schedule_error(
                job_id,
                (RetryCategory.PROVIDER_BUSY if exc.retryable else RetryCategory.PERMANENT_ERROR),
                exc.code,
                str(exc),
                submitted=False,
                claim_token=claim_token,
            )
        except FlowAffinityConflict as exc:
            self._release_live_generation_fence_before_boundary(
                live_fence,
                job_id=job_id,
                boundary_crossed=canary_boundary_crossed,
            )
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                "FLOW_AFFINITY_CONFLICT",
                str(exc),
                submitted=False,
                claim_token=claim_token,
            )
        except NoAccountAvailable as exc:
            self._release_live_generation_fence_before_boundary(
                live_fence,
                job_id=job_id,
                boundary_crossed=canary_boundary_crossed,
            )
            return self._schedule_error(
                job_id,
                RetryCategory.PROVIDER_BUSY,
                "NO_ACCOUNT",
                str(exc),
                submitted=False,
                claim_token=claim_token,
            )
        claim_lost = False
        affinity_lost = False
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if (
                not job
                or job.status != JobStatus.RESERVED.value
                or job.claim_token != claim_token
                or job.submission_state != "NOT_SENT"
            ):
                claim_lost = True
            else:
                if provider_name == FLOW_PROVIDER:
                    project_binding = session.scalar(
                        select(ProviderProjectBinding).where(
                            ProviderProjectBinding.id == flow_binding_id,
                            ProviderProjectBinding.local_project_id == job.project_id,
                            ProviderProjectBinding.provider == FLOW_PROVIDER,
                            ProviderProjectBinding.provider_account_id == account.id,
                            ProviderProjectBinding.provider_project_id == provider_project_id,
                            ProviderProjectBinding.status == ProviderProjectBindingStatus.READY.value,
                        )
                    )
                    affinity_lost = project_binding is None
                else:
                    project_binding = session.scalar(
                        select(ProviderProjectBinding).where(
                            ProviderProjectBinding.local_project_id == job.project_id,
                            ProviderProjectBinding.provider == job.provider,
                            ProviderProjectBinding.provider_account_id == account.id,
                            ProviderProjectBinding.status == ProviderProjectBindingStatus.READY.value,
                        )
                    )
                    provider_project_id = (
                        project_binding.provider_project_id
                        if project_binding
                        else account.metadata_json.get("project_id")
                    )
                if not affinity_lost:
                    job.account_id = account.id
                    job.worker_id = worker.id
                    job.provider_project_id = provider_project_id
                    job.started_at = job.started_at or utcnow()
                    job.reserved_at = utcnow()
                    job.claim_expires_at = utcnow() + timedelta(seconds=self.claim_lease_seconds)
                    job.attempt_count += 1
                    self._event(
                        session,
                        job.id,
                        "ACCOUNT_SELECTED",
                        account_id=account.id,
                        provider_project_id=provider_project_id,
                        flow_binding_id=flow_binding_id,
                        credits=account.credits,
                    )
                    self._event(session, job.id, "WORKER_SELECTED", worker_id=worker.id)
                    session.flush()
        if claim_lost or affinity_lost:
            self._release_live_generation_fence_before_boundary(
                live_fence,
                job_id=job_id,
                boundary_crossed=canary_boundary_crossed,
            )
            self.scheduler.release_job(
                job_id,
                success=None,
                error=("Flow affinity changed" if affinity_lost else "claim lost"),
                clear_routing=True,
            )
            if affinity_lost:
                return self._schedule_error(
                    job_id,
                    RetryCategory.PERMANENT_ERROR,
                    "FLOW_AFFINITY_CHANGED",
                    "Google Flow affinity changed before the generation boundary",
                    submitted=False,
                    claim_token=claim_token,
                )
            current = self.get(job_id)
            if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                raise LookupError("generation job not found")
            return current
        try:
            if provider_project_id:
                request["_provider_project_id"] = provider_project_id
            else:
                request.pop("_provider_project_id", None)
            current_job = self.get(job_id)
            if current_job is None:
                raise LookupError("generation job not found during asset resolution")
            if any(
                (
                    request.get("start_frame_asset_id"),
                    request.get("end_frame_asset_id"),
                    request.get("reference_asset_ids"),
                )
            ):
                # Asset resolution may validate or upload provider media. Both
                # are transport calls and therefore make failure ambiguous.
                canary_boundary_crossed = True
            request = await self._resolve_assets(
                current_job,
                request,
                provider,
                provider_project_id=provider_project_id,
            )
            if not self._begin_provider_submission(job_id, claim_token, request, provider.name):
                current = self.get(job_id)
                if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                    raise LookupError("generation job not found")
                if current.submission_state != "NOT_SENT":
                    return self._schedule_error(
                        job_id,
                        RetryCategory.PERMANENT_ERROR,
                        "ASSET_UPLOAD_BOUNDARY_CLAIM_LOST",
                        "generation claim expired after a provider asset upload boundary",
                        submitted=True,
                        claim_token=claim_token,
                    )
                self.scheduler.release_job(
                    job_id,
                    success=False,
                    error="submission claim expired or was superseded",
                    clear_routing=True,
                )
                self._release_live_generation_fence_before_boundary(
                    live_fence,
                    job_id=job_id,
                    boundary_crossed=canary_boundary_crossed,
                )
                return current
            submission_boundary_crossed = True
            canary_boundary_crossed = True
            if capability == "image":
                submission = await provider.generate_image(
                    request, account_id=account.id, worker_id=worker.id
                )
            else:
                submission = await provider.generate_video(
                    request, account_id=account.id, worker_id=worker.id
                )
            with self.database.session() as session:
                confirmation_conditions: list[Any] = [
                    GenerationJob.id == job_id,
                    GenerationJob.account_id == account.id,
                    GenerationJob.worker_id == worker.id,
                    GenerationJob.provider_job_id.is_(None),
                    GenerationJob.output_asset_id.is_(None),
                    GenerationJob.submission_state == "SENT_UNCONFIRMED",
                    or_(
                        and_(
                            GenerationJob.status == JobStatus.RESERVED.value,
                            GenerationJob.claim_token == claim_token,
                        ),
                        and_(
                            GenerationJob.status == JobStatus.WORKER_NEEDS_USER_ACTION.value,
                            GenerationJob.claim_token.is_(None),
                        ),
                    ),
                ]
                if provider_name == FLOW_PROVIDER:
                    confirmation_conditions.append(GenerationJob.provider_project_id == provider_project_id)
                confirmed = session.execute(
                    update(GenerationJob)
                    .where(*confirmation_conditions)
                    .values(
                        provider_job_id=submission.provider_job_id,
                        submission_state="CONFIRMED",
                        status=JobStatus.SUBMITTED.value,
                        safe_to_retry=False,
                        # A synchronous provider already holds the result, so
                        # there is nothing to wait for before reading it.
                        next_retry_at=None if submission.result else self._next_poll_at(),
                        claim_token=None,
                        claim_expires_at=None,
                        submitted_at=utcnow(),
                    )
                )
                if not self._updated_one_row(confirmed):
                    session.expire_all()
                    current = session.get(GenerationJob, job_id)
                    if current is None:
                        raise LookupError("generation job not found after provider submission")
                    if current.provider_job_id != submission.provider_job_id:
                        self._event(
                            session,
                            job_id,
                            "LATE_PROVIDER_RESPONSE_AFTER_TERMINAL",
                            provider_job_id=submission.provider_job_id,
                            status=current.status,
                            submission_state=current.submission_state,
                        )
                    return current
                job = session.get(GenerationJob, job_id)
                if job is None:  # pragma: no cover - guarded by conditional update.
                    raise LookupError("generation job not found after provider submission")
                remaining_credits = submission.raw.get("remainingCredits")
                if remaining_credits is not None:
                    provider_account = session.get(ProviderAccount, account.id)
                    if provider_account:
                        provider_account.credits = int(remaining_credits)
                idem = session.scalar(
                    select(GenerationIdempotency).where(GenerationIdempotency.generation_job_id == job.id)
                )
                if idem:
                    idem.provider_job_id = submission.provider_job_id
                if self.workspace_credits is not None:
                    self.workspace_credits.record_submission_confirmed(
                        session,
                        job,
                        attempt=max(job.attempt_count, 1),
                        provider_job_id=submission.provider_job_id,
                    )
                self._event(
                    session, job.id, "PROVIDER_JOB_STARTED", provider_job_id=submission.provider_job_id
                )
                if submission.result is not None:
                    # Same transaction as the confirmation: the artefact becomes
                    # durable exactly when the submission does, so there is no
                    # window in which the workspace has been billed for bytes
                    # that live only in this process.
                    self._hold_synchronous_result(
                        session,
                        job,
                        provider_job_id=submission.provider_job_id,
                        result=submission.result,
                    )
                session.flush()
            if submission.result is None:
                return self._require_job(job_id)
            # The provider answered synchronously and is already holding the
            # finished artefact. Hand the result to the ordinary poll rather
            # than duplicating completion, billing and settlement here.
            poll_claim = self._claim_for_polling(job_id)
            if poll_claim is None:
                # Another worker will poll it, and the held result is now
                # readable by that worker rather than only by this one.
                return self._require_job(job_id)
            return await self._poll(job_id, poll_claim)
        except GenerationTargetError as exc:
            self._release_live_generation_fence_before_boundary(
                live_fence,
                job_id=job_id,
                boundary_crossed=canary_boundary_crossed,
            )
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                exc.code,
                str(exc),
                submitted=False,
                claim_token=claim_token,
                release_reservation=True,
                release_error=str(exc),
                clear_routing=True,
            )
        except ProviderError as exc:
            # Once the durable paid-call fence has been crossed, an adapter's
            # local error classification may only make the outcome *more*
            # conservative. It must never move the job back to NOT_SENT or
            # authorize a refund/re-submit after provider execution began.
            effective_submitted = submission_boundary_crossed or exc.submitted
            self._release_live_generation_fence_before_boundary(
                live_fence,
                job_id=job_id,
                boundary_crossed=canary_boundary_crossed or effective_submitted,
            )
            return self._schedule_error(
                job_id,
                exc.category,
                exc.code,
                str(exc),
                submitted=effective_submitted,
                claim_token=claim_token,
                release_reservation=not effective_submitted,
                release_error=str(exc),
                clear_routing=not effective_submitted,
            )
        except Exception as exc:
            self._release_live_generation_fence_before_boundary(
                live_fence,
                job_id=job_id,
                boundary_crossed=canary_boundary_crossed,
            )
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
            provider_project_id = job.provider_project_id
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
        try:
            self._require_live_generation_fence_boundary(
                job_id=job_id,
                provider=provider_name,
                model=model,
                allow_settled=True,
            )
        except (LiveSpendDenied, LiveCanaryConflict, SpendAuthorizationConflict) as exc:
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                "LIVE_CANARY_MISSING_FOR_POLL",
                str(exc),
                submitted=True,
                claim_token=claim_token,
                force_user_action=True,
            )
        if not all([account_id, worker_id, provider_job_id]) or (
            provider_name == FLOW_PROVIDER and not provider_project_id
        ):
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                "JOB_STATE_INVALID",
                "submitted job is missing provider routing data",
                submitted=True,
                claim_token=claim_token,
                force_user_action=True,
            )
        assert account_id is not None
        assert worker_id is not None
        assert provider_job_id is not None
        poll_identity = None
        if provider_name == FLOW_PROVIDER:
            assert provider_project_id is not None
            poll_identity = ProviderPollIdentity(
                local_generation_job_id=job_id,
                provider_account_id=account_id,
                provider_project_id=provider_project_id,
                provider_job_id=provider_job_id,
            )
        poll_fence_conditions: list[Any] = []
        if poll_identity is not None:
            poll_fence_conditions = [
                GenerationJob.provider == FLOW_PROVIDER,
                GenerationJob.account_id == poll_identity.provider_account_id,
                GenerationJob.provider_project_id == poll_identity.provider_project_id,
            ]
        # Read the held result for *this* provider job. A row from an earlier
        # attempt names a different provider job and is therefore not a result
        # this poll may use; it is discarded rather than read.
        held = self._read_synchronous_result(job_id, provider_job_id)
        try:
            if held is not None:
                result = held
            elif poll_identity is None:
                result = await provider.get_job(
                    provider_job_id,
                    account_id=account_id,
                    worker_id=worker_id,
                    generation_type=capability,
                )
            else:
                result = await provider.get_job(
                    provider_job_id,
                    account_id=account_id,
                    worker_id=worker_id,
                    generation_type=capability,
                    poll_identity=poll_identity,
                )
            if not self._renew_claim(job_id, claim_token):
                current = self.get(job_id)
                if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                    raise LookupError("generation job not found")
                return current
            with self.database.session() as session:
                self._event(
                    session,
                    job_id,
                    "PROVIDER_JOB_POLL",
                    status=result.status,
                    progress=result.progress,
                    provider_account_id=account_id,
                    provider_project_id=provider_project_id,
                    provider_job_id=provider_job_id,
                )
            if result.status in {"CANCELLED", "CANCELED"}:
                with self.database.session() as session:
                    cancelled = session.execute(
                        update(GenerationJob)
                        .where(
                            GenerationJob.id == job_id,
                            GenerationJob.status == JobStatus.RESERVED.value,
                            GenerationJob.claim_token == claim_token,
                            GenerationJob.provider_job_id == provider_job_id,
                            GenerationJob.submission_state == "CONFIRMED",
                            GenerationJob.output_asset_id.is_(None),
                            *poll_fence_conditions,
                        )
                        .values(
                            status=JobStatus.CANCELLED.value,
                            safe_to_retry=False,
                            next_retry_at=None,
                            claim_token=None,
                            claim_expires_at=None,
                            error_code="PROVIDER_JOB_CANCELLED",
                            error_message=(result.error or "provider job was cancelled"),
                        )
                    )
                    if self._updated_one_row(cancelled):
                        job = session.get(GenerationJob, job_id)
                        if job is None:  # pragma: no cover - guarded by the conditional update.
                            raise LookupError("generation job not found")
                        session.refresh(job)
                        if self.workspace_credits is not None:
                            self.workspace_credits.require_reconciliation(
                                session,
                                job,
                                reason="PROVIDER_REPORTED_CANCELLED_WITH_BILLING_UNKNOWN",
                            )
                        idem = session.scalar(
                            select(GenerationIdempotency).where(
                                GenerationIdempotency.generation_job_id == job.id
                            )
                        )
                        if idem:
                            idem.status = "FAILED"
                        self.scheduler.release_job_in_session(
                            session,
                            job.id,
                            success=None,
                        )
                        self._event(
                            session,
                            job.id,
                            "PROVIDER_JOB_CANCELLED",
                            provider_job_id=provider_job_id,
                        )
                        session.flush()
                        return job
                current = self.get(job_id)
                if current is None:  # pragma: no cover - deleted by an administrator.
                    raise LookupError("generation job not found")
                return current
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
                    poll_identity=poll_identity,
                )
            if result.status != "COMPLETED":
                with self.database.session() as session:
                    advanced = session.execute(
                        update(GenerationJob)
                        .where(
                            GenerationJob.id == job_id,
                            GenerationJob.status == JobStatus.RESERVED.value,
                            GenerationJob.claim_token == claim_token,
                            GenerationJob.provider_job_id == provider_job_id,
                            GenerationJob.submission_state == "CONFIRMED",
                            GenerationJob.output_asset_id.is_(None),
                            *poll_fence_conditions,
                        )
                        .values(
                            status=JobStatus.RUNNING.value,
                            next_retry_at=self._next_poll_at(),
                            claim_token=None,
                            claim_expires_at=None,
                        )
                    )
                    if self._updated_one_row(advanced):
                        job = session.get(GenerationJob, job_id)
                        if job is None:  # pragma: no cover - guarded by the conditional update.
                            raise LookupError("generation job not found")
                        session.refresh(job)
                        return job
                current = self.get(job_id)
                if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                    raise LookupError("generation job not found")
                return current
            if not result.has_output:
                raise ProviderError(
                    "completed provider job returned no output",
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
                        GenerationJob.provider_job_id == provider_job_id,
                        GenerationJob.submission_state == "CONFIRMED",
                        GenerationJob.output_asset_id.is_(None),
                        *poll_fence_conditions,
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
            # Two phases with nothing between them but storage. Phase one
            # writes every artefact to its deterministic staging slot; phase
            # two adopts them all — media rows, sibling candidates, job
            # bindings, billing, settlement — in one database transaction. A
            # process death anywhere leaves either recyclable staging objects
            # or a completed job; never half a batch.
            primary, extras = await self._stage_provider_outputs(
                job_id,
                provider_name=provider.name,
                provider_job_id=provider_job_id,
                capability=capability,
                asset_type=asset_type,
                result=result,
            )
            job = self._finalize_completed_generation(
                job_id,
                claim_token=claim_token,
                provider_job_id=provider_job_id,
                poll_fence_conditions=poll_fence_conditions,
                provider_name=provider.name,
                project_id=project_id,
                shot_id=shot_id,
                candidate_id=candidate_id,
                asset_type=asset_type,
                primary=primary,
                extras=extras,
                raw=result.raw,
            )
            if job is None:
                current = self.get(job_id)
                if current is None:  # pragma: no cover - deleted concurrently by an administrator.
                    raise LookupError("generation job not found")
                return current
            asset_id = job.output_asset_id
            assert asset_id is not None
            try:
                self._settle_live_generation_fence(
                    job_id=job_id,
                    provider=provider_name,
                    model=model,
                    provider_job_id=provider_job_id,
                    raw=result.raw,
                )
            except (LiveSpendDenied, LiveCanaryConflict, SpendAuthorizationConflict, ValueError) as exc:
                # Generation is already durably complete. Canary settlement
                # ambiguity must remain reviewable, never roll back media.
                with self.database.session() as session:
                    self._event(
                        session,
                        job_id,
                        "LIVE_CANARY_SETTLEMENT_REVIEW_REQUIRED",
                        error=str(exc),
                    )
            try:
                self._record_live_generation_canary_verdict(
                    job_id=job_id,
                    provider=provider_name,
                    model=model,
                    provider_job_id=provider_job_id,
                )
            except Exception as exc:
                # The verdict is evidence about the model, never a reason to
                # touch a completed generation.
                with self.database.session() as session:
                    self._event(session, job_id, "LIVE_CANARY_VERDICT_FAILED", error=str(exc))
            if shot_id and not candidate_id and capability == "video" and self.continuity:
                try:
                    end_frame = self.continuity.extract_and_chain(shot_id, asset_id)
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
                poll_identity=poll_identity,
            )
        except RemoteMediaSecurityError as exc:
            # The provider has already completed (and may already have billed)
            # the remote job. Retrying an allowlist, DNS-safety, redirect or
            # content-type rejection cannot change that result, while treating
            # it as a transient poll failure keeps the provider account slot
            # occupied forever because submission attempts do not advance on
            # polls. End the job, release capacity and retain the workspace
            # charge for explicit provider-cost reconciliation.
            return self._schedule_error(
                job_id,
                RetryCategory.PERMANENT_ERROR,
                "PROVIDER_MEDIA_SECURITY_ERROR",
                str(exc),
                submitted=True,
                claim_token=claim_token,
                release_reservation=True,
                release_error=str(exc),
                poll_identity=poll_identity,
            )
        except Exception as exc:
            return self._schedule_error(
                job_id,
                RetryCategory.TRANSIENT_NETWORK,
                "POLL_PROCESSING_ERROR",
                str(exc),
                submitted=True,
                claim_token=claim_token,
                poll_identity=poll_identity,
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
        poll_identity: ProviderPollIdentity | None = None,
    ) -> GenerationJob:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise LookupError("generation job not found")
            if job.status in {
                JobStatus.COMPLETED.value,
                JobStatus.CANCELLED.value,
                JobStatus.FAILED.value,
            }:
                return job
            if claim_token is not None and (
                job.claim_token != claim_token or job.status != JobStatus.RESERVED.value
            ):
                return job
            if poll_identity is not None and (
                job.id != poll_identity.local_generation_job_id
                or job.provider != FLOW_PROVIDER
                or job.account_id != poll_identity.provider_account_id
                or job.provider_project_id != poll_identity.provider_project_id
                or job.provider_job_id != poll_identity.provider_job_id
            ):
                return job
            # Durable submission facts are monotonic. A caller may have read
            # NOT_SENT just before another transaction crossed the paid-call
            # boundary; no stale argument may downgrade that committed fact.
            submitted = submitted or bool(job.provider_job_id) or job.submission_state != "NOT_SENT"
            uncertain_paid_submission = submitted and not job.provider_job_id
            if uncertain_paid_submission:
                release_reservation = False
                clear_routing = False
            decision = self.retry_policy.decide(
                category,
                job.attempt_count,
                job.max_attempts,
                submitted=uncertain_paid_submission,
            )
            if force_user_action or decision.requires_user_action:
                target_status = JobStatus.WORKER_NEEDS_USER_ACTION.value
                next_retry_at = None
            elif decision.retry:
                target_status = JobStatus.RETRY_WAIT.value
                next_retry_at = utcnow() + timedelta(seconds=decision.delay_seconds)
            else:
                target_status = JobStatus.FAILED.value
                next_retry_at = None

            # Cancellation, completion and manual reconciliation all use
            # conditional updates. Error handling must participate in the
            # same fence; a stale ORM flush by the worker must never revive a
            # terminal/refunded job after another transaction wins.
            observed_status = job.status
            observed_submission_state = job.submission_state
            observed_provider_job_id = job.provider_job_id
            observed_claim_token = job.claim_token
            conditions: list[Any] = [
                GenerationJob.id == job.id,
                GenerationJob.status == observed_status,
                GenerationJob.submission_state == observed_submission_state,
                GenerationJob.output_asset_id.is_(None),
            ]
            if observed_provider_job_id is None:
                conditions.append(GenerationJob.provider_job_id.is_(None))
            else:
                conditions.append(GenerationJob.provider_job_id == observed_provider_job_id)
            if poll_identity is not None:
                conditions.extend(
                    [
                        GenerationJob.provider == FLOW_PROVIDER,
                        GenerationJob.account_id == poll_identity.provider_account_id,
                        GenerationJob.provider_project_id == poll_identity.provider_project_id,
                    ]
                )
            if claim_token is not None:
                conditions.extend(
                    [
                        GenerationJob.status == JobStatus.RESERVED.value,
                        GenerationJob.claim_token == claim_token,
                    ]
                )
            elif observed_claim_token is None:
                conditions.append(GenerationJob.claim_token.is_(None))
            else:
                conditions.append(GenerationJob.claim_token == observed_claim_token)
            if self.workspace_credits is not None and job.workspace_credit_required:
                credit = self.workspace_credits.entry_for_job_in_session(session, job.id)
                if credit is None:
                    raise WorkspaceCreditConflict(
                        "generation requires a server-owned workspace credit reservation"
                    )
                conditions.append(
                    select(WorkspaceCreditEntry.id)
                    .where(
                        WorkspaceCreditEntry.id == credit.id,
                        WorkspaceCreditEntry.status == credit.status,
                        WorkspaceCreditEntry.version == credit.version,
                    )
                    .exists()
                )
            transitioned = session.execute(
                update(GenerationJob)
                .where(*conditions)
                .values(
                    status=target_status,
                    retry_category=category.value,
                    error_code=code,
                    error_message=message[:4000],
                    safe_to_retry=not submitted,
                    next_retry_at=next_retry_at,
                    claim_token=None,
                    claim_expires_at=None,
                    submission_state=("NOT_SENT" if not submitted else observed_submission_state),
                )
            )
            if not self._updated_one_row(transitioned):
                session.expire_all()
                current = session.get(GenerationJob, job_id)
                if current is None:  # pragma: no cover - deleted by an administrator.
                    raise LookupError("generation job not found")
                return current
            session.expire(job)
            session.refresh(job)
            if target_status == JobStatus.FAILED.value:
                # Terminal and not retryable: nothing will ever consume a held
                # synchronous result, so the bytes go with the same transaction
                # that ends the job. RETRY_WAIT and WORKER_NEEDS_USER_ACTION
                # keep theirs — those jobs still have an attempt ahead of them.
                self._discard_synchronous_result(session, job_id)
            if target_status == JobStatus.WORKER_NEEDS_USER_ACTION.value:
                worker = session.get(BrowserWorker, job.worker_id) if job.worker_id else None
                if worker:
                    worker.status = WorkerStatus.NEEDS_USER_ACTION.value
            if self.workspace_credits is not None:
                if submitted and job.status in {
                    JobStatus.FAILED.value,
                    JobStatus.WORKER_NEEDS_USER_ACTION.value,
                }:
                    credit_transition = self.workspace_credits.require_reconciliation(
                        session,
                        job,
                        reason=code,
                    )
                    if credit_transition.applied and not credit_transition.replayed:
                        self._event(
                            session,
                            job.id,
                            "CREDIT_RECONCILIATION_REQUIRED",
                            reason=code,
                        )
                elif not submitted and job.status == JobStatus.FAILED.value:
                    credit_refund = self.workspace_credits.refund_generation(
                        session,
                        job,
                        reason=f"PRE_SUBMIT_FAILURE:{code}",
                    )
                    if credit_refund.applied and not credit_refund.replayed:
                        self._event(
                            session,
                            job.id,
                            "CREDIT_REFUNDED",
                            credits=credit_refund.refunded_credits,
                            reason=code,
                        )
                    # A refunded terminal job is immutable. A new attempt must
                    # use a new job/idempotency key and reserve again.
                    job.safe_to_retry = False
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
                    ),
                    # A creation deleted while the worker was down is not
                    # brought back to life by the restart that follows.
                    GenerationJob.deleted_at.is_(None),
                )
            ).all()
            for job in jobs:
                observed_status = job.status
                observed_submission_state = job.submission_state
                observed_provider_job_id = job.provider_job_id
                observed_claim_token = job.claim_token
                conditions: list[Any] = [
                    GenerationJob.id == job.id,
                    GenerationJob.status == observed_status,
                    GenerationJob.submission_state == observed_submission_state,
                ]
                if observed_provider_job_id is None:
                    conditions.append(GenerationJob.provider_job_id.is_(None))
                else:
                    conditions.append(GenerationJob.provider_job_id == observed_provider_job_id)
                if observed_claim_token is None:
                    conditions.append(GenerationJob.claim_token.is_(None))
                else:
                    conditions.append(GenerationJob.claim_token == observed_claim_token)
                if job.output_asset_id is None:
                    conditions.append(GenerationJob.output_asset_id.is_(None))
                else:
                    conditions.append(GenerationJob.output_asset_id == job.output_asset_id)
                if observed_status == JobStatus.RESERVED.value:
                    conditions.append(
                        or_(
                            GenerationJob.claim_expires_at.is_(None),
                            GenerationJob.claim_expires_at <= now,
                        )
                    )

                credit = None
                if self.workspace_credits is not None and job.workspace_credit_required:
                    credit = self.workspace_credits.entry_for_job_in_session(session, job.id)
                    if credit is None:
                        raise WorkspaceCreditConflict(
                            "generation requires a server-owned workspace credit reservation"
                        )
                    if credit.status == "REFUNDED":
                        continue
                    if (
                        not observed_provider_job_id
                        and observed_submission_state == "NOT_SENT"
                        and (credit.status != "RESERVED")
                    ):
                        continue
                    conditions.append(
                        select(WorkspaceCreditEntry.id)
                        .where(
                            WorkspaceCreditEntry.id == credit.id,
                            WorkspaceCreditEntry.status == credit.status,
                            WorkspaceCreditEntry.version == credit.version,
                        )
                        .exists()
                    )

                if job.provider_job_id:
                    values: dict[str, Any] = {
                        "status": JobStatus.SUBMITTED.value,
                        "submission_state": "CONFIRMED",
                        "safe_to_retry": False,
                        "next_retry_at": now,
                        "claim_token": None,
                        "claim_expires_at": None,
                    }
                elif job.submission_state == "SENT_UNCONFIRMED":
                    values = {
                        "status": JobStatus.WORKER_NEEDS_USER_ACTION.value,
                        "safe_to_retry": False,
                        "next_retry_at": None,
                        "claim_token": None,
                        "claim_expires_at": None,
                    }
                else:
                    values = {
                        "status": JobStatus.RETRY_WAIT.value,
                        "safe_to_retry": True,
                        "next_retry_at": now,
                        "submission_state": "NOT_SENT",
                        "claim_token": None,
                        "claim_expires_at": None,
                    }
                resumed = session.execute(
                    update(GenerationJob)
                    .where(*conditions)
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                if not self._updated_one_row(resumed):
                    session.expire(job)
                    continue
                session.expire(job)
                session.refresh(job)

                if observed_provider_job_id:
                    idem = session.scalar(
                        select(GenerationIdempotency).where(GenerationIdempotency.generation_job_id == job.id)
                    )
                    if idem:
                        idem.provider_job_id = observed_provider_job_id
                    if self.workspace_credits is not None:
                        self.workspace_credits.record_submission_confirmed(
                            session,
                            job,
                            attempt=max(job.attempt_count, 1),
                            provider_job_id=observed_provider_job_id,
                        )
                elif observed_submission_state == "SENT_UNCONFIRMED":
                    if self.workspace_credits is not None:
                        self.workspace_credits.require_reconciliation(
                            session,
                            job,
                            reason="RESTART_FOUND_UNCONFIRMED_SUBMISSION",
                        )
                else:
                    self.scheduler.release_job_in_session(
                        session,
                        job.id,
                        success=None,
                        clear_routing=True,
                    )
                self._event(session, job.id, "JOB_RESUMED", status=job.status)
                recovered += 1
        self.reconcile_credit_lifecycle()
        return recovered

    def reconcile_credit_lifecycle(self) -> int:
        """Repair deterministic crash gaps; ambiguous provider outcomes stay held."""

        if self.workspace_credits is None:
            return 0
        repaired = 0
        with self.database.session() as session:
            rows = session.execute(
                select(GenerationJob, WorkspaceCreditEntry)
                .join(
                    WorkspaceCreditEntry,
                    WorkspaceCreditEntry.generation_job_id == GenerationJob.id,
                )
                .where(WorkspaceCreditEntry.status.in_(["RESERVED", "RECONCILIATION_REQUIRED"]))
            ).all()
            for job, entry in rows:
                if job.status == JobStatus.COMPLETED.value:
                    transition = self.workspace_credits.settle_generation(
                        session,
                        job,
                        reason="RECOVERY_COMPLETED_JOB",
                    )
                elif (
                    entry.status == "RESERVED"
                    and job.status in {JobStatus.CANCELLED.value, JobStatus.FAILED.value}
                    and job.submission_state == "NOT_SENT"
                    and not job.provider_job_id
                ):
                    transition = self.workspace_credits.refund_generation(
                        session,
                        job,
                        reason="RECOVERY_PRE_SUBMIT_TERMINAL",
                    )
                    job.safe_to_retry = False
                elif entry.status == "RESERVED" and (
                    job.submission_state == "SENT_UNCONFIRMED"
                    or (
                        job.status in {JobStatus.CANCELLED.value, JobStatus.FAILED.value}
                        and bool(job.provider_job_id)
                    )
                ):
                    transition = self.workspace_credits.require_reconciliation(
                        session,
                        job,
                        reason="RECOVERY_AMBIGUOUS_PROVIDER_OUTCOME",
                    )
                else:
                    continue
                if transition.applied and not transition.replayed:
                    repaired += 1
                    self._event(
                        session,
                        job.id,
                        "CREDIT_LIFECYCLE_RECOVERED",
                        previous_status=transition.previous_status,
                        status=transition.status,
                    )
        return repaired
