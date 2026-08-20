from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import TYPE_CHECKING

from cost_core import CostEngine
from evaluation_core import EvaluationDecision, EvaluationEvidence
from generation_gateway import GenerationGateway
from generation_policy_core import CapabilityResolver
from platform_contracts import GenerationRequest
from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    CandidateStatus,
    CostRecord,
    DecisionRecord,
    GenerationCandidate,
    GenerationEvent,
    GenerationJob,
    GenerationPolicy,
    MediaAsset,
    ModelMetric,
    QADecision,
    QAResult,
    Shot,
    ShotStateSnapshot,
    ShotStatus,
    TimelineState,
    new_id,
)
from production_engine import ShotContinuityService
from qa_core import QAPipeline
from skill_core import PromptCompilerService
from sqlalchemy import func, select, update

if TYPE_CHECKING:
    from production_engine.runtime import VisualProductionRuntime


class CandidateNotCommittable(RuntimeError):
    pass


class CandidatePipeline:
    def __init__(
        self,
        database: Database,
        gateway: GenerationGateway,
        prompts: PromptCompilerService,
        capability_resolver: CapabilityResolver,
        qa: QAPipeline,
        cost: CostEngine,
        continuity: ShotContinuityService,
        visual_runtime: VisualProductionRuntime | None = None,
    ):
        self.database = database
        self.gateway = gateway
        self.prompts = prompts
        self.capability_resolver = capability_resolver
        self.qa = qa
        self.cost = cost
        self.continuity = continuity
        self.visual_runtime = visual_runtime

    @staticmethod
    def _embedding(value: str) -> list[float]:
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        return [round(byte / 255, 6) for byte in digest[:16]]

    def create_candidate(
        self,
        shot_id: str,
        *,
        idempotency_key: str,
        fallback_providers: list[str] | None = None,
        character_bindings: list[dict] | None = None,
        reference_asset_ids: list[str] | None = None,
        estimated_cost: float = 0.0,
    ) -> tuple[GenerationCandidate, bool]:
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise LookupError("shot not found")
            project_id = shot.scene.episode.project_id
            desired_policy = shot.generation_policy or GenerationPolicy.TEXT_TO_VIDEO.value
            preferred = shot.preferred_provider or shot.provider
            model = shot.preferred_model or shot.model
            shot_type = shot.shot_type
            duration = shot.duration
            aspect_ratio = shot.scene.episode.project.default_aspect_ratio
            start_frame_asset_id = shot.start_frame_asset_id
            end_frame_asset_id = shot.end_frame_asset_id
        if self.visual_runtime is not None:
            candidate_id = new_id()
            allowed_providers = list(
                dict.fromkeys(
                    [preferred, *(fallback_providers or ["google_flow", "seedance", "veo_official"])]
                )
            )
            prepared = self.visual_runtime.prepare_autopilot(
                shot_id,
                idempotency_key=idempotency_key,
                candidate_id=candidate_id,
                character_bindings=character_bindings,
                reference_asset_ids=reference_asset_ids,
                estimated_cost=estimated_cost,
                allowed_providers=allowed_providers,
            )
            generation_plan = {
                "policy": prepared.request.generation_policy,
                "required_inputs": {
                    "start_frame": bool(prepared.request.start_frame_asset_id),
                    "end_frame": bool(prepared.request.end_frame_asset_id),
                    "reference_assets": bool(prepared.request.reference_asset_ids),
                },
                "provider": prepared.request.provider,
                "model": prepared.request.model,
                "fallback_providers": [item.provider for item in prepared.router.candidates[1:]],
                "degraded_from": None,
                "reasons": prepared.router.candidates[0].reasons,
                "penalties": prepared.router.candidates[0].penalties,
                "router_version": prepared.router.router_version,
                "prompt_record_id": prepared.prompt_record_id,
            }

            def initialize_candidate(session, job: GenerationJob, replayed: bool) -> None:  # type: ignore[no-untyped-def]
                if replayed:
                    return
                with session.no_autoflush:
                    session.execute(update(Shot).where(Shot.id == shot_id).values(updated_at=Shot.updated_at))
                    locked_shot = session.get(Shot, shot_id)
                    if not locked_shot:
                        raise LookupError("shot not found during candidate allocation")
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
                    id=candidate_id,
                    shot_id=shot_id,
                    attempt_number=attempt,
                    status=CandidateStatus.CREATED.value,
                    metadata_json={"generation_plan": generation_plan},
                )
                session.add(candidate)
                session.flush([candidate])
                locked_shot.generation_policy = prepared.request.generation_policy
                locked_shot.preferred_provider = prepared.request.provider
                locked_shot.preferred_model = prepared.request.model

            job, replayed = self.visual_runtime.submit_autopilot(
                prepared,
                on_create=initialize_candidate,
            )
            with self.database.session() as session:
                if replayed:
                    existing = (
                        session.get(GenerationCandidate, job.candidate_id) if job.candidate_id else None
                    )
                    if not existing:
                        raise RuntimeError("replayed generation job has no candidate")
                    return existing, True
                candidate = session.get(GenerationCandidate, candidate_id)
                if not candidate:
                    raise RuntimeError("generation job was created without its candidate")
                return candidate, False
        plan = self.capability_resolver.resolve(
            desired_policy,
            preferred,
            fallback_providers or ["google_flow", "seedance", "veo_official"],
            project_id=project_id,
            shot_id=shot_id,
            quality_profile=shot_type,
        )
        compilation = self.prompts.compile_shot(
            shot_id,
            provider=plan.provider,
            model=model,
            character_bindings=character_bindings,
        )
        generation_plan = {
            "policy": plan.policy,
            "required_inputs": plan.required_inputs,
            "provider": plan.provider,
            "fallback_providers": plan.fallback_providers,
            "degraded_from": plan.degraded_from,
            "reasons": plan.reasons,
        }
        candidate_id = new_id()
        request = GenerationRequest(
            project_id=project_id,
            shot_id=shot_id,
            candidate_id=candidate_id,
            type="video",
            provider=plan.provider,
            model=model,
            prompt=compilation.compiled_prompt,
            negative_prompt="identity drift, duplicate people, extra limbs, changed costume, changed props, "
            "unintended cuts, text artifacts, direct gaze into camera",
            duration=duration,
            aspect_ratio=aspect_ratio,
            start_frame_asset_id=start_frame_asset_id,
            end_frame_asset_id=end_frame_asset_id,
            reference_asset_ids=reference_asset_ids or [],
            idempotency_key=idempotency_key,
            generation_policy=plan.policy,
            cost_estimate=estimated_cost,
            metadata={"generation_plan": generation_plan},
        )

        def initialize_legacy_candidate(session, job: GenerationJob, replayed: bool) -> None:  # type: ignore[no-untyped-def]
            if replayed:
                return
            with session.no_autoflush:
                session.execute(update(Shot).where(Shot.id == shot_id).values(updated_at=Shot.updated_at))
                locked_shot = session.get(Shot, shot_id)
                if not locked_shot:
                    raise LookupError("shot not found during candidate allocation")
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
                id=candidate_id,
                shot_id=shot_id,
                attempt_number=attempt,
                status=CandidateStatus.CREATED.value,
                metadata_json={"generation_plan": generation_plan},
            )
            session.add(candidate)
            session.flush([candidate])
            locked_shot.generation_policy = plan.policy
            locked_shot.preferred_provider = plan.provider

        job, replayed = self.gateway.create(request, on_create=initialize_legacy_candidate)
        with self.database.session() as session:
            if replayed:
                existing = session.get(GenerationCandidate, job.candidate_id) if job.candidate_id else None
                if not existing:
                    raise RuntimeError("replayed generation job has no candidate")
                return existing, True
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate:
                raise RuntimeError("generation job was created without its candidate")
            return candidate, replayed

    def sync_candidate(self, candidate_id: str, evidence: dict | None = None) -> GenerationCandidate:
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate:
                raise LookupError("candidate not found")
            if candidate.status in {
                CandidateStatus.COMMITTED.value,
                CandidateStatus.REJECTED.value,
            }:
                raise LookupError("committed or rejected candidates cannot be revalidated")
            job_id = candidate.generation_job_id
            output_asset_id = candidate.output_asset_id
            shot = session.get(Shot, candidate.shot_id)
            shot_type = shot.shot_type if shot else None
        job = self.gateway.get(job_id) if job_id else None
        if job and self.visual_runtime is not None and job.status == "COMPLETED":
            self.visual_runtime.metrics.record_once(
                provider=job.provider,
                model_id=job.model,
                metric="generation_success",
                generation_job_id=job.id,
                project_id=job.project_id,
                shot_id=job.shot_id,
            )
        if job and job.output_asset_id and not output_asset_id:
            with self.database.session() as session:
                candidate = session.get(GenerationCandidate, candidate_id)
                if not candidate or candidate.status in {
                    CandidateStatus.COMMITTED.value,
                    CandidateStatus.REJECTED.value,
                }:
                    raise LookupError("terminal candidate cannot accept a generation result")
                candidate.output_asset_id = job.output_asset_id
                candidate.status = CandidateStatus.VALIDATING.value
                output_asset_id = job.output_asset_id
        if output_asset_id:
            auto_evaluation_enabled = bool(
                job
                and self.visual_runtime is not None
                and self.visual_runtime.flags.enabled("auto_evaluation", project_id=job.project_id)
            )
            evaluation_evidence: EvaluationEvidence | None = None
            if auto_evaluation_enabled:
                evaluation_payload = (evidence or {}).get("evaluation_evidence")
                evaluation_evidence = (
                    EvaluationEvidence.model_validate(evaluation_payload)
                    if evaluation_payload is not None
                    else None
                )
            legacy_qa = self.qa.validate_candidate(
                candidate_id,
                evidence,
                profile=shot_type or "DIALOGUE",
                defer_pass=auto_evaluation_enabled,
            )
            if auto_evaluation_enabled and legacy_qa.decision == QADecision.PASS.value:
                assert job is not None and self.visual_runtime is not None
                try:
                    visual_result, retry_plan, retry_job = self.visual_runtime.evaluate_job(
                        job.id, evaluation_evidence
                    )
                except Exception:
                    with self.database.session() as session:
                        failed = session.execute(
                            update(GenerationCandidate)
                            .where(
                                GenerationCandidate.id == candidate_id,
                                GenerationCandidate.status == CandidateStatus.VALIDATING.value,
                                GenerationCandidate.qa_result_id == legacy_qa.id,
                            )
                            .values(status=CandidateStatus.HARD_FAILED.value)
                        )
                        qa_result = session.get(QAResult, legacy_qa.id)
                        if affected_rows(failed) == 1 and qa_result:
                            qa_result.decision = QADecision.HARD_FAIL.value
                            qa_result.hard_failures = list(
                                dict.fromkeys([*qa_result.hard_failures, "VISUAL_EVALUATION_ERROR"])
                            )
                            qa_result.summary = "HARD_FAIL: visual evaluation could not complete"
                    raise
                with self.database.session() as session:
                    next_status = (
                        CandidateStatus.PASSED.value
                        if visual_result.decision == EvaluationDecision.ACCEPT
                        else CandidateStatus.HARD_FAILED.value
                    )
                    finalized = session.execute(
                        update(GenerationCandidate)
                        .where(
                            GenerationCandidate.id == candidate_id,
                            GenerationCandidate.status == CandidateStatus.VALIDATING.value,
                            GenerationCandidate.qa_result_id == legacy_qa.id,
                        )
                        .values(status=next_status)
                    )
                    if affected_rows(finalized) != 1:
                        raise LookupError("candidate validation was superseded before visual QA finished")
                    candidate = session.get(GenerationCandidate, candidate_id)
                    qa_result = session.get(QAResult, legacy_qa.id)
                    candidate.metadata_json = {
                        **candidate.metadata_json,
                        "visual_evaluation": visual_result.model_dump(mode="json"),
                        "visual_retry_plan": (retry_plan.model_dump(mode="json") if retry_plan else None),
                        "visual_retry_job_id": retry_job.id if retry_job else None,
                    }
                    if visual_result.decision != EvaluationDecision.ACCEPT:
                        if qa_result:
                            failure = f"VISUAL_{visual_result.decision.value}"
                            qa_result.decision = QADecision.HARD_FAIL.value
                            qa_result.hard_failures = list(dict.fromkeys([*qa_result.hard_failures, failure]))
                            qa_result.metrics_json = {
                                **qa_result.metrics_json,
                                "visual_evaluation": visual_result.model_dump(mode="json"),
                            }
                            qa_result.summary = f"HARD_FAIL: visual evaluation {visual_result.decision.value}"
        with self.database.session() as session:
            return session.get(GenerationCandidate, candidate_id)

    def commit(self, candidate_id: str, *, accepted_by: str | None = None) -> GenerationCandidate:
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate or not candidate.qa_result_id or not candidate.output_asset_id:
                raise CandidateNotCommittable("candidate has no completed QA result")
            shot = session.get(Shot, candidate.shot_id)
            if not shot:
                raise CandidateNotCommittable("candidate shot no longer exists")
            if shot.committed_candidate_id == candidate.id:
                if candidate.status != CandidateStatus.COMMITTED.value:
                    raise CandidateNotCommittable("shot canonical state is inconsistent")
                return candidate
            if shot.committed_candidate_id:
                raise CandidateNotCommittable("this shot already has an adopted candidate")
            if candidate.status != CandidateStatus.PASSED.value:
                raise CandidateNotCommittable("only candidates in PASSED state can be committed")
            qa = session.get(QAResult, candidate.qa_result_id)
            if not qa or qa.decision != QADecision.PASS.value:
                raise CandidateNotCommittable("only candidates with PASS validation can be committed")
            asset = session.get(MediaAsset, candidate.output_asset_id)
            if not asset or asset.project_id != shot.scene.episode.project_id:
                raise CandidateNotCommittable("candidate output does not belong to the shot project")
            output_state = session.get(TimelineState, shot.output_state_id) if shot.output_state_id else None
            if not output_state:
                raise CandidateNotCommittable("shot has no output timeline state")
            shot_id = shot.id
            asset_id = asset.id
        # Extraction and storage are deliberately non-canonical. Only the CAS
        # transaction below may mutate the shot, next shot, timeline, cost, or
        # candidate state. A losing concurrent request can at most leave a
        # content-addressed unreferenced frame for later garbage collection.
        end_frame = self.continuity.extract_end_frame(shot_id, asset_id)
        with self.database.session() as session:
            claimed = session.execute(
                update(Shot)
                .where(Shot.id == shot_id, Shot.committed_candidate_id.is_(None))
                .values(committed_candidate_id=candidate_id)
            )
            if affected_rows(claimed) != 1:
                current_shot = session.get(Shot, shot_id)
                current_candidate = session.get(GenerationCandidate, candidate_id)
                if (
                    current_shot
                    and current_candidate
                    and current_shot.committed_candidate_id == candidate_id
                    and current_candidate.status == CandidateStatus.COMMITTED.value
                ):
                    return current_candidate
                raise CandidateNotCommittable("another candidate was already adopted for this shot")
            candidate = session.get(GenerationCandidate, candidate_id)
            shot = session.get(Shot, shot_id)
            if (
                not candidate
                or candidate.shot_id != shot_id
                or candidate.status != CandidateStatus.PASSED.value
                or not candidate.qa_result_id
                or not candidate.output_asset_id
            ):
                raise CandidateNotCommittable("candidate changed before it could be adopted")
            qa = session.get(QAResult, candidate.qa_result_id)
            if not qa or qa.decision != QADecision.PASS.value:
                raise CandidateNotCommittable("candidate validation changed before adoption")
            asset = session.get(MediaAsset, candidate.output_asset_id)
            current_end_frame = session.get(MediaAsset, end_frame.id)
            if not shot or not asset or not current_end_frame:
                raise CandidateNotCommittable("candidate output disappeared before adoption")
            if asset.id != asset_id or asset.project_id != shot.scene.episode.project_id:
                raise CandidateNotCommittable("candidate output changed before adoption")
            output_state = session.get(TimelineState, shot.output_state_id) if shot.output_state_id else None
            if not output_state:
                raise CandidateNotCommittable("shot output state disappeared before adoption")
            other_candidates = list(
                session.scalars(
                    select(GenerationCandidate).where(
                        GenerationCandidate.shot_id == shot.id,
                        GenerationCandidate.id != candidate.id,
                    )
                )
            )
            if any(other.status == CandidateStatus.COMMITTED.value for other in other_candidates):
                raise CandidateNotCommittable("shot has multiple legacy committed candidates")
            candidate_claimed = session.execute(
                update(GenerationCandidate)
                .where(
                    GenerationCandidate.id == candidate_id,
                    GenerationCandidate.status == CandidateStatus.PASSED.value,
                    GenerationCandidate.qa_result_id == qa.id,
                    GenerationCandidate.output_asset_id == asset_id,
                )
                .values(
                    status=CandidateStatus.COMMITTED.value,
                    accepted_by=accepted_by,
                )
            )
            if affected_rows(candidate_claimed) != 1:
                raise CandidateNotCommittable("candidate validation changed during adoption")
            session.refresh(candidate)
            self.continuity.chain_existing_end_frame(
                session,
                shot,
                asset,
                current_end_frame,
            )
            state_json = deepcopy(output_state.state_json if output_state else {})
            state_json["committed_candidate_id"] = candidate.id
            state_json["output_asset_id"] = asset.id
            state_json["end_frame_asset_id"] = current_end_frame.id
            output_state.state_json = state_json
            output_state.semantic_embedding = self._embedding(shot.compiled_prompt or shot.prompt)
            output_state.visual_embedding = self._embedding(asset.sha256)
            output_state.camera_embedding = self._embedding(str(state_json.get("camera", {})))
            snapshot = ShotStateSnapshot(
                shot_id=shot.id,
                candidate_id=candidate.id,
                timeline_state_id=output_state.id,
                snapshot_json=state_json,
            )
            session.add(snapshot)
            session.flush()
            shot.output_video_asset_id = asset.id
            shot.status = ShotStatus.COMMITTED.value
            for other in other_candidates:
                if other.status == CandidateStatus.PASSED.value:
                    other.status = CandidateStatus.REJECTED.value
                    other.rejection_reason = "another candidate was committed"
            if candidate.generation_job_id:
                if self.visual_runtime is not None:
                    job = session.get(GenerationJob, candidate.generation_job_id)
                    existing_metric = session.scalar(
                        select(ModelMetric).where(
                            ModelMetric.generation_job_id == candidate.generation_job_id,
                            ModelMetric.metric_name == "user_accept",
                        )
                    )
                    if job and not existing_metric:
                        session.add(
                            ModelMetric(
                                provider=job.provider,
                                model_id=job.model,
                                metric_name="user_accept",
                                generation_job_id=job.id,
                                project_id=job.project_id,
                                shot_id=job.shot_id,
                            )
                        )
                session.add(
                    GenerationEvent(
                        generation_job_id=candidate.generation_job_id,
                        event_type="CANDIDATE_COMMITTED",
                        detail={"candidate_id": candidate.id, "snapshot_id": snapshot.id},
                    )
                )
            next_shot = session.get(Shot, shot.next_shot_id) if shot.next_shot_id else None
            if next_shot and next_shot.input_state_id:
                next_input = session.get(TimelineState, next_shot.input_state_id)
                next_input.state_json = deepcopy(output_state.state_json)
                next_input.previous_state_id = output_state.id
            candidate_ids = {item.id for item in other_candidates}
            for record in session.scalars(select(CostRecord).where(CostRecord.shot_id == shot.id)):
                if record.candidate_id == candidate_id:
                    record.accepted = True
                    record.wasted = False
                elif record.candidate_id in candidate_ids:
                    record.accepted = False
                    record.wasted = True
            session.add(
                DecisionRecord(
                    project_id=shot.scene.episode.project_id,
                    shot_id=shot.id,
                    decision_type="CANDIDATE_COMMIT",
                    input_features={"candidate_id": candidate_id, "qa": "PASS"},
                    selected_action="COMMIT",
                    reason_codes=["QA_PASS"],
                    model_version="commit-pipeline-v2-cas",
                )
            )
            session.flush()
            return session.get(GenerationCandidate, candidate_id)
