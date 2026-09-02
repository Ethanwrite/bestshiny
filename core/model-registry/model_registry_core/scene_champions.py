"""The hand-authored scene-champion table the video router selects within.

The router does not ask "which video model is best?"; it asks "which model is
the champion for *this kind of scene*?". This module holds that table: for each
evidence scenario (the same :class:`~router_evidence_core.keys.Scenario` the
production posterior groups by), an ordered list of champions — primary first,
then fallbacks. The router restricts its choice to the champions that survive
the deterministic hard filter, in table order, and only reorders them when
production evidence is both sufficient (``min_demotion_samples`` observations
on each side) and decisive (a blended-score gap above ``demotion_margin``).

That inversion is deliberate. Open scoring over a dozen models makes the
selection a function of hand-authored priors that mostly have no diagnostic
evidence behind them (OPEN_ISSUES §2.25); a manual champion table makes the
selection a recorded product judgement that data then *corrects*, rather than a
ranking data must *discover*. With a few dozen early observations the table
holds; with sufficient evidence the demotion rule lets the fallback overtake.

The table is config (``config/model-registry/scene-champions.json``), keyed by
``logical_name`` — the registry's canonical model id — and validated against
the :class:`~router_evidence_core.keys.Scenario` vocabulary so a typo'd scene
cannot silently become dead policy.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from router_evidence_core import Scenario


class ChampionBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    logical_name: str = Field(min_length=1)
    # Hand-authored judgement is only auditable if it says why. The rationale
    # is carried, not interpreted.
    rationale: str = Field(min_length=1)


class SceneChampions(BaseModel):
    model_config = ConfigDict(frozen=True)

    champions: list[ChampionBinding] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct_champions(self) -> SceneChampions:
        names = [binding.logical_name for binding in self.champions]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate champion within one scene: {names}")
        return self


class SceneChampionTable(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str = Field(min_length=1)
    # A champion is demoted below its own fallback only when both sides carry
    # at least ``min_demotion_samples`` production observations and the blended
    # score gap exceeds ``demotion_margin``. Below either threshold the manual
    # order holds — early data adjusts nothing, which is what keeps a
    # few-dozen-sample cold start from thrashing the routing.
    demotion_margin: float = Field(default=0.05, gt=0, le=0.5)
    min_demotion_samples: int = Field(default=20, ge=1)
    scenes: dict[str, SceneChampions]

    @field_validator("scenes")
    @classmethod
    def _scene_keys_are_scenarios(
        cls, value: dict[str, SceneChampions]
    ) -> dict[str, SceneChampions]:
        valid = {scenario.value for scenario in Scenario} - {Scenario.ANY.value}
        unknown = sorted(set(value) - valid)
        if unknown:
            raise ValueError(f"unknown scenario keys: {unknown}; valid: {sorted(valid)}")
        return value

    def champions_for(self, scenario: Scenario) -> SceneChampions | None:
        return self.scenes.get(scenario.value)


def load_scene_champions(path: Path) -> SceneChampionTable:
    """Read and validate the champion table; fails closed on any defect.

    A missing or malformed table is a configuration error, not a degraded
    mode — routing policy silently reverting to open scoring is exactly the
    surprise this table exists to prevent.
    """

    with Path(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return SceneChampionTable.model_validate(payload)


__all__ = [
    "ChampionBinding",
    "SceneChampionTable",
    "SceneChampions",
    "load_scene_champions",
]
