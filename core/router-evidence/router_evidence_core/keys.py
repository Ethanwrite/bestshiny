"""The isolation key, and the vocabularies it is built from.

Every number this package handles — an official capability claim, a benchmark
score, a community opinion, a production outcome — is addressed by a key. Two
numbers may only meet each other if their keys are equal. That single rule is
what the mandate calls model/version/task/scenario/scale isolation, and keeping
it in one place is the only way to check it mechanically.

The key is deliberately wider than the router's own ``provider:model_id``.
The router ranks a *model*; this package reasons about a *measurement*, and a
measurement of Veo 3.1 Fast on image-to-video camera motion at 1080P is not a
measurement of Veo 3.1 on text-to-video dialogue, however similar the marketing
page makes them look.

Nothing here normalises across scales. ``MetricScale`` records how a number was
measured so that a Likert 3.75 and an Elo 1154 can be carried side by side
without either being converted into the other. A conversion needs a calibration
bridge, and this platform has none — see ``calibration.py``.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TaskType(StrEnum):
    """What was asked of the model, not what the model can do in general."""

    T2V = "T2V"
    I2V = "I2V"
    R2V = "R2V"
    V2V = "V2V"
    T2I = "T2I"
    I2I = "I2I"
    R2I = "R2I"
    #: Not a task. The slot filler for a key that deliberately spans tasks —
    #: a version-level posterior, for instance. Named rather than borrowing a
    #: real value, because a row reading "T2V" when it means "all of them" is
    #: a lie that survives every later join.
    ANY = "ANY"


VIDEO_TASKS: frozenset[TaskType] = frozenset({TaskType.T2V, TaskType.I2V, TaskType.R2V, TaskType.V2V})
IMAGE_TASKS: frozenset[TaskType] = frozenset({TaskType.T2I, TaskType.I2I, TaskType.R2I})


class Scenario(StrEnum):
    """The thirteen scenes the router is asked to distinguish, plus a holistic one.

    ``GENERIC`` exists because most public evidence is holistic — an Arena Elo
    is not a measurement of physics — and forcing such a number into a specific
    scene would be the fabrication this package exists to prevent.
    """

    MOTION = "motion"
    PHYSICS = "physics"
    HUMAN_CONSISTENCY = "human_consistency"
    IDENTITY = "identity"
    CAMERA_MOTION = "camera_motion"
    PROMPT_ADHERENCE = "prompt_adherence"
    TEXT_RENDERING = "text_rendering"
    CHINESE_TEXT = "chinese_text"
    DIALOGUE_LIPSYNC = "dialogue_lipsync"
    REFERENCE_ADHERENCE = "reference_adherence"
    FIRST_LAST_FRAME = "first_last_frame"
    CINEMATIC_QUALITY = "cinematic_quality"
    COMMERCIAL_PRODUCT = "commercial_product"
    PORTRAIT = "portrait"
    GENERIC = "generic"
    #: The same slot filler as ``TaskType.ANY``, and distinct from ``GENERIC``:
    #: "generic" is a real holistic measurement, "ANY" is an aggregate over
    #: scenes that were each measured separately.
    ANY = "ANY"


class ReferenceMode(StrEnum):
    """How the request was conditioned. Part of the key, not a detail.

    A model that holds identity beautifully from a reference image and loses it
    from a text description is two different measurements, and pooling them
    produces a number that describes neither.
    """

    NONE = "NONE"
    FIRST_FRAME = "FIRST_FRAME"
    LAST_FRAME = "LAST_FRAME"
    FIRST_LAST_FRAME = "FIRST_LAST_FRAME"
    REFERENCE_IMAGE = "REFERENCE_IMAGE"
    MULTI_REFERENCE = "MULTI_REFERENCE"
    REFERENCE_VIDEO = "REFERENCE_VIDEO"
    REFERENCE_VOICE = "REFERENCE_VOICE"


class ScaleKind(StrEnum):
    RATIO_0_1 = "RATIO_0_1"
    PERCENT_0_100 = "PERCENT_0_100"
    LIKERT_1_5 = "LIKERT_1_5"
    LIKERT_1_10 = "LIKERT_1_10"
    WIN_RATE = "WIN_RATE"
    ELO = "ELO"
    RANK = "RANK"
    BINARY = "BINARY"
    COUNT = "COUNT"
    ORDINAL_STANCE = "ORDINAL_STANCE"


#: Scales whose values can be placed on a common [0, 1] axis *within one scale*
#: without assuming anything about another scale. ELO, RANK and COUNT are
#: absent on purpose: an Elo has no upper bound, a rank depends on who else
#: entered, and a count is not a rate. They are carried, reported and never
#: turned into a probability.
BOUNDED_SCALE_KINDS: frozenset[ScaleKind] = frozenset(
    {
        ScaleKind.RATIO_0_1,
        ScaleKind.PERCENT_0_100,
        ScaleKind.LIKERT_1_5,
        ScaleKind.LIKERT_1_10,
        ScaleKind.WIN_RATE,
        ScaleKind.BINARY,
        ScaleKind.ORDINAL_STANCE,
    }
)

_SCALE_BOUNDS: dict[ScaleKind, tuple[float, float]] = {
    ScaleKind.RATIO_0_1: (0.0, 1.0),
    ScaleKind.PERCENT_0_100: (0.0, 100.0),
    ScaleKind.LIKERT_1_5: (1.0, 5.0),
    ScaleKind.LIKERT_1_10: (1.0, 10.0),
    ScaleKind.WIN_RATE: (0.0, 1.0),
    ScaleKind.BINARY: (0.0, 1.0),
    ScaleKind.ORDINAL_STANCE: (-1.0, 1.0),
}

_SCALE_ID = re.compile(r"^[a-z0-9]+(?:[-.][a-z0-9]+)*$")


class MetricScale(BaseModel):
    """One measurement scale, named so that it can be refused a partner.

    ``scale_id`` is the isolation token. ``vbench2-total-0-1`` and
    ``arena-elo-video`` are different scales even when both claim to measure
    "quality", and the posterior machinery will not put them in the same
    distribution.
    """

    model_config = ConfigDict(frozen=True)

    scale_id: str = Field(min_length=1, max_length=80)
    kind: ScaleKind
    higher_is_better: bool = True
    description: str = Field(min_length=1)

    @field_validator("scale_id")
    @classmethod
    def _lowercase_token(cls, value: str) -> str:
        if not _SCALE_ID.match(value):
            raise ValueError(
                f"scale_id {value!r} must be a lowercase dotted/hyphenated token, "
                "so that it can never be confused with a metric's display name"
            )
        return value

    @property
    def bounded(self) -> bool:
        return self.kind in BOUNDED_SCALE_KINDS

    def to_unit(self, value: float) -> float:
        """Place a value on [0, 1] *within this scale*.

        This is not a conversion between scales. It is the affine map from a
        scale's own declared bounds onto the unit interval, which is what a
        Beta posterior needs in order to exist at all. Two values mapped this
        way are still not comparable unless they came from the same
        ``scale_id`` — the posterior store enforces that, not this method.
        """

        if not self.bounded:
            raise ValueError(
                f"scale {self.scale_id} ({self.kind}) is unbounded; it has no unit representation "
                "and may not be turned into a probability"
            )
        low, high = _SCALE_BOUNDS[self.kind]
        if not low <= value <= high:
            raise ValueError(f"value {value} is outside the bounds of scale {self.scale_id}: [{low}, {high}]")
        unit = (value - low) / (high - low)
        return unit if self.higher_is_better else 1.0 - unit


class EvidenceKey(BaseModel):
    """The address of a measurement. Equality here is permission to combine.

    ``exact_version`` is required and may not be an alias. An alias is a name
    that a provider is free to repoint — ``google/veo-3.1`` today and something
    else after a silent refresh — and evidence attached to an alias describes
    whatever was behind it on the day it was measured. The alias is kept in
    ``model_id``; the snapshot goes here.
    """

    model_config = ConfigDict(frozen=True)

    provider: str = Field(min_length=1, max_length=80)
    model_id: str = Field(min_length=1, max_length=160)
    exact_version: str = Field(min_length=1, max_length=120)
    task_type: TaskType
    scenario: Scenario
    metric_scale_id: str = Field(min_length=1, max_length=80)

    @property
    def router_model_key(self) -> str:
        """The narrower key the existing router uses.

        Collapsing to it loses the version, task and scenario, so it happens at
        exactly one place — the moment a posterior is handed to the router for
        one specific request — and never during aggregation.
        """

        return f"{self.provider}:{self.model_id}"

    @property
    def token(self) -> str:
        return "|".join(
            (
                self.provider,
                self.model_id,
                self.exact_version,
                self.task_type.value,
                self.scenario.value,
                self.metric_scale_id,
            )
        )

    def __str__(self) -> str:  # pragma: no cover - debugging affordance
        return self.token


class ConditionBucket(BaseModel):
    """Generation conditions that change what an outcome means.

    Duration is bucketed rather than kept exact because a 5.0s and a 5.2s clip
    are the same experiment and would otherwise never pool; resolution and
    reference mode are kept exact because they are not.
    """

    model_config = ConfigDict(frozen=True)

    duration_bucket: Literal["<=2s", "2-5s", "5-8s", "8-12s", ">12s", "n/a"] = "n/a"
    #: No pipe, because `token` joins on one and the posterior unpacks the
    #: result back into three fields. A resolution containing a separator would
    #: not corrupt one cell, it would raise out of the whole computation.
    resolution: str = Field(default="n/a", min_length=1, max_length=32, pattern=r"^[^|]+$")
    reference_mode: ReferenceMode = ReferenceMode.NONE

    @staticmethod
    def bucket_duration(seconds: float | None) -> Literal["<=2s", "2-5s", "5-8s", "8-12s", ">12s", "n/a"]:
        if seconds is None:
            return "n/a"
        if seconds <= 2:
            return "<=2s"
        if seconds <= 5:
            return "2-5s"
        if seconds <= 8:
            return "5-8s"
        if seconds <= 12:
            return "8-12s"
        return ">12s"

    @property
    def token(self) -> str:
        return f"{self.duration_bucket}|{self.resolution}|{self.reference_mode.value}"


__all__ = [
    "BOUNDED_SCALE_KINDS",
    "IMAGE_TASKS",
    "VIDEO_TASKS",
    "ConditionBucket",
    "EvidenceKey",
    "MetricScale",
    "ReferenceMode",
    "ScaleKind",
    "Scenario",
    "TaskType",
]
