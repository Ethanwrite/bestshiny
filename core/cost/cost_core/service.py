from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from model_registry_core import ModelCapabilityRegistry
from platform_database import Database
from production_domain.models import (
    BillingEvidenceSource,
    CostRecord,
    GenerationCandidate,
    GenerationJob,
    ModelPricingProfile,
    ProviderBillingEvidence,
    QADecision,
    QAResult,
    Shot,
    new_id,
    utcnow,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


class PricingUnverified(ValueError):
    """No confirmed provider price covers this request, and money is at stake."""

    def __init__(self, provider: str, model: str, detail: str):
        super().__init__(
            f"no verified price for {provider}:{model} — {detail}; "
            "confirm the provider's published rate before this model takes a paid request"
        )
        self.provider = provider
        self.model = model


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# Whether the *input* carries video, which is the axis Ark actually prices on.
# Only a continuation feeds a clip back in; a character or scene reference is an
# image, and a start/end frame is an image. Keying this off `reference_count`
# billed every reference-guided shot as video input — and because the
# video-input rates are deliberately unseeded, fail-closed then refused the
# whole class of shots the platform generates most.
VIDEO_INPUT_POLICIES = frozenset({"CONTINUE_V2V"})


def _input_mode(media_type: str, generation_policy: str) -> str:
    if media_type != "video":
        return "default"
    return (
        "video_input"
        if generation_policy.strip().upper() in VIDEO_INPUT_POLICIES
        else "no_video_input"
    )


@dataclass(frozen=True)
class CreditEstimate:
    provider_cost_usd: float
    resolution_multiplier: float
    reference_multiplier: float
    service_multiplier: float
    estimated_total_usd: float
    credits: int
    usd_per_credit: float
    image_count: int = 1
    # VERIFIED when the figure came from a dated provider price with a source
    # URL; UNVERIFIED when it fell back to the seeded placeholder. A paid route
    # refuses UNVERIFIED, so this is not decoration.
    pricing_status: str = "UNVERIFIED"
    pricing_source_url: str = ""
    pricing_checked_at: str = ""
    billing_unit: str = ""
    settlement_formula: str = ""


class CreditPricingEngine:
    """Transparent estimate: provider cost plus a bounded service/retry reserve."""

    version = "credit-pricing-v1"

    def __init__(
        self,
        registry: ModelCapabilityRegistry,
        *,
        usd_per_credit: float = 0.01,
        service_multiplier: float = 1.20,
        database: Database | None = None,
        require_verified_pricing: bool = False,
    ):
        self.registry = registry
        self.usd_per_credit = usd_per_credit
        self.service_multiplier = service_multiplier
        self.database = database
        # In live mode an unpriced model must not be quotable: a placeholder that
        # is 40% of the real rate loses money on every call and nothing reports
        # it. Outside live mode the fallback keeps development and the offline
        # suite working, and the estimate says which of the two it was.
        self.require_verified_pricing = require_verified_pricing

    def _active_profile(
        self,
        *,
        provider: str,
        model: str,
        scopes: tuple[tuple[str, str], ...],
    ) -> ModelPricingProfile | None:
        """The narrowest price in force right now, promotion before list.

        `scopes` is the (input_mode, resolution) precedence, narrowest first. All
        of them are answered from one read: a quote is on the request path, and
        an unpriced model would otherwise pay a round trip per candidate to be
        told nothing four times.
        """

        if self.database is None:
            return None
        now = utcnow()
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(ModelPricingProfile).where(
                        ModelPricingProfile.provider == provider,
                        ModelPricingProfile.provider_model_id == model,
                        ModelPricingProfile.effective_from <= now,
                    )
                )
            )
        live = [
            row
            for row in rows
            if row.effective_until is None or _aware(row.effective_until) > now
        ]
        for scope in scopes:
            candidates = [row for row in live if (row.input_mode, row.resolution) == scope]
            if not candidates:
                continue
            # A dated promotion is more specific than an open-ended list price,
            # so it wins while it is in force and stops applying on its own
            # afterwards. Later start dates win among equals: a reissued rate
            # supersedes the one it was issued to replace.
            candidates.sort(
                key=lambda row: (
                    row.effective_until is None,
                    -_aware(row.effective_from).timestamp(),
                )
            )
            return candidates[0]
        return None

    def estimate(
        self,
        *,
        provider: str,
        model: str,
        media_type: str,
        duration: float = 1.0,
        resolution: str = "720p",
        reference_count: int = 0,
        image_count: int = 1,
        generation_policy: str = "TEXT_TO_VIDEO",
    ) -> CreditEstimate:
        profile = self.registry.get(model, provider)
        if profile is None or f"{media_type}_generation" not in profile.supported_operations:
            raise ValueError(f"selected {media_type} model is not registered for this provider")
        images = max(1, int(image_count))
        if media_type == "video" and images != 1:
            raise ValueError("image_count applies to image generation only")

        input_mode = _input_mode(media_type, generation_policy)
        # Narrowest first. A provider that prices one axis but not the other —
        # a per-image rate with no resolution, a flat rate with no input mode —
        # is described by dropping that axis to "", which is what the model
        # documents "" to mean. Widening never invents a price: every candidate
        # is a row someone recorded from a published page.
        priced = self._active_profile(
            provider=provider,
            model=model,
            scopes=(
                (input_mode, resolution.lower()),
                (input_mode, ""),
                ("default", resolution.lower()),
                ("default", ""),
            ),
        )

        if priced is not None:
            # The provider's own published rate, in the provider's own currency,
            # converted at a rate that carries its own source and date.
            quantity = max(1.0, duration) if media_type == "video" else float(images)
            provider_cost = (
                float(priced.estimate_unit_price) * quantity * float(priced.usd_per_currency)
            )
            # Resolution is priced by the profile that was selected for it, so
            # there is no multiplier left to apply. That table used to charge
            # 1080p at 1.30x for every provider on the platform; Ark's own rates
            # put its 1080p at 2.47x of 720p and its 480p at 0.44x.
            resolution_multiplier = 1.0
            pricing_status = "VERIFIED"
            source_url = priced.source_url
            checked_at = priced.source_checked_at.isoformat()
            billing_unit = priced.billing_unit
            settlement_formula = priced.settlement_formula
        else:
            if self.require_verified_pricing:
                raise PricingUnverified(
                    provider,
                    model,
                    f"no pricing profile for input_mode={input_mode!r} resolution={resolution!r}",
                )
            # Development and the offline suite still need a number. It is the
            # seeded placeholder, and the estimate says so.
            if media_type == "video":
                provider_cost = profile.cost.get("estimated_per_second", 0.0) * max(1.0, duration)
            else:
                # Every image in the batch is generated and billed, so every
                # image is reserved before the call. Charging for one and
                # delivering four would make the workspace balance a fiction.
                provider_cost = profile.cost.get("estimated_per_image", 0.0) * images
            resolution_multiplier = {"720p": 1.0, "1080p": 1.30, "2k": 1.65, "4k": 2.4}.get(
                resolution.lower(), 1.0
            )
            pricing_status = "UNVERIFIED"
            source_url = ""
            checked_at = ""
            billing_unit = ""
            settlement_formula = ""

        reference_multiplier = min(1.25, 1.0 + max(0, reference_count) * 0.04)
        total = provider_cost * resolution_multiplier * reference_multiplier * self.service_multiplier
        credits = max(1, math.ceil(total / self.usd_per_credit))
        return CreditEstimate(
            provider_cost_usd=round(provider_cost, 4),
            resolution_multiplier=resolution_multiplier,
            reference_multiplier=round(reference_multiplier, 4),
            service_multiplier=self.service_multiplier,
            estimated_total_usd=round(total, 4),
            credits=credits,
            usd_per_credit=self.usd_per_credit,
            image_count=images,
            pricing_status=pricing_status,
            pricing_source_url=source_url,
            pricing_checked_at=checked_at,
            billing_unit=billing_unit,
            settlement_formula=settlement_formula,
        )


class CostEngine:
    def __init__(self, database: Database):
        self.database = database

    def record_job(
        self,
        job_id: str,
        *,
        estimated_cost: float = 0.0,
        actual_cost: float | None = None,
        credits: float | None = None,
        retry_cost: float = 0.0,
        resolution: str = "720p",
    ) -> CostRecord:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                raise LookupError("generation job not found")
            existing = session.scalar(select(CostRecord).where(CostRecord.generation_job_id == job.id))
            if existing is None:
                values = {
                    "id": new_id(),
                    "project_id": job.project_id,
                    "shot_id": job.shot_id,
                    "candidate_id": job.candidate_id,
                    "generation_job_id": job.id,
                    "provider": job.provider,
                    "model": job.model,
                    "duration": float(job.request_json.get("duration") or 0),
                    "resolution": resolution,
                    "credits": credits if credits is not None else 0.0,
                    "estimated_cost": estimated_cost,
                    "actual_cost": actual_cost,
                    "retry_cost": retry_cost,
                    "accepted": False,
                    "wasted": False,
                }
                dialect = session.get_bind().dialect.name
                if dialect == "postgresql":
                    postgres_statement = postgresql_insert(CostRecord).values(**values)
                    postgres_statement = postgres_statement.on_conflict_do_nothing(
                        index_elements=["generation_job_id"]
                    )
                    session.execute(postgres_statement)
                elif dialect == "sqlite":
                    sqlite_statement = sqlite_insert(CostRecord).values(**values)
                    sqlite_statement = sqlite_statement.on_conflict_do_nothing(
                        index_elements=["generation_job_id"]
                    )
                    session.execute(sqlite_statement)
                else:
                    session.add(CostRecord(**values))
                    session.flush()
                existing = session.scalar(select(CostRecord).where(CostRecord.generation_job_id == job.id))
            record = existing
            if record is None:
                raise RuntimeError("cost record insert did not produce a durable row")
            if record.project_id != job.project_id:
                raise RuntimeError("cost record project does not match its generation job")
            if credits is not None:
                record.credits = credits
            record.estimated_cost = estimated_cost
            if actual_cost is not None:
                record.actual_cost = actual_cost
            record.retry_cost = retry_cost
            job.cost_estimate = estimated_cost
            if actual_cost is not None:
                job.actual_cost = actual_cost
            session.flush()
            return record

    def record_billing_evidence(
        self,
        job_id: str,
        *,
        evidence_key: str,
        source: BillingEvidenceSource | str,
        actual_cost_usd: Decimal | str | float | None = None,
        estimated_cost_usd: Decimal | str | float | None = None,
        provider_credits: Decimal | str | float | None = None,
        provider_reference: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProviderBillingEvidence:
        """Persist one idempotent billing fact without inventing provider actuals.

        Only provider-verified or manually reconciled evidence may populate
        ``actual_cost``.  Estimated and unknown evidence remain useful for
        economics while retaining their weaker provenance.
        """

        key = evidence_key.strip()
        if not key:
            raise ValueError("billing evidence_key is required")
        evidence_source = BillingEvidenceSource(source)
        actual = _money(actual_cost_usd)
        estimate = _money(estimated_cost_usd)
        credits = _money(provider_credits)
        reference = provider_reference.strip() if provider_reference else None
        if (
            evidence_source
            in {
                BillingEvidenceSource.ESTIMATED,
                BillingEvidenceSource.UNKNOWN,
            }
            and actual is not None
        ):
            raise ValueError(f"{evidence_source.value} evidence cannot set actual_cost_usd")
        if (
            evidence_source is BillingEvidenceSource.VERIFIED_PROVIDER
            and actual is not None
            and not reference
        ):
            raise ValueError("verified provider actual cost requires provider_reference")
        if evidence_source is BillingEvidenceSource.RECONCILED_MANUAL and not reference:
            raise ValueError("manual reconciliation requires an evidence reference")
        normalized_metadata = dict(metadata or {})
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise LookupError("generation job not found")
            cost_record = session.scalar(select(CostRecord).where(CostRecord.generation_job_id == job.id))
            existing = session.scalar(
                select(ProviderBillingEvidence).where(
                    ProviderBillingEvidence.generation_job_id == job.id,
                    ProviderBillingEvidence.evidence_key == key,
                )
            )
            facts = (
                evidence_source.value,
                reference,
                actual,
                estimate,
                credits,
                normalized_metadata,
            )
            if existing is not None:
                recorded = (
                    existing.source,
                    existing.provider_reference,
                    _money(existing.actual_cost_usd),
                    _money(existing.estimated_cost_usd),
                    _money(existing.provider_credits),
                    existing.metadata_json,
                )
                if recorded != facts:
                    raise ValueError("billing evidence key was reused with different facts")
                return existing
            evidence = ProviderBillingEvidence(
                generation_job_id=job.id,
                cost_record_id=cost_record.id if cost_record else None,
                evidence_key=key,
                provider=job.provider,
                model=job.model,
                source=evidence_source.value,
                provider_reference=reference,
                actual_cost_usd=actual,
                estimated_cost_usd=estimate,
                provider_credits=credits,
                metadata_json=normalized_metadata,
                verified_at=(
                    utcnow()
                    if evidence_source
                    in {
                        BillingEvidenceSource.VERIFIED_PROVIDER,
                        BillingEvidenceSource.RECONCILED_MANUAL,
                    }
                    else None
                ),
            )
            session.add(evidence)
            if estimate is not None and cost_record is not None:
                cost_record.estimated_cost = float(estimate)
                job.cost_estimate = float(estimate)
            if actual is not None:
                job.actual_cost = float(actual)
                if cost_record is not None:
                    cost_record.actual_cost = float(actual)
            session.flush()
            return evidence

    def finalize_candidate(self, candidate_id: str, *, accepted: bool) -> None:
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate:
                raise LookupError("candidate not found")
            records = session.scalars(select(CostRecord).where(CostRecord.candidate_id == candidate.id))
            for record in records:
                record.accepted = accepted
                record.wasted = not accepted

    def shot_cost(self, shot_id: str) -> dict[str, float | int]:
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise LookupError("shot not found")
            records = list(session.scalars(select(CostRecord).where(CostRecord.shot_id == shot.id)))
            costs = [_effective_cost(record) for record in records]
            total = sum(costs)
            accepted_attempt = sum(
                cost for record, cost in zip(records, costs, strict=True) if record.accepted
            )
            verified_total = sum(
                float(record.actual_cost or 0.0) for record in records if record.actual_cost is not None
            )
            estimated_total = sum(
                float(record.estimated_cost) for record in records if record.actual_cost is None
            )
            return {
                "attempts": len(records),
                "total_cost": round(total, 4),
                "accepted_cost": round(accepted_attempt, 4),
                "accepted_shot_cost": round(total, 4) if shot.committed_candidate_id else 0.0,
                "wasted_cost": round(max(0.0, total - accepted_attempt), 4),
                "verified_cost_total": round(verified_total, 4),
                "estimated_cost_total": round(estimated_total, 4),
                "cost_per_accepted_shot": round(total, 4) if shot.committed_candidate_id else 0.0,
            }

    def provider_metrics(self, provider: str) -> dict[str, float | int]:
        with self.database.session() as session:
            records = list(session.scalars(select(CostRecord).where(CostRecord.provider == provider)))
            candidate_ids = [record.candidate_id for record in records if record.candidate_id]
            qa_results = (
                list(
                    session.scalars(
                        select(QAResult)
                        .join(GenerationCandidate, GenerationCandidate.qa_result_id == QAResult.id)
                        .where(GenerationCandidate.id.in_(candidate_ids))
                    )
                )
                if candidate_ids
                else []
            )
            jobs = list(
                session.scalars(
                    select(GenerationJob).where(
                        GenerationJob.id.in_(
                            [record.generation_job_id for record in records if record.generation_job_id]
                        )
                    )
                )
            )
            total = len(records)
            accepted = sum(1 for record in records if record.accepted)
            effective_total = sum(_effective_cost(record) for record in records)
            verified_total = sum(
                float(record.actual_cost or 0.0) for record in records if record.actual_cost is not None
            )
            estimated_total = sum(
                float(record.estimated_cost) for record in records if record.actual_cost is None
            )
            completed_qa = len(qa_results)
            latencies = [
                (job.completed_at - job.started_at).total_seconds()
                for job in jobs
                if job.completed_at and job.started_at
            ]

            def dimension_rate(field: str) -> float:
                values = [getattr(result, field) for result in qa_results]
                scored = [float(value) for value in values if value is not None]
                return round(sum(value >= 0.8 for value in scored) / len(scored), 4) if scored else 0.0

            return {
                "attempts": total,
                "accepted": accepted,
                "qa_pass_rate": (
                    round(
                        sum(result.decision == QADecision.PASS.value for result in qa_results) / completed_qa,
                        4,
                    )
                    if completed_qa
                    else 0.0
                ),
                "identity_pass_rate": dimension_rate("character_score"),
                "camera_pass_rate": dimension_rate("camera_score"),
                "action_pass_rate": dimension_rate("action_score"),
                "average_latency": (round(sum(latencies) / len(latencies), 4) if latencies else 0.0),
                "failure_rate": round((total - accepted) / total, 4) if total else 0.0,
                "verified_cost_total": round(verified_total, 4),
                "estimated_cost_total": round(estimated_total, 4),
                "effective_cost": round(effective_total, 4),
                "cost_per_accepted_shot": (round(effective_total / accepted, 4) if accepted else 0.0),
            }


def _money(value: Decimal | str | float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("cost evidence must be a decimal amount") from exc
    if parsed < 0:
        raise ValueError("cost evidence cannot be negative")
    return parsed.quantize(Decimal("0.000001"))


def _effective_cost(record: CostRecord) -> float:
    base = record.actual_cost if record.actual_cost is not None else record.estimated_cost
    return float(base or 0.0) + float(record.retry_cost or 0.0)
