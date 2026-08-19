from __future__ import annotations

from platform_database import Database
from production_domain.models import CostRecord, GenerationCandidate, GenerationJob, Shot
from sqlalchemy import func, select


class CostEngine:
    def __init__(self, database: Database):
        self.database = database

    def record_job(
        self,
        job_id: str,
        *,
        estimated_cost: float = 0.0,
        actual_cost: float = 0.0,
        credits: float = 0.0,
        retry_cost: float = 0.0,
        resolution: str = "720p",
    ) -> CostRecord:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                raise LookupError("generation job not found")
            existing = session.scalar(select(CostRecord).where(CostRecord.generation_job_id == job.id))
            record = existing or CostRecord(
                project_id=job.project_id,
                shot_id=job.shot_id,
                candidate_id=job.candidate_id,
                generation_job_id=job.id,
                provider=job.provider,
                model=job.model,
                duration=float(job.request_json.get("duration") or 0),
                resolution=resolution,
            )
            if not existing:
                session.add(record)
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
