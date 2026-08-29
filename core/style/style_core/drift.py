"""Aggregate style drift across episodes: the walk no single shot reveals.

Candidate-level evaluation (OPEN_ISSUES 2.2) gates each shot against the
locked reference, but every shot can individually pass while the series
slides — each episode a fraction further from episode 1's look. This monitor
reads the append-only ``candidate_style_evaluations`` that the commit gate
already produces, aggregates them per episode over **committed** candidates
(the series as adopted, not as attempted), and reports drift against the
earliest episode with enough evidence.

Monitoring only, on purpose: it changes no gate and blocks nothing. It gives
the operator the aggregate number nobody was computing — per-episode means
for both layers, drift from baseline, the flagged episodes, and the length
of the current decline streak. Seasons are not modeled in the schema; the
per-episode series is the cross-episode axis that exists, and a season
boundary is a reading of it, not a column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from platform_database import Database
from production_domain.models import (
    CandidateStatus,
    CandidateStyleEvaluation,
    Episode,
    GenerationCandidate,
    Scene,
    Shot,
)
from sqlalchemy import select

#: Drop from the baseline episode's mean similarity that flags an episode.
#: The deterministic layer's scores are cosine-like in [0, 1]; 0.05 is a
#: visible aggregate move. Uncalibrated against live model output like every
#: similarity threshold in this system — reported, not enforced.
DEFAULT_DRIFT_THRESHOLD = 0.05

#: An episode needs this many committed, evaluated candidates before its mean
#: is treated as evidence rather than noise.
DEFAULT_MIN_CANDIDATES = 1


@dataclass(frozen=True)
class EpisodeStyleAggregate:
    episode_number: int
    episode_id: str
    committed_evaluations: int
    mean_similarity: float | None
    min_similarity: float | None
    mean_semantic_similarity: float | None
    drift_from_baseline: float | None = None
    flagged: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "episode_number": self.episode_number,
            "episode_id": self.episode_id,
            "committed_evaluations": self.committed_evaluations,
            "mean_similarity": self.mean_similarity,
            "min_similarity": self.min_similarity,
            "mean_semantic_similarity": self.mean_semantic_similarity,
            "drift_from_baseline": self.drift_from_baseline,
            "flagged": self.flagged,
        }


@dataclass(frozen=True)
class SeriesStyleDriftReport:
    project_id: str
    status: str  # INSUFFICIENT_DATA | STABLE | DRIFTING
    baseline_episode_number: int | None
    drift_threshold: float
    episodes: list[EpisodeStyleAggregate] = field(default_factory=list)
    flagged_episode_numbers: list[int] = field(default_factory=list)
    max_drift: float | None = None
    decline_streak: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "status": self.status,
            "baseline_episode_number": self.baseline_episode_number,
            "drift_threshold": self.drift_threshold,
            "episodes": [episode.as_dict() for episode in self.episodes],
            "flagged_episode_numbers": self.flagged_episode_numbers,
            "max_drift": self.max_drift,
            "decline_streak": self.decline_streak,
            "monitor_version": StyleDriftMonitor.version,
        }


class StyleDriftMonitor:
    version = "style-drift-monitor-v1"

    def __init__(self, database: Database):
        self.database = database

    def series_report(
        self,
        project_id: str,
        *,
        drift_threshold: float = DEFAULT_DRIFT_THRESHOLD,
        min_candidates: int = DEFAULT_MIN_CANDIDATES,
    ) -> SeriesStyleDriftReport:
        """Per-episode aggregates of committed style evidence, with drift flags."""

        with self.database.session() as session:
            rows = session.execute(
                select(
                    Episode.episode_number,
                    Episode.id,
                    CandidateStyleEvaluation.average_similarity,
                    CandidateStyleEvaluation.minimum_similarity,
                    CandidateStyleEvaluation.semantic_average_similarity,
                )
                .join(Scene, Scene.episode_id == Episode.id)
                .join(Shot, Shot.scene_id == Scene.id)
                .join(GenerationCandidate, GenerationCandidate.shot_id == Shot.id)
                .join(
                    CandidateStyleEvaluation,
                    CandidateStyleEvaluation.candidate_id == GenerationCandidate.id,
                )
                .where(
                    Episode.project_id == project_id,
                    # The adopted series, not every attempt: rejected takes do
                    # not move the canon's look.
                    GenerationCandidate.status == CandidateStatus.COMMITTED.value,
                )
                .order_by(Episode.episode_number)
            ).all()

        per_episode: dict[int, dict[str, Any]] = {}
        for episode_number, episode_id, average, minimum, semantic in rows:
            bucket = per_episode.setdefault(
                episode_number,
                {"episode_id": episode_id, "averages": [], "minimums": [], "semantic": []},
            )
            if average is not None:
                bucket["averages"].append(float(average))
            if minimum is not None:
                bucket["minimums"].append(float(minimum))
            if semantic is not None:
                bucket["semantic"].append(float(semantic))

        aggregates: list[EpisodeStyleAggregate] = []
        baseline_mean: float | None = None
        baseline_number: int | None = None
        for number in sorted(per_episode):
            bucket = per_episode[number]
            count = len(bucket["averages"])
            mean = sum(bucket["averages"]) / count if count else None
            aggregate = EpisodeStyleAggregate(
                episode_number=number,
                episode_id=bucket["episode_id"],
                committed_evaluations=count,
                mean_similarity=round(mean, 6) if mean is not None else None,
                min_similarity=round(min(bucket["minimums"]), 6) if bucket["minimums"] else None,
                mean_semantic_similarity=(
                    round(sum(bucket["semantic"]) / len(bucket["semantic"]), 6)
                    if bucket["semantic"]
                    else None
                ),
            )
            if baseline_mean is None and mean is not None and count >= max(1, min_candidates):
                baseline_mean = mean
                baseline_number = number
            aggregates.append(aggregate)

        flagged: list[int] = []
        max_drift: float | None = None
        enriched: list[EpisodeStyleAggregate] = []
        for aggregate in aggregates:
            drift: float | None = None
            is_flagged = False
            if (
                baseline_mean is not None
                and aggregate.mean_similarity is not None
                and aggregate.episode_number != baseline_number
                and aggregate.committed_evaluations >= max(1, min_candidates)
            ):
                drift = round(baseline_mean - aggregate.mean_similarity, 6)
                max_drift = drift if max_drift is None else max(max_drift, drift)
                if drift > drift_threshold:
                    is_flagged = True
                    flagged.append(aggregate.episode_number)
            enriched.append(
                EpisodeStyleAggregate(
                    episode_number=aggregate.episode_number,
                    episode_id=aggregate.episode_id,
                    committed_evaluations=aggregate.committed_evaluations,
                    mean_similarity=aggregate.mean_similarity,
                    min_similarity=aggregate.min_similarity,
                    mean_semantic_similarity=aggregate.mean_semantic_similarity,
                    drift_from_baseline=drift,
                    flagged=is_flagged,
                )
            )

        decline_streak = 0
        means = [item.mean_similarity for item in enriched if item.mean_similarity is not None]
        for previous, current in zip(means, means[1:], strict=False):
            decline_streak = decline_streak + 1 if current < previous else 0

        if baseline_mean is None or len([m for m in means if m is not None]) < 2:
            status = "INSUFFICIENT_DATA"
        elif flagged:
            status = "DRIFTING"
        else:
            status = "STABLE"
        return SeriesStyleDriftReport(
            project_id=project_id,
            status=status,
            baseline_episode_number=baseline_number,
            drift_threshold=drift_threshold,
            episodes=enriched,
            flagged_episode_numbers=flagged,
            max_drift=max_drift,
            decline_streak=decline_streak,
        )


__all__ = [
    "DEFAULT_DRIFT_THRESHOLD",
    "DEFAULT_MIN_CANDIDATES",
    "EpisodeStyleAggregate",
    "SeriesStyleDriftReport",
    "StyleDriftMonitor",
]
