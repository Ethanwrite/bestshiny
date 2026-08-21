from __future__ import annotations

import math
from dataclasses import dataclass

from model_registry_core import ModelCapabilityRegistry
from platform_database import Database
from production_domain.models import CostRecord, GenerationCandidate, GenerationJob, Shot, new_id
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


@dataclass(frozen=True)
class CreditEstimate:
    provider_cost_usd: float
    resolution_multiplier: float
    reference_multiplier: float
    service_multiplier: float
    estimated_total_usd: float
    credits: int
    usd_per_credit: float


class CreditPricingEngine:
    """Transparent estimate: provider cost plus a bounded service/retry reserve."""

    version = "credit-pricing-v1"

    def __init__(
        self,
        registry: ModelCapabilityRegistry,
        *,
        usd_per_credit: float = 0.01,
        service_multiplier: float = 1.20,
    ):
        self.registry = registry
        self.usd_per_credit = usd_per_credit
        self.service_multiplier = service_multiplier

    def estimate(
        self,
        *,
        provider: str,
        model: str,
        media_type: str,
        duration: float = 1.0,
        resolution: str = "720p",
        reference_count: int = 0,
    ) -> CreditEstimate:
        profile = self.registry.get(model, provider) if media_type == "video" else None
        if media_type == "video" and profile is None:
            raise ValueError("selected video model is not registered for this provider")
        if profile:
            provider_cost = profile.cost.get("estimated_per_second", 0.0) * max(1.0, duration)
        else:
            # Provisional image rate is intentionally visible in the breakdown
            # until an image model capability registry supplies a live rate.
            provider_cost = 0.04
        resolution_multiplier = {"720p": 1.0, "1080p": 1.30, "2k": 1.65, "4k": 2.4}.get(
            resolution.lower(), 1.0
        )
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
        )


class CostEngine:
    def __init__(self, database: Database):
        self.database = database

    def record_job(
        self,
        job_id: str,
        *,
        estimated_cost: float = 0.0,
        actual_cost: float = 0.0,
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
            record.actual_cost = actual_cost
            record.retry_cost = retry_cost
            job.cost_estimate = estimated_cost
            job.actual_cost = actual_cost
            session.flush()
            return record

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
            total, accepted, attempts = session.execute(
                select(
                    func.coalesce(func.sum(CostRecord.actual_cost + CostRecord.retry_cost), 0.0),
                    func.coalesce(
                        func.sum(
                            (CostRecord.actual_cost + CostRecord.retry_cost)
                            * CostRecord.accepted.cast(type_=CostRecord.actual_cost.type)
                        ),
                        0.0,
                    ),
                    func.count(CostRecord.id),
                ).where(CostRecord.shot_id == shot.id)
            ).one()
            return {
                "attempts": int(attempts),
                "total_cost": round(float(total), 4),
                "accepted_cost": round(float(accepted), 4),
                "cost_per_accepted_shot": round(float(total), 4) if shot.committed_candidate_id else 0.0,
            }

    def provider_metrics(self, provider: str) -> dict[str, float | int]:
        with self.database.session() as session:
            total, accepted, cost = session.execute(
                select(
                    func.count(CostRecord.id),
                    func.coalesce(func.sum(CostRecord.accepted.cast(type_=CostRecord.credits.type)), 0.0),
                    func.coalesce(func.sum(CostRecord.actual_cost + CostRecord.retry_cost), 0.0),
                ).where(CostRecord.provider == provider)
            ).one()
            total = int(total)
            accepted = int(accepted)
            return {
                "attempts": total,
                "accepted": accepted,
                "qa_pass_rate": round(accepted / total, 4) if total else 0.0,
                "effective_cost": round(float(cost), 4),
                "cost_per_accepted_shot": round(float(cost) / accepted, 4) if accepted else 0.0,
            }
