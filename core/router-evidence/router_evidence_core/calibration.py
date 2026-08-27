"""Bridges between measurement scales — and the fact that there are none.

Two numbers on different scales can only be compared if something establishes
the exchange rate between them: the same models measured on both scales, on
overlapping material, with the mapping published. That artefact is a
calibration bridge, and this platform does not have one for any pair of scales
it holds.

The module exists anyway, for two reasons. It gives the posterior machinery a
single place to ask "may I pool these?" and receive a definite no, rather than
each call site deciding for itself. And it makes the absence a visible, dated,
reviewable fact rather than an omission — ``BRIDGES`` being empty is a
statement, and the report prints it.

Until a bridge exists, each ``metric_scale_id`` gets its own posterior and they
are reported side by side. That is less convenient than a single number and it
is the only honest arrangement.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationBridge:
    """A published, evidenced mapping from one scale onto another.

    ``anchor_count`` is the number of models measured on *both* scales. A
    bridge built on two anchors is a line through two points and should be
    treated as such; the field is here so that nobody has to guess.
    """

    from_scale_id: str
    to_scale_id: str
    method: str
    anchor_count: int
    source_url: str
    established_at: str
    rationale: str


#: Empty, deliberately, as of 2026-08-26. Adding an entry is a research act
#: with a source, not a convenience.
BRIDGES: tuple[CalibrationBridge, ...] = ()


def bridge_between(from_scale_id: str, to_scale_id: str) -> CalibrationBridge | None:
    if from_scale_id == to_scale_id:
        return None
    for bridge in BRIDGES:
        if bridge.from_scale_id == from_scale_id and bridge.to_scale_id == to_scale_id:
            return bridge
    return None


def may_pool(from_scale_id: str, to_scale_id: str) -> bool:
    """Whether two scales' values may enter the same distribution.

    Identical scale ids may. Anything else needs a bridge, and there are none,
    so this returns False for every cross-scale pair on the platform today.
    """

    if from_scale_id == to_scale_id:
        return True
    return bridge_between(from_scale_id, to_scale_id) is not None


__all__ = ["BRIDGES", "CalibrationBridge", "bridge_between", "may_pool"]
