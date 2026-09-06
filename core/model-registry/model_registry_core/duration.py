"""Execution duration: the length a model actually runs for a requested one.

A shot's ``duration`` is the director's intent and stays on the canonical shot
untouched. Which model renders it is decided by the router, and a model
declares what it can run: a continuous range (``min_duration`` /
``max_duration``, per profile or per mode) and sometimes a discrete set
(``provider_metadata.supported_durations`` - Veo publishes ``[4, 6, 8]``). The
plan below maps intent onto capability:

* ``EXACT`` - the requested length is legal as it is.
* ``SNAP_UP`` - the request is inside the range but not on a legal value, or
  below the minimum: the shortest legal length that still holds the whole
  action. Never down, because cutting a shot short truncates its narrative.
* ``SPLIT_SHOT`` - the request exceeds the model's ceiling: the shot would have
  to run as several consecutive segments, each a legal length. The router
  reports the segment plan; it does not select such a model, because running
  one shot as two provider calls is a planning decision with its own
  continuity and assembly obligations, not a routing detail.

Nothing here rewrites the requested figure. The quote, the reservation and the
provider payload use the execution length; the shot keeps the request.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

EXACT = "EXACT"
SNAP_UP = "SNAP_UP"
SPLIT_SHOT = "SPLIT_SHOT"


@dataclass(frozen=True)
class ExecutionDurationPlan:
    requested_duration: float
    strategy: str
    #: What one provider call runs (EXACT / SNAP_UP), or the sum of the
    #: segments (SPLIT_SHOT).
    execution_duration: float
    segments: tuple[float, ...]
    detail: str = ""

    @property
    def runnable(self) -> bool:
        """Whether one provider call can carry the shot."""

        return self.strategy != SPLIT_SHOT


def legal_durations(
    supported_durations: Sequence[float] | None,
    *,
    min_duration: float | None,
    max_duration: float | None,
) -> list[float]:
    """The declared discrete lengths that also respect the range, ascending."""

    values: list[float] = []
    for item in supported_durations or []:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            continue
        value = float(item)
        if value <= 0:
            continue
        if min_duration is not None and value < min_duration:
            continue
        if max_duration is not None and value > max_duration:
            continue
        if value not in values:
            values.append(value)
    return sorted(values)


def _round(value: float) -> float:
    return round(float(value), 2)


def plan_execution_duration(
    requested: float,
    *,
    min_duration: float | None = None,
    max_duration: float | None = None,
    supported_durations: Sequence[float] | None = None,
) -> ExecutionDurationPlan:
    """Map a requested length onto what the model can run."""

    wanted = float(requested)
    legal = legal_durations(
        supported_durations, min_duration=min_duration, max_duration=max_duration
    )
    if legal:
        if wanted in legal:
            return ExecutionDurationPlan(wanted, EXACT, wanted, (wanted,))
        longer = [value for value in legal if value >= wanted]
        if longer:
            chosen = longer[0]
            return ExecutionDurationPlan(
                wanted,
                SNAP_UP,
                chosen,
                (chosen,),
                f"{wanted:g}s is not a declared length; the model runs {chosen:g}s",
            )
        ceiling = legal[-1]
        count = max(2, math.ceil(wanted / ceiling))
        per_segment = wanted / count
        segments = tuple(next(value for value in legal if value >= per_segment) for _ in range(count))
        return ExecutionDurationPlan(
            wanted,
            SPLIT_SHOT,
            _round(sum(segments)),
            segments,
            f"{wanted:g}s exceeds the {ceiling:g}s ceiling; SPLIT_SHOT would run "
            f"{count} segments of {', '.join(f'{item:g}s' for item in segments)}",
        )
    if min_duration is not None and wanted < min_duration:
        return ExecutionDurationPlan(
            wanted,
            SNAP_UP,
            float(min_duration),
            (float(min_duration),),
            f"{wanted:g}s is below the {min_duration:g}s minimum; the model runs {min_duration:g}s",
        )
    if max_duration is not None and wanted > max_duration:
        count = max(2, math.ceil(wanted / float(max_duration)))
        per_segment = wanted / count
        if min_duration is not None:
            per_segment = max(per_segment, float(min_duration))
        segments = tuple(_round(per_segment) for _ in range(count))
        return ExecutionDurationPlan(
            wanted,
            SPLIT_SHOT,
            _round(sum(segments)),
            segments,
            f"{wanted:g}s exceeds the {max_duration:g}s ceiling; SPLIT_SHOT would run "
            f"{count} segments of {', '.join(f'{item:g}s' for item in segments)}",
        )
    return ExecutionDurationPlan(wanted, EXACT, wanted, (wanted,))


__all__ = [
    "EXACT",
    "SNAP_UP",
    "SPLIT_SHOT",
    "ExecutionDurationPlan",
    "legal_durations",
    "plan_execution_duration",
]
