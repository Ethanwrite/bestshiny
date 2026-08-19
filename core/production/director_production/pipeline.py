from __future__ import annotations

import hashlib
from copy import deepcopy

from cost_core import CostEngine
from generation_gateway import GenerationGateway
from generation_policy_core import CapabilityResolver
from platform_contracts import GenerationRequest
from platform_database import Database
from production_domain.models import (
    CandidateStatus,
    CostRecord,
    DecisionRecord,
    GenerationCandidate,
    GenerationEvent,
    GenerationPolicy,
    MediaAsset,
    QADecision,
    QAResult,
    Shot,
    ShotStateSnapshot,
    ShotStatus,
    TimelineState,
)
from production_engine import ShotContinuityService
from qa_core import QAPipeline
from skill_core import PromptCompilerService
from sqlalchemy import func, select


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
    ):
        self.database = database
        self.gateway = gateway
        self.prompts = prompts
        self.capability_resolver = capability_resolver
        self.qa = qa
        self.cost = cost
        self.continuity = continuity

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
            attempt = (
                int(
                    session.scalar(
                        select(func.coalesce(func.max(GenerationCandidate.attempt_number), 0)).where(
                            GenerationCandidate.shot_id == shot.id
                        )
                    )
                    or 0
                )
                + 1
            )
            desired_policy = shot.generation_policy or GenerationPolicy.TEXT_TO_VIDEO.value
            preferred = shot.preferred_provider or shot.provider
            model = shot.preferred_model or shot.model
            shot_type = shot.shot_type
            duration = shot.duration
            aspect_ratio = shot.scene.episode.project.default_aspect_ratio
            start_frame_asset_id = shot.start_frame_asset_id
            end_frame_asset_id = shot.end_frame_asset_id
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
        with self.database.session() as session:
            candidate = GenerationCandidate(
                shot_id=shot_id,
                attempt_number=attempt,
                status=CandidateStatus.CREATED.value,
                metadata_json={"generation_plan": generation_plan},
            )
            session.add(candidate)
            session.flush()
            candidate_id = candidate.id
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
        try:
            job, replayed = self.gateway.create(request)
        except Exception:
            with self.database.session() as session:
                stale = session.get(GenerationCandidate, candidate_id)
                if stale and not stale.generation_job_id:
                    session.delete(stale)
            raise
        with self.database.session() as session:
            if replayed:
                stale = session.get(GenerationCandidate, candidate_id)
                if stale:
                    session.delete(stale)
                existing = session.get(GenerationCandidate, job.candidate_id) if job.candidate_id else None
                if not existing:
                    raise RuntimeError("replayed generation job has no candidate")
                return existing, True
            candidate = session.get(GenerationCandidate, candidate_id)
            candidate.generation_job_id = job.id
            candidate.status = CandidateStatus.GENERATING.value
            shot = session.get(Shot, shot_id)
            shot.status = ShotStatus.QUEUED.value
            shot.generation_policy = plan.policy
            shot.preferred_provider = plan.provider
            session.flush()
            return candidate, replayed

    def sync_candidate(self, candidate_id: str, evidence: dict | None = None) -> GenerationCandidate:
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate:
                raise LookupError("candidate not found")
            job_id = candidate.generation_job_id
            output_asset_id = candidate.output_asset_id
            shot = session.get(Shot, candidate.shot_id)
            shot_type = shot.shot_type if shot else None
        job = self.gateway.get(job_id) if job_id else None
        if job and job.output_asset_id and not output_asset_id:
            with self.database.session() as session:
                candidate = session.get(GenerationCandidate, candidate_id)
                candidate.output_asset_id = job.output_asset_id
                candidate.status = CandidateStatus.VALIDATING.value
                output_asset_id = job.output_asset_id
        if output_asset_id:
            self.qa.validate_candidate(candidate_id, evidence, profile=shot_type or "DIALOGUE")
        with self.database.session() as session:
            return session.get(GenerationCandidate, candidate_id)

    def commit(self, candidate_id: str, *, accepted_by: str | None = None) -> GenerationCandidate:
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate or not candidate.qa_result_id or not candidate.output_asset_id:
                raise CandidateNotCommittable("candidate has no completed QA result")
            qa = session.get(QAResult, candidate.qa_result_id)
            if qa.decision != QADecision.PASS.value:
                raise CandidateNotCommittable("only candidates with PASS validation can be committed")
            shot = session.get(Shot, candidate.shot_id)
            asset = session.get(MediaAsset, candidate.output_asset_id)
            output_state = session.get(TimelineState, shot.output_state_id) if shot.output_state_id else None
            if not output_state:
                raise CandidateNotCommittable("shot has no output timeline state")
            shot_id = shot.id
            asset_id = asset.id
        end_frame = self.continuity.extract_and_chain(shot_id, asset_id)
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            shot = session.get(Shot, candidate.shot_id)
            asset = session.get(MediaAsset, candidate.output_asset_id)
            output_state = session.get(TimelineState, shot.output_state_id)
            state_json = deepcopy(output_state.state_json if output_state else {})
            state_json["committed_candidate_id"] = candidate.id
            state_json["output_asset_id"] = asset.id
            state_json["end_frame_asset_id"] = end_frame.id
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
            candidate.status = CandidateStatus.COMMITTED.value
            candidate.accepted_by = accepted_by
            shot.committed_candidate_id = candidate.id
            shot.output_video_asset_id = asset.id
            shot.status = ShotStatus.COMMITTED.value
            other_candidates = session.scalars(
                select(GenerationCandidate).where(
                    GenerationCandidate.shot_id == shot.id,
                    GenerationCandidate.id != candidate.id,
                    GenerationCandidate.status != CandidateStatus.COMMITTED.value,
                )
            )
            for other in other_candidates:
                if other.status == CandidateStatus.PASSED.value:
                    other.status = CandidateStatus.REJECTED.value
                    other.rejection_reason = "another candidate was committed"
            if candidate.generation_job_id:
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
            for record in session.scalars(select(CostRecord).where(CostRecord.candidate_id == candidate_id)):
                record.accepted = True
                record.wasted = False
            session.add(
                DecisionRecord(
                    project_id=shot.scene.episode.project_id,
                    shot_id=shot.id,
                    decision_type="CANDIDATE_COMMIT",
                    input_features={"candidate_id": candidate_id, "qa": "PASS"},
                    selected_action="COMMIT",
                    reason_codes=["QA_PASS"],
                    model_version="commit-pipeline-v1",
                )
            )
            session.flush()
            return session.get(GenerationCandidate, candidate_id)
