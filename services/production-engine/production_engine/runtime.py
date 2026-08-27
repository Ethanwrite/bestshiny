from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from asset_registry_core import AssetRegistry, CanonicalVersionNotSet
from entitlement_core import GenerationAdmissionService
from evaluation_core import (
    EvaluationDecision,
    EvaluationEvidence,
    EvaluationExpectation,
    EvaluationResult,
    GenerationEvaluator,
    RetryEngine,
    RetryPlan,
)
from generation_gateway import GenerationGateway, TimelineGenerationPlanStale
from memory_core import (
    ContextAssembler,
    EpisodeScope,
    GenerationContext,
    MemoryQuery,
    MultimodalMemoryEngine,
)
from model_metrics_core import ModelBenchmarkSuite, ModelMetricsService
from model_registry_core import (
    RouterDecision,
    RoutingEvidence,
    ShotRequirements,
    VideoModelRouter,
)
from platform_contracts import (
    AuthoritativeTimelineFence,
    CanonicalShotSpec,
    GenerationRequest,
    PassengerGenerationCommand,
    authoritative_timeline_state_hash,
)
from platform_database import Database
from production_domain.models import (
    AssetVersion,
    DecisionRecord,
    GenerationCandidate,
    GenerationIdempotency,
    GenerationJob,
    JobStatus,
    ProductionTrace,
    Project,
    Shot,
    TimelineState,
    new_id,
    utcnow,
)
from router_evidence_core import (
    CandidateModel,
    ConditionBucket,
    ConservativeLcbBuilder,
    LcbSettings,
    PosteriorLookup,
    ProductionObservation,
    ReferenceMode,
    Scenario,
    TaskType,
    merge_with_baseline,
    router_reference_mode,
    router_scenario,
    router_task_type,
)
from router_evidence_core.service import RouterObservationService
from runtime_control_core import FeatureFlagService
from skill_core import PromptCompilerService
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from style_core import ProjectStyleService
from video_adapter_core import AdapterInput, ModelGenerationRequest, VideoAdapterRegistry


@dataclass(frozen=True)
class PreparedAutopilotGeneration:
    request: GenerationRequest
    shot_spec: CanonicalShotSpec
    context: GenerationContext
    router: RouterDecision
    model_request: ModelGenerationRequest
    prompt_record_id: str
    timeline_fence: AuthoritativeTimelineFence


def _new_trace_id() -> str:
    return secrets.token_hex(16)


#: How long a posterior snapshot may be reused before the runtime looks for a
#: newer offline run. A minute: long enough that a burst of generations pays for
#: one query rather than one each, short enough that an operator who has just
#: saved a run sees it take effect without a restart.
_LCB_SNAPSHOT_TTL_SECONDS = 60.0


class VisualProductionRuntime:
    """Shared production boundary used by Passenger Seat and Autopilot."""

    version = "visual-production-runtime-v1"

    def __init__(
        self,
        database: Database,
        gateway: GenerationGateway,
        assets: AssetRegistry,
        memory: MultimodalMemoryEngine,
        context: ContextAssembler,
        router: VideoModelRouter,
        adapters: VideoAdapterRegistry,
        compiler: PromptCompilerService,
        evaluator: GenerationEvaluator,
        retry: RetryEngine,
        metrics: ModelMetricsService,
        benchmarks: ModelBenchmarkSuite,
        flags: FeatureFlagService,
        generation_admission: GenerationAdmissionService | None = None,
        styles: ProjectStyleService | None = None,
        router_observations: RouterObservationService | None = None,
    ):
        self.database = database
        self.gateway = gateway
        self.assets = assets
        self.memory = memory
        self.context = context
        self.router = router
        self.adapters = adapters
        self.compiler = compiler
        self.evaluator = evaluator
        self.retry = retry
        self.metrics = metrics
        self.benchmarks = benchmarks
        self.flags = flags
        self.generation_admission = generation_admission
        self.styles = styles
        self.router_observations = router_observations
        # The posterior snapshot is an offline artefact, so the routing path
        # must not query for it per request — that would put the offline side
        # back in the hot path, which is the one thing the split is for. The
        # snapshot is materialised once per run id and the check for a newer
        # run is rate-limited to `_LCB_SNAPSHOT_TTL_SECONDS`, so a freshly saved
        # run is picked up within a minute without every generation paying for
        # a query.
        self._lcb_snapshot: tuple[str, PosteriorLookup] | None = None
        self._lcb_snapshot_checked_at: float = 0.0

    def submit_passenger(
        self,
        command: PassengerGenerationCommand,
        *,
        estimated_credits: int | None = None,
        pricing_version: str = "",
    ):  # type: ignore[no-untyped-def]
        request = GenerationRequest(
            project_id=command.project_id,
            type=command.media_type,
            provider=command.provider,
            model=command.model,
            prompt=command.prompt,
            negative_prompt=command.negative_prompt,
            duration=command.duration,
            aspect_ratio=command.aspect_ratio,
            asset_criticality=command.asset_criticality,
            start_frame_asset_id=command.start_frame_asset_id,
            end_frame_asset_id=command.end_frame_asset_id,
            reference_asset_ids=command.reference_asset_ids,
            idempotency_key=command.idempotency_key,
            cost_estimate=command.estimated_cost,
            metadata={
                "mode": "PASSENGER_SEAT",
                "resolution": command.resolution,
                # Why this model ran, so the choice stays auditable after the fact.
                # Admission clears model_role when it obeyed a named model, so its
                # absence is exactly what distinguishes a manual pick from a route.
                "model_selection": "MANUAL" if command.model_role is None else "ROUTER",
                **({"image_task": command.image_task} if command.media_type == "image" else {}),
            },
        )
        return self.submit(
            request,
            mode="PASSENGER_SEAT",
            prompt_version="user-authored-v1",
            # Public command models are never a billing authority. Only the
            # server Admission result supplied by the caller may price a job.
            estimated_credits=estimated_credits,
            pricing_version=pricing_version,
            resolution=command.resolution,
        )

    def submit(
        self,
        request: GenerationRequest,
        *,
        mode: str,
        prompt_version: str = "",
        context_asset_ids: list[str] | None = None,
        retrieved_memory_ids: list[str] | None = None,
        router_scores: list[dict[str, Any]] | None = None,
        on_create: Callable[[Any, GenerationJob, bool], None] | None = None,
        estimated_credits: int | None = None,
        pricing_version: str = "",
        resolution: str = "720p",
        timeline_fence: AuthoritativeTimelineFence | None = None,
    ):  # type: ignore[no-untyped-def]
        trace_id = _new_trace_id()

        def add_trace(session, job: GenerationJob, _replayed: bool) -> None:  # type: ignore[no-untyped-def]
            existing = session.scalar(
                select(ProductionTrace).where(ProductionTrace.generation_job_id == job.id)
            )
            if not existing:
                session.add(
                    ProductionTrace(
                        trace_id=trace_id,
                        mode=mode,
                        project_id=request.project_id,
                        shot_id=request.shot_id,
                        generation_job_id=job.id,
                        provider=request.provider,
                        model_id=request.model,
                        prompt_version=prompt_version,
                        context_asset_ids=context_asset_ids or request.reference_asset_ids,
                        retrieved_memory_ids=retrieved_memory_ids or [],
                        router_scores_json=router_scores or [],
                        estimated_cost=request.cost_estimate,
                    )
                )
            if on_create:
                on_create(session, job, _replayed)

        job, replayed = self.gateway.create(
            request,
            on_create=add_trace,
            estimated_credits=estimated_credits,
            pricing_version=pricing_version,
            resolution=resolution,
            timeline_fence=timeline_fence,
        )
        return job, replayed

    @staticmethod
    def _timeline_fence(
        session: Any,
        shot: Shot,
        project_id: str,
    ) -> AuthoritativeTimelineFence:
        if not shot.input_state_id or not shot.output_state_id:
            raise TimelineGenerationPlanStale(
                "shot has no complete authoritative timeline; plan the shot again"
            )
        input_state = session.get(TimelineState, shot.input_state_id)
        output_state = session.get(TimelineState, shot.output_state_id)
        if input_state is None or output_state is None:
            raise TimelineGenerationPlanStale("shot authoritative timeline disappeared; plan the shot again")
        if (
            input_state.project_id != project_id
            or input_state.state_kind != "SHOT_INPUT"
            or input_state.shot_id not in {None, shot.id}
            or output_state.project_id != project_id
            or output_state.state_kind != "SHOT_OUTPUT"
            or output_state.shot_id not in {None, shot.id}
        ):
            raise TimelineGenerationPlanStale(
                "shot authoritative timeline ownership changed; plan the shot again"
            )
        return AuthoritativeTimelineFence(
            shot_id=shot.id,
            shot_status=shot.status,
            input_state_id=input_state.id,
            input_state_hash=authoritative_timeline_state_hash(
                input_state.state_json,
                previous_state_id=input_state.previous_state_id,
            ),
            output_state_id=output_state.id,
            output_state_hash=authoritative_timeline_state_hash(
                output_state.state_json,
                previous_state_id=output_state.previous_state_id,
            ),
        )

    def _current_timeline_fence(self, shot_id: str) -> AuthoritativeTimelineFence:
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if shot is None:
                raise TimelineGenerationPlanStale("shot disappeared; plan the shot again")
            return self._timeline_fence(session, shot, shot.scene.episode.project_id)

    def prepare_autopilot(
        self,
        shot_id: str,
        *,
        idempotency_key: str,
        candidate_id: str | None = None,
        character_bindings: list[dict[str, Any]] | None = None,
        reference_asset_ids: list[str] | None = None,
        estimated_cost: float = 0.0,
        allowed_providers: list[str] | None = None,
    ) -> PreparedAutopilotGeneration:
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise LookupError("shot not found")
            project_id = shot.scene.episode.project_id
            episode_id = shot.scene.episode_id
            scene_id = shot.scene_id
            state = session.get(TimelineState, shot.input_state_id) if shot.input_state_id else None
            temporal_state = dict(state.state_json) if state else {}
            start_frame_asset_id = shot.start_frame_asset_id
            end_frame_asset_id = shot.end_frame_asset_id
            preferred_provider = shot.preferred_provider or shot.provider
            timeline_fence = self._timeline_fence(session, shot, project_id)

        canonical_assets, canonical_media_ids = self._canonical_assets(project_id)
        style_control = self.styles.generation_control(project_id) if self.styles else None
        if style_control:
            for item in canonical_assets:
                if item.get("id") == style_control.asset_id:
                    item["style_lock"] = style_control.prompt_view()
        entity_ids = [str(item["id"]) for item in canonical_assets]
        memories = (
            self.memory.search(
                MemoryQuery(
                    project_id=project_id,
                    text=shot.user_prompt or shot.prompt,
                    entity_ids=entity_ids,
                    # Series scope: a long-running show establishes facts in
                    # earlier episodes that the current shot still depends on.
                    # The current episode is ranked up rather than fenced off.
                    episode_id=episode_id,
                    episode_scope=EpisodeScope.SERIES,
                    scene_id=scene_id,
                    top_k=8,
                )
            )
            if self.flags.enabled("voyage_memory", project_id=project_id)
            else []
        )
        generation_context = self.context.assemble(
            canonical_assets=canonical_assets,
            temporal_state=temporal_state,
            shot_requirement={"action": shot.user_prompt or shot.prompt, "shot_id": shot_id},
            memories=memories,
            world_rules=self._world_rules(canonical_assets),
            previous_final_frame_asset_id=start_frame_asset_id,
        )
        extra_references = list(dict.fromkeys(reference_asset_ids or []))
        style_references = list(style_control.reference_media_ids) if style_control else []
        generation_context.reference_images = list(
            dict.fromkeys(
                [
                    *style_references,
                    *extra_references,
                    *canonical_media_ids,
                    *generation_context.reference_images,
                ]
            )
        )[: self.context.budget.max_images]
        compiled = self.compiler.compile(
            shot_id,
            character_bindings=character_bindings,
            canonical_assets=canonical_assets,
        )
        requirements = self._requirements(
            compiled.spec,
            generation_context,
            end_frame_asset_id,
            preferred_provider=preferred_provider,
        )
        evidence = self.router.baseline_evidence
        if self.flags.enabled("adaptive_router", project_id=project_id):
            production, counts = self.metrics.production_adjustments()
            evidence = RoutingEvidence(
                benchmark_adjustments=dict(self.benchmarks.adjustments()),
                production_adjustments=production,
                production_sample_counts=counts,
            )
        evidence = self._apply_conservative_lcb(evidence, requirements, project_id=project_id)
        excluded = {
            profile.key
            for profile in self.router.registry.all()
            if not self.gateway.providers.is_configured(profile.provider)
            or (allowed_providers and profile.provider not in allowed_providers)
        }
        decision = self.router.rank(requirements, excluded_models=excluded, evidence=evidence)
        selected = decision.candidates[0]
        adapter_context = generation_context.model_dump(mode="json")
        adapter_context.update(
            {
                "start_frame": start_frame_asset_id,
                "end_frame": end_frame_asset_id,
                "reference_images": generation_context.reference_images,
                "style_control": style_control.provider_view() if style_control else None,
            }
        )
        model_request = self.adapters.get(selected.adapter).compile(
            selected.model,
            AdapterInput(shot=compiled.spec, context=adapter_context),
        )
        request = GenerationRequest(
            project_id=project_id,
            shot_id=shot_id,
            candidate_id=candidate_id,
            type="video",
            provider=selected.provider,
            model=selected.model,
            prompt=model_request.prompt,
            negative_prompt=model_request.negative_prompt,
            duration=compiled.spec.duration,
            aspect_ratio=compiled.spec.aspect_ratio,
            start_frame_asset_id=start_frame_asset_id,
            end_frame_asset_id=end_frame_asset_id,
            reference_asset_ids=generation_context.reference_images,
            idempotency_key=idempotency_key,
            generation_policy=compiled.spec.generation_policy,
            cost_estimate=estimated_cost,
            provider_payload=model_request.payload,
            metadata={
                "mode": "AUTOPILOT",
                "canonical_shot_spec": compiled.spec.model_dump(mode="json"),
                "router": decision.model_dump(mode="json"),
                "style_lock": style_control.prompt_view() if style_control else None,
                "style_control": style_control.provider_view() if style_control else None,
                # Recorded at decision time rather than re-derived at evaluation
                # time. The shot spec can be edited between the two, and an
                # observation filed under the scene the shot *became* would be
                # attributed to a cell that never ran.
                "routing_context": {
                    "task_type": router_task_type(requirements).value,
                    "scenario": router_scenario(requirements).value,
                    "reference_mode": router_reference_mode(requirements).value,
                    "duration_seconds": requirements.duration,
                    "resolution": requirements.resolution,
                    "aspect_ratio": requirements.aspect_ratio,
                    "asset_criticality": requirements.asset_criticality.value,
                    "router_version": decision.router_version,
                    "exact_version": selected.version,
                },
            },
        )
        if self._current_timeline_fence(shot_id) != timeline_fence:
            raise TimelineGenerationPlanStale(
                "authoritative timeline changed while preparing generation; plan the shot again"
            )
        return PreparedAutopilotGeneration(
            request=request,
            shot_spec=compiled.spec,
            context=generation_context,
            router=decision,
            model_request=model_request,
            prompt_record_id=compiled.record_id,
            timeline_fence=timeline_fence,
        )

    def submit_autopilot(
        self,
        prepared: PreparedAutopilotGeneration,
        *,
        on_create: Callable[[Any, GenerationJob, bool], None] | None = None,
        estimated_credits: int | None = None,
        pricing_version: str = "",
        resolution: str = "720p",
    ):  # type: ignore[no-untyped-def]
        candidates = [candidate.model_dump(mode="json") for candidate in prepared.router.candidates]

        def add_autopilot_records(session, job: GenerationJob, replayed: bool) -> None:  # type: ignore[no-untyped-def]
            if on_create:
                on_create(session, job, replayed)
            if replayed:
                return
            session.add(
                DecisionRecord(
                    project_id=prepared.request.project_id,
                    shot_id=prepared.request.shot_id,
                    decision_type="VIDEO_MODEL_ROUTING",
                    input_features=prepared.shot_spec.model_dump(mode="json"),
                    selected_action=f"{prepared.request.provider}:{prepared.request.model}",
                    reason_codes=[
                        *prepared.router.candidates[0].reasons,
                        *prepared.router.candidates[0].penalties,
                    ],
                    model_version=prepared.router.router_version,
                    policy_version=self.version,
                )
            )

        job, replayed = self.submit(
            prepared.request,
            mode="AUTOPILOT",
            prompt_version=PromptCompilerService.version,
            context_asset_ids=prepared.context.canonical_asset_ids,
            retrieved_memory_ids=[item.id for item in prepared.context.episodic_memories],
            router_scores=candidates,
            on_create=add_autopilot_records,
            estimated_credits=estimated_credits,
            pricing_version=pricing_version,
            resolution=resolution,
            timeline_fence=prepared.timeline_fence,
        )
        return job, replayed

    def evaluate_job(
        self,
        job_id: str,
        evidence: EvaluationEvidence | None = None,
    ) -> tuple[EvaluationResult, RetryPlan | None, GenerationJob | None]:
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if not job:
                raise LookupError("generation job not found")
            request = dict(job.request_json)
            metadata = dict(request.get("metadata") or {})
            spec_data = metadata.get("canonical_shot_spec") or {}
            expected_state = spec_data.get("end_state") or {}
            props = spec_data.get("props") or []
            required_props = [str(item.get("asset_id") or item.get("name")) for item in props]
            attempt_number = int(metadata.get("retry_attempt", 0))
            project_id = job.project_id
            provider = job.provider
            model_id = job.model
            shot_id = job.shot_id
            output_asset_id = job.output_asset_id
            job_status = job.status
        if job_status != JobStatus.COMPLETED.value or not output_asset_id:
            raise ValueError("generation must be completed with an output asset before evaluation")
        expectation = EvaluationExpectation(
            project_id=project_id,
            generation_id=job_id,
            shot_id=shot_id,
            canonical_reference_asset_ids=list(request.get("reference_asset_ids") or []),
            previous_frame_asset_id=request.get("start_frame_asset_id"),
            generated_asset_id=output_asset_id,
            shot_requirement=spec_data,
            expected_state=expected_state,
            required_props=[item for item in required_props if item and item != "None"],
            forbid_camera_gaze=not bool(spec_data.get("allow_camera_gaze", False)),
        )
        result = self.evaluator.evaluate(
            expectation,
            evidence,
            attempt_number=attempt_number,
            model_id=model_id,
            provider=provider,
        )
        failure_metrics = {
            "wrong_character": "identity_failure",
            "identity": "identity_failure",
            "hair": "identity_failure",
            "wrong_costume": "identity_failure",
            "wardrobe": "identity_failure",
            "wrong_scene": "scene_failure",
            "scene": "scene_failure",
            "missing_key_prop": "prop_failure",
            "props": "prop_failure",
            "dialogue": "dialogue_failure",
            "camera": "camera_failure",
            "wrong_screen_direction": "camera_failure",
            "direct_camera_gaze": "gaze_failure",
            "eyeline": "gaze_failure",
            "motion": "physics_failure",
        }
        for metric in {
            failure_metrics[reason] for reason in result.retry_reasons if reason in failure_metrics
        }:
            self.metrics.record_once(
                provider=provider,
                model_id=model_id,
                metric=metric,
                generation_job_id=job_id,
                project_id=project_id,
                shot_id=shot_id,
            )
        plan: RetryPlan | None = None
        retry_job: GenerationJob | None = None
        if (
            self.flags.enabled("auto_retry", project_id=project_id)
            and result.decision != EvaluationDecision.ACCEPT
        ):
            alternatives = [
                (str(item.get("provider")), str(item.get("model")))
                for item in (metadata.get("router") or {}).get("candidates", [])
                if item.get("provider") and item.get("model")
            ]
            plan = self.retry.plan(
                result,
                attempt_number=attempt_number,
                current_provider=provider,
                current_model=model_id,
                alternatives=alternatives,
                references_already_strengthened=bool(metadata.get("references_strengthened")),
            )
            if not plan.terminal:
                retry_job = self._execute_retry(job_id, request, metadata, spec_data, plan)
                self.metrics.record_once(
                    provider=provider,
                    model_id=model_id,
                    metric="auto_retry",
                    generation_job_id=job_id,
                    project_id=project_id,
                    shot_id=shot_id,
                )
        with self.database.session() as session:
            trace = session.scalar(select(ProductionTrace).where(ProductionTrace.generation_job_id == job_id))
            if trace:
                trace.evaluation_json = result.model_dump(mode="json")
                trace.retry_json = plan.model_dump(mode="json") if plan else {}
        self._record_router_observation(job_id, metadata, result)
        return result, plan, retry_job

    def _retargeted_routing_context(
        self, context: object, provider: str, model_id: str
    ) -> dict[str, Any] | None:
        """The routing context, re-pointed at the model a retry actually uses.

        Returns ``None`` when there is no context to carry or the registry
        cannot name the new target's version, which makes the recorder skip the
        attempt rather than attribute it to the wrong snapshot.
        """

        if not isinstance(context, dict):
            return None
        profile = self.router.registry.get(model_id, provider)
        if profile is None or not str(profile.version).strip():
            return None
        return {**context, "exact_version": profile.version}

    def _record_router_observation(
        self,
        job_id: str,
        metadata: dict[str, Any],
        result: EvaluationResult,
    ) -> None:
        """Write the wide production observation for one evaluated generation.

        Deliberately best-effort and deliberately silent about failure. This is
        evidence collection for an offline analysis, not part of delivering the
        user's shot, and an exception here must never turn a successful
        generation into a failed request. What it must not do instead is write
        something wrong, so every path that cannot name the version, the task or
        the scene declines to write at all.
        """

        if self.router_observations is None:
            return
        context = metadata.get("routing_context")
        if not isinstance(context, dict) or not context.get("exact_version"):
            # A job planned before this field existed, or by a path that does
            # not route. Skipped rather than guessed: re-deriving the scene from
            # the current shot spec would attribute the outcome to a cell that
            # may never have run.
            return
        with self.database.session() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:  # pragma: no cover - evaluate_job already loaded it
                return
            provider = job.provider
            model_id = job.model
            project_id = job.project_id
            shot_id = job.shot_id
            job_status = job.status
            actual_cost = job.actual_cost
            quoted_credits = job.quoted_credits
            error_code = job.error_code
            submitted_at = job.submitted_at
            completed_at = job.completed_at
            created_at = job.created_at
            # The job carries no workspace; the project does.
            project = session.get(Project, project_id) if project_id else None
            workspace_id = project.workspace_id if project else None

        latency_ms: int | None = None
        started = submitted_at or created_at
        if started and completed_at:
            latency_ms = max(0, int((completed_at - started).total_seconds() * 1000))
        succeeded = job_status == JobStatus.COMPLETED.value
        scores = result.scores if succeeded else {}
        try:
            observation = ProductionObservation(
                observation_id=new_id(),
                occurred_at=completed_at or utcnow(),
                provider=provider,
                model_id=model_id,
                exact_version=str(context["exact_version"]),
                task_type=TaskType(str(context.get("task_type", "T2V"))),
                scenario=Scenario(str(context.get("scenario", "generic"))),
                asset_criticality=str(context.get("asset_criticality", "STANDARD")),
                reference_mode=ReferenceMode(str(context.get("reference_mode", "NONE"))),
                duration_seconds=context.get("duration_seconds"),
                resolution=str(context.get("resolution") or "n/a"),
                aspect_ratio=str(context.get("aspect_ratio") or "n/a"),
                generation_success=succeeded,
                provider_failure=None if succeeded else (error_code or "UNKNOWN_FAILURE"),
                latency_ms=latency_ms,
                # `is not None`, not truthiness: a generation quoted at zero
                # credits was observed to cost nothing, which is not the same
                # as its cost not having been observed.
                cost_credits=float(quoted_credits) if quoted_credits is not None else None,
                cost_usd=float(actual_cost) if actual_cost is not None else None,
                accepted_output=(
                    result.decision == EvaluationDecision.ACCEPT if succeeded else None
                ),
                # `qc_prompt_alignment` has no source here on purpose: the
                # evaluator publishes fourteen named checks and none of them is
                # prompt adherence. Mapping `scene` or `blocking` onto it would
                # be inventing a measurement.
                qc_identity_score=scores.get("identity"),
                qc_motion_score=scores.get("motion"),
                qc_temporal_consistency=scores.get("continuity"),
                router_version=str(context.get("router_version") or ""),
                project_id=project_id,
                workspace_id=workspace_id,
                generation_job_id=job_id,
                shot_id=shot_id,
                metadata={"evaluator_version": result.evaluator_version},
            )
            self.router_observations.record(observation)
        except (ValueError, LookupError):
            # The contract's own refusals — an alias binding, a failed
            # generation carrying a score. `UnattributableObservation` is a
            # `ValueError`, so it lands here too. Not writing is correct.
            return
        except SQLAlchemyError:
            # Anything the database refuses: a serialization failure under
            # concurrent evaluation, a constraint this code did not anticipate.
            # The docstring above promises that collecting evidence never fails
            # the user's request, and a `ValueError` catch does not keep that
            # promise — `OperationalError` is not one.
            return

    def _execute_retry(
        self,
        original_job_id: str,
        request: dict[str, Any],
        metadata: dict[str, Any],
        spec_data: dict[str, Any],
        plan: RetryPlan,
    ) -> GenerationJob:
        retry_key = f"auto-retry-{original_job_id}-{plan.attempt_number}"
        with self.database.session() as session:
            existing_retry = session.scalar(
                select(GenerationIdempotency).where(
                    GenerationIdempotency.project_id == str(request["project_id"]),
                    GenerationIdempotency.key == retry_key,
                )
            )
            if existing_retry:
                existing_job = session.get(GenerationJob, existing_retry.generation_job_id)
                if existing_job:
                    return existing_job
        candidate_id = None
        shot_id = request.get("shot_id")
        if shot_id:
            with self.database.session() as session:
                attempt = (
                    int(
                        session.scalar(
                            select(func.coalesce(func.max(GenerationCandidate.attempt_number), 0)).where(
                                GenerationCandidate.shot_id == shot_id
                            )
                        )
                        or 0
                    )
                    + 1
                )
                candidate = GenerationCandidate(
                    shot_id=shot_id,
                    attempt_number=attempt,
                    status="CREATED",
                    metadata_json={
                        "retry_of": original_job_id,
                        "retry_plan": plan.model_dump(mode="json"),
                    },
                )
                session.add(candidate)
                session.flush()
                candidate_id = candidate.id

        next_provider = plan.next_provider or str(request["provider"])
        next_model = plan.next_model or str(request["model"])
        prompt = str(request["prompt"])
        negative_prompt = str(request.get("negative_prompt") or "")
        # References must be final before the Adapter payload is compiled; the
        # payload embeds them and a payload built from the previous attempt's
        # references would contradict the request the Gateway resolves.
        reference_asset_ids = list(request.get("reference_asset_ids") or [])
        if plan.inject_stronger_references:
            _canonical_assets, canonical_reference_ids = self._canonical_assets(str(request["project_id"]))
            reference_asset_ids = list(dict.fromkeys([*reference_asset_ids, *canonical_reference_ids]))[:20]
        target_changed = next_model != request["model"] or next_provider != request["provider"]
        references_changed = reference_asset_ids != list(request.get("reference_asset_ids") or [])
        provider_payload = dict(request.get("provider_payload") or {})
        if target_changed or references_changed:
            # The persisted Adapter payload describes the previous attempt: it was
            # shaped for the previous model and embeds the previous reference list.
            # Reusing it here would submit transport parameters that contradict this
            # retry, so it is recompiled for the actual target and dropped when it
            # cannot be recompiled.
            provider_payload = {}
            profile = self.router.registry.get(next_model, next_provider) if spec_data else None
            if profile:
                context = {
                    "reference_images": list(reference_asset_ids),
                    "canonical_asset_ids": list(reference_asset_ids),
                    "start_frame": request.get("start_frame_asset_id"),
                    "end_frame": request.get("end_frame_asset_id"),
                    "style_control": metadata.get("style_control") or metadata.get("style_lock"),
                }
                adapted = self.adapters.get(profile.adapter).compile(
                    next_model,
                    AdapterInput(shot=CanonicalShotSpec.model_validate(spec_data), context=context),
                )
                provider_payload = dict(adapted.payload)
                if target_changed:
                    # Only a different model justifies replacing the approved prompt;
                    # a reference refresh keeps the prompt this shot was admitted with.
                    prompt = adapted.prompt
                    negative_prompt = adapted.negative_prompt
        if plan.prompt_patch:
            prompt = f"{prompt}\nREPAIR CONSTRAINT: {plan.prompt_patch}"
        if "prompt" in provider_payload:
            # The payload carries its own copy of the prompt. Only the final
            # canonical prompt of this retry may reach the provider.
            provider_payload["prompt"] = prompt
        retry_metadata = {
            **metadata,
            "retry_of": original_job_id,
            "retry_attempt": plan.attempt_number,
            "references_strengthened": plan.inject_stronger_references
            or bool(metadata.get("references_strengthened")),
            "retry_plan": plan.model_dump(mode="json"),
        }
        if target_changed:
            # `routing_context.exact_version` describes the model the *previous*
            # attempt ran. Carrying it onto a retry that re-routes would file
            # this attempt's outcome under `newProvider:newModel@oldVersion` — a
            # pair that never ran, and the cross-version contamination the
            # observation table exists to prevent. Re-resolve it from the
            # registry, and drop the context entirely when the registry cannot
            # name the new target: no observation is better than a mislabelled
            # one.
            retry_metadata["routing_context"] = self._retargeted_routing_context(
                metadata.get("routing_context"), next_provider, next_model
            )
        retry_request = GenerationRequest.model_validate(
            {
                **request,
                "candidate_id": candidate_id,
                "provider": next_provider,
                "model": next_model,
                "prompt": prompt,
                "negative_prompt": negative_prompt,
                "reference_asset_ids": reference_asset_ids,
                "provider_payload": provider_payload,
                "idempotency_key": retry_key,
                "metadata": retry_metadata,
            }
        )
        try:
            estimated_credits: int | None = None
            pricing_version = ""
            if self.generation_admission is not None:
                admitted_retry = self.generation_admission.admit_autopilot(retry_request)
                retry_request = admitted_retry.request
                estimated_credits = admitted_retry.estimate.credits
                pricing_version = self.generation_admission.pricing.version
            job, replayed = self.submit(
                retry_request,
                mode="AUTOPILOT_RETRY",
                prompt_version="retry-patch-v1",
                context_asset_ids=reference_asset_ids,
                router_scores=(metadata.get("router") or {}).get("candidates", []),
                estimated_credits=estimated_credits,
                pricing_version=pricing_version,
            )
            if replayed and candidate_id and job.candidate_id != candidate_id:
                with self.database.session() as session:
                    stale = session.get(GenerationCandidate, candidate_id)
                    if stale and not stale.generation_job_id:
                        session.delete(stale)
            return job
        except Exception:
            if candidate_id:
                with self.database.session() as session:
                    stale = session.get(GenerationCandidate, candidate_id)
                    if stale and not stale.generation_job_id:
                        session.delete(stale)
            raise

    def _canonical_assets(self, project_id: str) -> tuple[list[dict[str, Any]], list[str]]:
        result: list[dict[str, Any]] = []
        image_media_ids: list[str] = []
        with self.database.session() as session:
            project = session.get(Project, project_id)
            locked_style_version_id = project.canonical_style_version_id if project else None
            locked_style_version = (
                session.get(AssetVersion, locked_style_version_id) if locked_style_version_id else None
            )
            locked_style_asset_id = locked_style_version.asset_id if locked_style_version else None
        for asset in self.assets.list(project_id):
            if asset.asset_type == "STYLE" and not locked_style_version_id:
                continue
            if asset.asset_type == "STYLE" and asset.id != locked_style_asset_id:
                continue
            try:
                resolved = self.assets.resolve(
                    asset.id,
                    version_id=(locked_style_version_id if asset.asset_type == "STYLE" else None),
                )
            except CanonicalVersionNotSet:
                continue
            if asset.asset_type == "STYLE" and resolved.version.id != locked_style_version_id:
                continue
            media = [reference.media for reference in resolved.references]
            if resolved.primary_media:
                media.insert(0, resolved.primary_media)
            version_images = [item.id for item in media if item.mime_type.startswith("image/")]
            version_videos = [item.id for item in media if item.mime_type.startswith("video/")]
            image_media_ids.extend(version_images)
            result.append(
                {
                    "id": asset.id,
                    "version_id": resolved.version.id,
                    "type": asset.asset_type,
                    "name": asset.name,
                    "canonical_metadata": asset.canonical_metadata,
                    "version_metadata": resolved.version.metadata_json,
                    "continuity_state": resolved.version.continuity_state,
                    "image_urls": version_images,
                    "video_urls": version_videos,
                    "constraints": asset.canonical_metadata.get("constraints", []),
                }
            )
        return result, list(dict.fromkeys(image_media_ids))

    @staticmethod
    def _world_rules(canonical_assets: list[dict[str, Any]]) -> list[str]:
        return [
            str(rule)
            for asset in canonical_assets
            if asset.get("type") == "STYLE"
            for rule in asset.get("canonical_metadata", {}).get("world_rules", [])
        ]

    def _conservative_lcb_lookup(self) -> PosteriorLookup | None:
        """The saved posterior the LCB reads, or ``None`` if there is none.

        Returns ``None`` rather than an empty lookup when no offline run has
        ever been saved, so the caller can tell "nothing computed yet" from
        "computed, and it had nothing to say about these models".

        Both the run-id check and the materialisation are cached. Without the
        first cache every routed generation issued a `router_posteriors` query
        just to learn that the answer had not changed.
        """

        if self.router_observations is None:
            return None
        now = time.monotonic()
        if (
            self._lcb_snapshot is not None
            and now - self._lcb_snapshot_checked_at < _LCB_SNAPSHOT_TTL_SECONDS
        ):
            return self._lcb_snapshot[1]
        run_id = self.router_observations.latest_posterior_run_id()
        self._lcb_snapshot_checked_at = now
        if run_id is None:
            return None
        if self._lcb_snapshot is None or self._lcb_snapshot[0] != run_id:
            self._lcb_snapshot = (run_id, self.router_observations.lookup_for(run_id))
        return self._lcb_snapshot[1]

    def _apply_conservative_lcb(
        self,
        evidence: RoutingEvidence,
        requirements: ShotRequirements,
        *,
        project_id: str,
    ) -> RoutingEvidence:
        """Overlay lower-bound evidence, or return the caller's evidence unchanged.

        Every early return here is a fallback to the routing behaviour that
        already exists: flag off, no snapshot saved, no sufficient cell for
        these models. That is the design — the conservative thing to do with
        thin evidence is to keep using the priors that were reviewed by hand.
        """

        if not self.flags.enabled("router_lcb", project_id=project_id):
            return evidence
        lookup = self._conservative_lcb_lookup()
        if lookup is None:
            return evidence
        candidates = [
            CandidateModel(
                provider=profile.provider, model_id=profile.model_id, exact_version=profile.version
            )
            for profile in self.router.registry.all()
            if profile.modality == "video"
        ]
        builder = ConservativeLcbBuilder(lookup, LcbSettings(enabled=True))
        adjustments = builder.build(
            candidates,
            task_type=router_task_type(requirements),
            scenario=router_scenario(requirements),
            conditions=ConditionBucket(
                duration_bucket=ConditionBucket.bucket_duration(requirements.duration),
                resolution=requirements.resolution,
                reference_mode=router_reference_mode(requirements),
            ),
        )
        if adjustments.is_noop:
            return evidence
        return RoutingEvidence(
            benchmark_adjustments=dict(evidence.benchmark_adjustments),
            production_adjustments=merge_with_baseline(evidence.production_adjustments, adjustments),
            production_sample_counts={
                **dict(evidence.production_sample_counts),
                **adjustments.sample_counts,
            },
        )

    @staticmethod
    def _requirements(
        spec: CanonicalShotSpec,
        context: GenerationContext,
        end_frame_asset_id: str | None,
        *,
        preferred_provider: str | None = None,
    ) -> ShotRequirements:
        orientations = " ".join(subject.body_orientation.lower() for subject in spec.subjects)
        orientations = f"{orientations} {json.dumps(spec.end_state, ensure_ascii=False).lower()}"
        rear = any(token in orientations for token in ("rear", "back", "背面", "背对"))
        profile = any(token in orientations for token in ("profile", "侧面", "three-quarter"))
        return ShotRequirements(
            duration=spec.duration,
            resolution=spec.resolution,
            aspect_ratio=spec.aspect_ratio,
            reference_image_count=len(context.reference_images),
            characters=len(spec.subjects),
            profile=spec.profile,
            requires_image_to_video=bool(context.reference_images),
            requires_start_frame=bool(context.previous_final_frame_asset_id),
            requires_end_frame=bool(end_frame_asset_id),
            requires_reference_images=bool(context.reference_images),
            requires_multi_reference=len(context.reference_images) > 1,
            requires_native_audio=bool(spec.audio),
            requires_dialogue=bool(spec.dialogue),
            requires_chinese_dialogue=bool(spec.dialogue and spec.language.startswith("zh")),
            requires_character_consistency=bool(spec.subjects),
            requires_scene_consistency=True,
            requires_complex_action=spec.profile == "action",
            requires_physical_plausibility=spec.profile == "action",
            requires_camera_control=spec.camera.dominant_movement != "locked-off",
            requires_multi_character=len(spec.subjects) > 1,
            requires_end_frame_profile=profile,
            requires_rear_view_ending=rear,
            forbid_camera_gaze=not spec.allow_camera_gaze,
            visual_quality_priority=0.9 if spec.profile == "commercial_hero" else 0.6,
            product_fidelity_priority=1.0 if spec.profile == "commercial_hero" else 0.0,
            preferred_provider=preferred_provider,
        )
