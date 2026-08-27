"""Reading and writing router evidence in the database.

The only module in this package that touches SQLAlchemy. Everything else takes
lists and returns dataclasses, which is what makes the posterior engine and the
replay harness testable without a database and impossible to accidentally wire
into a request path.

Three responsibilities, all of them append-mostly:

* record a production observation, once per generation job;
* save an offline posterior run;
* save a replay result, because the LCB feature flag's precondition is a
  replay that passed and a claim is not a record.

What it will not do is write an observation it cannot attribute. An attempt
whose exact version is unknown, or whose model was recorded by an alias the
provider can repoint, is refused at the boundary — with an exception naming the
model — rather than stored under a version it may not belong to. The one thing
worse than a missing observation is a confidently mislabelled one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, cast

from platform_database import Database
from production_domain.models import (
    RouterObservation,
    RouterPosterior,
    RouterReplayRun,
    new_id,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .keys import ConditionBucket, EvidenceKey, ReferenceMode, Scenario, TaskType
from .lcb import PosteriorLookup
from .observations import OutcomeName, ProductionObservation, PromptComplexity
from .posterior import PosteriorLevel, PosteriorRecord, PosteriorRun
from .replay import ReplayResult


class UnattributableObservation(ValueError):
    """An attempt that cannot be filed against an exact version."""


class RouterObservationService:
    """Append-only production evidence, wide enough to compute a posterior from."""

    version = "router-observation-service-v1"

    def __init__(self, database: Database):
        self.database = database

    # ------------------------------------------------------------------
    # writing

    def record(self, observation: ProductionObservation) -> RouterObservation:
        """Persist one observation, idempotently per generation job.

        The conflict target is ``generation_job_id``. A retried worker or a
        replayed webhook therefore lands on the row that already exists rather
        than adding a second copy of the same attempt — which would inflate
        every count that uses this table, including the effective sample size
        the LCB gate depends on.

        An observation with no generation job (a backfill, a synthetic replay
        fixture) is inserted plainly, because there is no key to be idempotent
        against.
        """

        if observation.model_is_alias:
            raise UnattributableObservation(
                f"{observation.provider}:{observation.model_id} was recorded by alias; "
                "resolve the snapshot before recording, or the outcome is attributed to "
                "whatever the alias points at today"
            )
        if not observation.exact_version.strip():
            raise UnattributableObservation(
                f"{observation.provider}:{observation.model_id} has no exact version"
            )

        values = self._row_values(observation)
        with self.database.session() as session:
            if observation.generation_job_id is None:
                record = RouterObservation(**values)
                session.add(record)
                session.flush()
                return record
            dialect = session.get_bind().dialect.name
            insert_statement: Any
            if dialect == "postgresql":
                insert_statement = postgresql_insert(RouterObservation).values(**values)
            elif dialect == "sqlite":
                insert_statement = sqlite_insert(RouterObservation).values(**values)
            else:  # pragma: no cover - the platform supports SQLite and PostgreSQL.
                raise RuntimeError(f"unsupported dialect for router observation upsert: {dialect}")
            session.execute(
                insert_statement.on_conflict_do_nothing(index_elements=["generation_job_id"])
            )
            stored = session.scalar(
                select(RouterObservation).where(
                    RouterObservation.generation_job_id == observation.generation_job_id
                )
            )
            if stored is None:  # pragma: no cover - defensive guard for a failed insert
                raise RuntimeError("router observation upsert did not return a persisted row")
            session.flush()
            return stored

    @staticmethod
    def _row_values(observation: ProductionObservation) -> dict[str, Any]:
        now = datetime.now(UTC)
        return {
            "id": observation.observation_id if len(observation.observation_id) <= 36 else new_id(),
            "occurred_at": observation.occurred_at,
            "provider": observation.provider,
            "model_id": observation.model_id,
            "exact_version": observation.exact_version,
            "model_is_alias": observation.model_is_alias,
            "task_type": observation.task_type.value,
            "scenario": observation.scenario.value,
            "asset_criticality": observation.asset_criticality,
            "prompt_complexity": observation.prompt_complexity.value,
            "reference_mode": observation.reference_mode.value,
            "duration_seconds": observation.duration_seconds,
            "resolution": observation.resolution,
            "aspect_ratio": observation.aspect_ratio,
            "generation_success": observation.generation_success,
            "provider_failure": observation.provider_failure,
            "latency_ms": observation.latency_ms,
            "cost_credits": observation.cost_credits,
            "cost_usd": Decimal(str(observation.cost_usd)) if observation.cost_usd is not None else None,
            "user_rating": observation.user_rating,
            "user_preference_ab": observation.user_preference_ab,
            "user_preference_opponent": observation.user_preference_opponent,
            "regenerated": observation.regenerated,
            "switched_model": observation.switched_model,
            "downloaded": observation.downloaded,
            "accepted_output": observation.accepted_output,
            "used_in_next_shot": observation.used_in_next_shot,
            "qc_identity_score": observation.qc_identity_score,
            "qc_motion_score": observation.qc_motion_score,
            "qc_prompt_alignment": observation.qc_prompt_alignment,
            "qc_temporal_consistency": observation.qc_temporal_consistency,
            "router_version": observation.router_version,
            "router_decision_id": observation.router_decision_id,
            "project_id": observation.project_id,
            "workspace_id": observation.workspace_id,
            "generation_job_id": observation.generation_job_id,
            "shot_id": observation.shot_id,
            "metadata_json": dict(observation.metadata),
            "created_at": now,
            "updated_at": now,
        }

    # ------------------------------------------------------------------
    # reading

    def observations(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        provider: str | None = None,
        limit: int | None = None,
    ) -> list[ProductionObservation]:
        """Read observations back as the contract type, oldest first.

        Ordered by time then id so that a replay's chronological split is
        deterministic even when two attempts share a timestamp.
        """

        statement = select(RouterObservation).order_by(
            RouterObservation.occurred_at, RouterObservation.id
        )
        if since is not None:
            statement = statement.where(RouterObservation.occurred_at >= since)
        if until is not None:
            statement = statement.where(RouterObservation.occurred_at <= until)
        if provider is not None:
            statement = statement.where(RouterObservation.provider == provider)
        if limit is not None:
            statement = statement.limit(limit)
        with self.database.session() as session:
            rows = list(session.scalars(statement))
            return [self._to_contract(row) for row in rows]

    @staticmethod
    def _to_contract(row: RouterObservation) -> ProductionObservation:
        return ProductionObservation(
            observation_id=row.id,
            occurred_at=row.occurred_at,
            provider=row.provider,
            model_id=row.model_id,
            exact_version=row.exact_version,
            model_is_alias=row.model_is_alias,
            task_type=TaskType(row.task_type),
            scenario=Scenario(row.scenario),
            asset_criticality=row.asset_criticality,
            prompt_complexity=PromptComplexity(row.prompt_complexity),
            reference_mode=ReferenceMode(row.reference_mode),
            duration_seconds=row.duration_seconds,
            resolution=row.resolution,
            aspect_ratio=row.aspect_ratio,
            generation_success=row.generation_success,
            provider_failure=row.provider_failure,
            latency_ms=row.latency_ms,
            cost_credits=row.cost_credits,
            cost_usd=float(row.cost_usd) if row.cost_usd is not None else None,
            user_rating=row.user_rating,
            user_preference_ab=cast(
                "Literal['win', 'loss', 'tie'] | None", row.user_preference_ab
            ),
            user_preference_opponent=row.user_preference_opponent,
            regenerated=row.regenerated,
            switched_model=row.switched_model,
            downloaded=row.downloaded,
            accepted_output=row.accepted_output,
            used_in_next_shot=row.used_in_next_shot,
            qc_identity_score=row.qc_identity_score,
            qc_motion_score=row.qc_motion_score,
            qc_prompt_alignment=row.qc_prompt_alignment,
            qc_temporal_consistency=row.qc_temporal_consistency,
            router_version=row.router_version,
            router_decision_id=row.router_decision_id,
            project_id=row.project_id,
            workspace_id=row.workspace_id,
            generation_job_id=row.generation_job_id,
            shot_id=row.shot_id,
            metadata=dict(row.metadata_json or {}),
        )

    # ------------------------------------------------------------------
    # posterior and replay snapshots

    def save_posterior_run(self, run: PosteriorRun) -> int:
        """Persist every cell of one offline run. Returns the row count.

        Runs are immutable: a re-run gets a new ``run_id`` rather than
        overwriting an earlier one, so a decision taken last week can still be
        explained by the numbers that were on file last week.
        """

        written = 0
        now = datetime.now(UTC)
        with self.database.session() as session:
            for record in run.records:
                session.add(
                    RouterPosterior(
                        id=new_id(),
                        run_id=run.run_id,
                        engine_version=record.engine_version,
                        provider=record.key.provider,
                        model_id=record.key.model_id,
                        exact_version=record.key.exact_version,
                        task_type=record.key.task_type.value,
                        scenario=record.key.scenario.value,
                        metric_scale_id=record.key.metric_scale_id,
                        outcome_name=record.outcome.value,
                        level=record.level.value,
                        condition_token=record.condition.token if record.condition else "-",
                        posterior_mean=record.posterior_mean,
                        posterior_lower_quantile=record.posterior_lower_quantile,
                        posterior_upper_quantile=record.posterior_upper_quantile,
                        lower_quantile_level=record.lower_quantile_level,
                        upper_quantile_level=record.upper_quantile_level,
                        effective_sample_size=record.effective_sample_size,
                        observation_count=record.observation_count,
                        alpha=record.alpha,
                        beta=record.beta,
                        prior_alpha=record.prior_alpha,
                        prior_beta=record.prior_beta,
                        prior_sources=list(record.prior_sources),
                        prior_version=record.prior_version,
                        parent_level=record.parent_level.value if record.parent_level else None,
                        parent_mean=record.parent_mean,
                        calculated_at=record.calculated_at,
                        created_at=now,
                        updated_at=now,
                    )
                )
                written += 1
            session.flush()
        return written

    def save_replay(self, result: ReplayResult, *, posterior_run_id: str | None = None) -> RouterReplayRun:
        now = datetime.now(UTC)
        with self.database.session() as session:
            row = RouterReplayRun(
                id=new_id(),
                run_id=result.run_id,
                harness_version="router-replay-v1",
                outcome_name=result.outcome.value,
                posterior_run_id=posterior_run_id,
                fit_observations=result.fit_observations,
                eval_observations=result.eval_observations,
                contexts=result.contexts,
                unscored_contexts=result.unscored_contexts,
                baseline_json=_policy_json(result.baseline),
                posterior_json=_policy_json(result.posterior),
                coverage_json={
                    "nominal": result.coverage.nominal,
                    "observed": result.coverage.observed,
                    "cells_checked": result.coverage.cells_checked,
                    "cells_covered": result.coverage.cells_covered,
                    "cells_below": result.coverage.cells_below,
                    "cells_above": result.coverage.cells_above,
                    "calibrated": result.coverage.calibrated,
                },
                passed=result.passed,
                notes_json=list(result.notes),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return row

    def latest_passing_replay(self, outcome: OutcomeName) -> RouterReplayRun | None:
        """The evidence the LCB flag is allowed to rely on, or nothing.

        Callers use this to answer "may the flag be on?" from data rather than
        from someone's memory of a run.
        """

        with self.database.session() as session:
            return session.scalar(
                select(RouterReplayRun)
                .where(
                    RouterReplayRun.outcome_name == outcome.value,
                    RouterReplayRun.passed.is_(True),
                )
                .order_by(RouterReplayRun.created_at.desc())
                .limit(1)
            )

    def posterior_records(self, run_id: str) -> list[RouterPosterior]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(RouterPosterior)
                    .where(RouterPosterior.run_id == run_id)
                    .order_by(RouterPosterior.id)
                )
            )

    def latest_posterior_run_id(self) -> str | None:
        with self.database.session() as session:
            return session.scalar(
                select(RouterPosterior.run_id)
                .order_by(RouterPosterior.calculated_at.desc(), RouterPosterior.id.desc())
                .limit(1)
            )

    def lookup_for(self, run_id: str) -> PosteriorLookup:
        """Rebuild the in-memory lookup the LCB reads, from one saved run.

        Called once per snapshot and cached by the caller, never per request:
        the offline/online split is only real if the routing path is not
        querying this table while a user waits.
        """

        return PosteriorLookup([_to_record(row) for row in self.posterior_records(run_id)])

    def coverage_counts(self) -> dict[str, int]:
        """Observations per ``provider:model_id@exact_version``, for the report."""

        with self.database.session() as session:
            rows = list(
                session.execute(
                    select(
                        RouterObservation.provider,
                        RouterObservation.model_id,
                        RouterObservation.exact_version,
                    )
                )
            )
        counts: dict[str, int] = {}
        for provider, model_id, exact_version in rows:
            token = f"{provider}:{model_id}@{exact_version}"
            counts[token] = counts.get(token, 0) + 1
        return dict(sorted(counts.items()))


def _to_record(row: RouterPosterior) -> PosteriorRecord:
    """Turn a saved row back into the dataclass the LCB understands.

    ``external_contributions`` is dropped rather than reconstructed: the row
    keeps the pseudo-counts those contributions produced, in ``prior_alpha``
    and ``prior_beta``, and the per-layer breakdown is an audit detail that no
    routing decision reads. Rebuilding it from a list of layer names would
    invent an apportionment the row does not record.
    """

    condition: ConditionBucket | None = None
    if row.condition_token and row.condition_token != "-":
        duration, resolution, reference_mode = row.condition_token.split("|")
        condition = ConditionBucket(
            duration_bucket=duration,  # type: ignore[arg-type]
            resolution=resolution,
            reference_mode=ReferenceMode(reference_mode),
        )
    return PosteriorRecord(
        key=EvidenceKey(
            provider=row.provider,
            model_id=row.model_id,
            exact_version=row.exact_version,
            task_type=TaskType(row.task_type),
            scenario=Scenario(row.scenario),
            metric_scale_id=row.metric_scale_id,
        ),
        outcome=OutcomeName(row.outcome_name),
        level=PosteriorLevel(row.level),
        condition=condition,
        posterior_mean=row.posterior_mean,
        posterior_lower_quantile=row.posterior_lower_quantile,
        posterior_upper_quantile=row.posterior_upper_quantile,
        lower_quantile_level=row.lower_quantile_level,
        upper_quantile_level=row.upper_quantile_level,
        effective_sample_size=row.effective_sample_size,
        observation_count=row.observation_count,
        alpha=row.alpha,
        beta=row.beta,
        prior_alpha=row.prior_alpha,
        prior_beta=row.prior_beta,
        prior_sources=tuple(row.prior_sources or ()),
        prior_version=row.prior_version,
        external_contributions=(),
        parent_level=PosteriorLevel(row.parent_level) if row.parent_level else None,
        parent_mean=row.parent_mean,
        calculated_at=row.calculated_at,
        engine_version=row.engine_version,
    )


def _policy_json(policy: Any) -> dict[str, Any]:
    return {
        "name": policy.name,
        "scored_contexts": policy.scored_contexts,
        "abstentions": policy.abstentions,
        "fell_back": policy.fell_back,
        "mean_regret": policy.mean_regret,
        "quality_mean": policy.quality_mean,
        "failure_rate": policy.failure_rate,
        "cost_credits_mean": policy.cost_credits_mean,
        "chosen_models": dict(policy.chosen_models),
    }


def condition_of(observation: ProductionObservation) -> ConditionBucket:
    return observation.conditions


def posterior_levels_written(run: PosteriorRun) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys((level.value for level in PosteriorLevel), 0)
    for record in run.records:
        counts[record.level.value] += 1
    return counts


def summarize_observations(observations: Sequence[ProductionObservation]) -> dict[str, Any]:
    """Volume and shape of what has been collected, for the coverage report."""

    versions = {f"{item.provider}:{item.model_id}@{item.exact_version}" for item in observations}
    return {
        "observations": len(observations),
        "distinct_versions": len(versions),
        "distinct_scenarios": len({item.scenario.value for item in observations}),
        "distinct_tasks": len({item.task_type.value for item in observations}),
        "successes": sum(1 for item in observations if item.generation_success),
        "provider_failures": sum(1 for item in observations if item.provider_failure),
    }


__all__ = [
    "RouterObservationService",
    "UnattributableObservation",
    "condition_of",
    "posterior_levels_written",
    "summarize_observations",
]
