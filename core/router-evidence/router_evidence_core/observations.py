"""The production observation contract.

One row per generation attempt, carrying the conditions the attempt ran under
and every outcome that was actually observed. It replaces nothing: the existing
``model_metrics`` table keeps recording what it records and keeps feeding the
adaptive router exactly as before. This is a second, wider record written
alongside it, because the old one cannot answer the question this work is
about.

What the old shape cannot express, and this one must:

* which *snapshot* ran — ``model_metrics`` has a ``model_version`` column that
  is empty on almost every row, and an outcome attributed to "wan-2.7" that
  actually came from an alias is contamination that no later analysis can undo;
* what was asked — a text-to-video failure and an image-to-video failure are
  different failures;
* under what conditions — duration, resolution and reference mode change what
  a success rate means;
* and what the user did next, which is the only outcome signal that is not an
  opinion about a still frame.

Every outcome is optional. A generation that failed at the provider has no
quality score and no user rating, and writing a zero there would be an
invention; ``None`` means not observed and is excluded from that outcome's
posterior rather than counted as a bad result.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .keys import ConditionBucket, EvidenceKey, MetricScale, ReferenceMode, ScaleKind, Scenario, TaskType


class PromptComplexity(StrEnum):
    SIMPLE = "SIMPLE"
    MODERATE = "MODERATE"
    COMPLEX = "COMPLEX"
    MULTI_CONSTRAINT = "MULTI_CONSTRAINT"


class OutcomeName(StrEnum):
    """The named outcomes a posterior can be computed for.

    Latency and cost are absent by design. They are real outcomes and they are
    recorded on every observation, but they are unbounded continuous
    quantities and a Beta posterior over them would be a category error. They
    are summarised separately, in their own units, by
    :class:`~router_evidence_core.posterior.CostLatencySummary`.
    """

    GENERATION_SUCCESS = "generation_success"
    PROVIDER_FAILURE = "provider_failure"
    ACCEPTED_OUTPUT = "accepted_output"
    REGENERATED = "regenerated"
    SWITCHED_MODEL = "switched_model"
    DOWNLOADED = "downloaded"
    USED_IN_NEXT_SHOT = "used_in_next_shot"
    USER_RATING = "user_rating"
    USER_PREFERENCE_AB = "user_preference_ab"
    QC_IDENTITY = "qc_identity_score"
    QC_MOTION = "qc_motion_score"
    QC_PROMPT_ALIGNMENT = "qc_prompt_alignment"
    QC_TEMPORAL_CONSISTENCY = "qc_temporal_consistency"


#: Each outcome's own scale, with its own id. The ``prod.`` prefix is not
#: decoration: it guarantees that no production outcome can ever share a
#: ``metric_scale_id`` with an external benchmark, so the posterior store's
#: same-scale rule blocks the mixture structurally rather than by convention.
OUTCOME_SCALES: dict[OutcomeName, MetricScale] = {
    OutcomeName.GENERATION_SUCCESS: MetricScale(
        scale_id="prod.generation-success",
        kind=ScaleKind.BINARY,
        higher_is_better=True,
        description="The provider returned a usable artefact for this attempt.",
    ),
    OutcomeName.PROVIDER_FAILURE: MetricScale(
        scale_id="prod.provider-failure",
        kind=ScaleKind.BINARY,
        higher_is_better=False,
        description="The attempt failed inside the provider rather than in our own pipeline.",
    ),
    OutcomeName.ACCEPTED_OUTPUT: MetricScale(
        scale_id="prod.accepted-output",
        kind=ScaleKind.BINARY,
        higher_is_better=True,
        description="A human accepted the result as the take for this shot.",
    ),
    OutcomeName.REGENERATED: MetricScale(
        scale_id="prod.regenerated",
        kind=ScaleKind.BINARY,
        higher_is_better=False,
        description="The user asked for another take of the same shot from the same model.",
    ),
    OutcomeName.SWITCHED_MODEL: MetricScale(
        scale_id="prod.switched-model",
        kind=ScaleKind.BINARY,
        higher_is_better=False,
        description="The user moved this shot to a different model after seeing the result.",
    ),
    OutcomeName.DOWNLOADED: MetricScale(
        scale_id="prod.downloaded",
        kind=ScaleKind.BINARY,
        higher_is_better=True,
        description="The artefact was downloaded.",
    ),
    OutcomeName.USED_IN_NEXT_SHOT: MetricScale(
        scale_id="prod.used-in-next-shot",
        kind=ScaleKind.BINARY,
        higher_is_better=True,
        description="A frame from this take became the reference or start frame of the following shot.",
    ),
    OutcomeName.USER_RATING: MetricScale(
        scale_id="prod.user-rating-1-5",
        kind=ScaleKind.LIKERT_1_5,
        higher_is_better=True,
        description="Explicit 1-5 star rating left by the user.",
    ),
    OutcomeName.USER_PREFERENCE_AB: MetricScale(
        scale_id="prod.preference-ab",
        kind=ScaleKind.WIN_RATE,
        higher_is_better=True,
        description="Head-to-head choice against another model on the same shot; a tie counts as half.",
    ),
    OutcomeName.QC_IDENTITY: MetricScale(
        scale_id="prod.qc-identity-0-1",
        kind=ScaleKind.RATIO_0_1,
        higher_is_better=True,
        description="Automated identity-consistency check against the canonical character asset.",
    ),
    OutcomeName.QC_MOTION: MetricScale(
        scale_id="prod.qc-motion-0-1",
        kind=ScaleKind.RATIO_0_1,
        higher_is_better=True,
        description="Automated motion-plausibility check.",
    ),
    OutcomeName.QC_PROMPT_ALIGNMENT: MetricScale(
        scale_id="prod.qc-prompt-alignment-0-1",
        kind=ScaleKind.RATIO_0_1,
        higher_is_better=True,
        description="Automated agreement between the compiled prompt and the delivered shot.",
    ),
    OutcomeName.QC_TEMPORAL_CONSISTENCY: MetricScale(
        scale_id="prod.qc-temporal-consistency-0-1",
        kind=ScaleKind.RATIO_0_1,
        higher_is_better=True,
        description="Automated frame-to-frame stability check.",
    ),
}

#: Which router scoring dimension each outcome speaks to. Most outcomes speak
#: to none, and are deliberately absent rather than mapped to a plausible
#: neighbour: a download says something real about satisfaction and nothing
#: about camera control.
#:
#: ``qc_prompt_alignment`` is the conspicuous omission. It is a genuine quality
#: signal and the router simply has no prompt-adherence dimension to receive
#: it — ``ModelCapabilityProfile.capability_prior`` publishes fifteen
#: dimensions and that is not one of them. Inventing a mapping to
#: ``visual_quality`` would move a score for a reason nobody could later
#: reconstruct, so the outcome is recorded, given a posterior, and left out of
#: routing until a dimension exists for it.
#: ``test_router_lcb_gate.py`` asserts every value here is a real dimension.
OUTCOME_TO_ROUTER_DIMENSION: dict[OutcomeName, str] = {
    OutcomeName.QC_IDENTITY: "character_consistency",
    OutcomeName.QC_MOTION: "complex_motion",
    OutcomeName.QC_TEMPORAL_CONSISTENCY: "scene_consistency",
    OutcomeName.ACCEPTED_OUTPUT: "visual_quality",
}


class ProductionObservation(BaseModel):
    """One generation attempt on this platform, with its conditions and outcomes."""

    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(min_length=1, max_length=64)
    occurred_at: datetime

    # --- what ran -------------------------------------------------------
    provider: str = Field(min_length=1, max_length=80)
    model_id: str = Field(min_length=1, max_length=160)
    #: The exact configuration that ran, as this platform can vouch for it:
    #: ``ModelCapabilityProfile.version`` — for example ``wan-2.7-manual-v4``.
    #:
    #: It is worth being precise about what that does and does not pin down.
    #: For a provider whose model id carries a dated snapshot
    #: (``doubao-seedance-2-5-260628``) the pair really is an exact version of
    #: the weights. For an alias like ``google/veo-3.1`` it is not: the
    #: provider may repoint it, and all this field then pins is *our* side of
    #: the configuration. That case is what ``model_is_alias`` is for — set it
    #: and the observation is quarantined rather than attributed, because
    #: pooling outcomes from before and after a silent repoint is exactly the
    #: cross-version contamination this table exists to prevent.
    #:
    #: Required either way. An observation that cannot name a version does not
    #: get one invented for it.
    exact_version: str = Field(min_length=1, max_length=120)
    model_is_alias: bool = False

    # --- what was asked -------------------------------------------------
    task_type: TaskType
    scenario: Scenario
    asset_criticality: str = Field(min_length=1, max_length=40)
    prompt_complexity: PromptComplexity = PromptComplexity.MODERATE
    reference_mode: ReferenceMode = ReferenceMode.NONE
    duration_seconds: float | None = Field(default=None, ge=0)
    resolution: str = Field(default="n/a", min_length=1, max_length=32)
    aspect_ratio: str = Field(default="n/a", min_length=1, max_length=32)

    # --- outcomes: delivery --------------------------------------------
    generation_success: bool
    provider_failure: str | None = Field(default=None, max_length=120)
    latency_ms: int | None = Field(default=None, ge=0)
    cost_credits: float | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)

    # --- outcomes: what the human did ----------------------------------
    user_rating: int | None = Field(default=None, ge=1, le=5)
    user_preference_ab: Literal["win", "loss", "tie"] | None = None
    user_preference_opponent: str | None = Field(default=None, max_length=200)
    regenerated: bool | None = None
    switched_model: bool | None = None
    downloaded: bool | None = None
    accepted_output: bool | None = None
    used_in_next_shot: bool | None = None

    # --- outcomes: automated quality -----------------------------------
    qc_identity_score: float | None = Field(default=None, ge=0, le=1)
    qc_motion_score: float | None = Field(default=None, ge=0, le=1)
    qc_prompt_alignment: float | None = Field(default=None, ge=0, le=1)
    qc_temporal_consistency: float | None = Field(default=None, ge=0, le=1)

    # --- provenance -----------------------------------------------------
    router_version: str = Field(default="", max_length=80)
    router_decision_id: str | None = Field(default=None, max_length=64)
    project_id: str | None = Field(default=None, max_length=64)
    workspace_id: str | None = Field(default=None, max_length=64)
    generation_job_id: str | None = Field(default=None, max_length=64)
    shot_id: str | None = Field(default=None, max_length=64)
    metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_aggregate_sentinels(self) -> ProductionObservation:
        """``ANY`` addresses a group of cells; it never describes an attempt.

        Allowing it here would let an observation be filed at a level of the
        hierarchy that is supposed to be *derived*, which would double-count it
        against its own aggregate.
        """

        if self.task_type is TaskType.ANY or self.scenario is Scenario.ANY:
            raise ValueError(
                f"observation {self.observation_id} uses the ANY aggregate slot; "
                "a real attempt has a real task type and a real scenario"
            )
        return self

    @model_validator(mode="after")
    def _failed_generations_have_no_quality(self) -> ProductionObservation:
        """A failure cannot also carry a quality score.

        Nothing was produced, so there is nothing to score. Allowing it lets a
        provider outage look like a quality problem, which is the one
        misreading that would make the router avoid a good model permanently.
        """

        if self.generation_success:
            return self
        scored = [
            name
            for name in (
                "qc_identity_score",
                "qc_motion_score",
                "qc_prompt_alignment",
                "qc_temporal_consistency",
                "user_rating",
                "accepted_output",
            )
            if getattr(self, name) is not None
        ]
        if scored:
            raise ValueError(
                f"observation {self.observation_id} failed generation but carries {scored}; "
                "a failed attempt has no artefact to judge"
            )
        return self

    @property
    def conditions(self) -> ConditionBucket:
        return ConditionBucket(
            duration_bucket=ConditionBucket.bucket_duration(self.duration_seconds),
            resolution=self.resolution,
            reference_mode=self.reference_mode,
        )

    def key_for(self, outcome: OutcomeName) -> EvidenceKey:
        return EvidenceKey(
            provider=self.provider,
            model_id=self.model_id,
            exact_version=self.exact_version,
            task_type=self.task_type,
            scenario=self.scenario,
            metric_scale_id=OUTCOME_SCALES[outcome].scale_id,
        )

    def outcome_value(self, outcome: OutcomeName) -> float | None:
        """The observed value on the outcome's own scale, or ``None``.

        ``None`` means "not observed", never "observed as zero". The
        distinction is the whole reason the posterior can be trusted: a shot
        nobody rated must not become a one-star.
        """

        match outcome:
            case OutcomeName.GENERATION_SUCCESS:
                return 1.0 if self.generation_success else 0.0
            case OutcomeName.PROVIDER_FAILURE:
                return 1.0 if self.provider_failure else 0.0
            case OutcomeName.USER_RATING:
                return None if self.user_rating is None else float(self.user_rating)
            case OutcomeName.USER_PREFERENCE_AB:
                if self.user_preference_ab is None:
                    return None
                return {"win": 1.0, "tie": 0.5, "loss": 0.0}[self.user_preference_ab]
            case OutcomeName.ACCEPTED_OUTPUT:
                return None if self.accepted_output is None else float(self.accepted_output)
            case OutcomeName.REGENERATED:
                return None if self.regenerated is None else float(self.regenerated)
            case OutcomeName.SWITCHED_MODEL:
                return None if self.switched_model is None else float(self.switched_model)
            case OutcomeName.DOWNLOADED:
                return None if self.downloaded is None else float(self.downloaded)
            case OutcomeName.USED_IN_NEXT_SHOT:
                return None if self.used_in_next_shot is None else float(self.used_in_next_shot)
            case OutcomeName.QC_IDENTITY:
                return self.qc_identity_score
            case OutcomeName.QC_MOTION:
                return self.qc_motion_score
            case OutcomeName.QC_PROMPT_ALIGNMENT:
                return self.qc_prompt_alignment
            case OutcomeName.QC_TEMPORAL_CONSISTENCY:
                return self.qc_temporal_consistency
        raise AssertionError(f"unhandled outcome {outcome}")  # pragma: no cover

    def observed_outcomes(self) -> dict[OutcomeName, float]:
        return {
            outcome: value
            for outcome in OutcomeName
            if (value := self.outcome_value(outcome)) is not None
        }


__all__ = [
    "OUTCOME_SCALES",
    "OUTCOME_TO_ROUTER_DIMENSION",
    "OutcomeName",
    "ProductionObservation",
    "PromptComplexity",
]
