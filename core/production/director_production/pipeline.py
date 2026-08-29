from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

from character_core import (
    CharacterIdentityService,
    CharacterStateError,
    PersistentCharacterStateService,
    canonical_json_hash,
    preview_character_state_transition,
    required_visual_state_paths,
)
from continuity_core import FrameAnchorPlan, FrameAnchorPlanner, FrameAnchorPlanUnresolved
from cost_core import CostEngine
from evaluation_core import EvaluationDecision, EvaluationEvidence
from generation_gateway import GenerationGateway
from generation_policy_core import (
    AvailableGenerationAssets,
    CapabilityResolver,
    GenerationPolicyEngine,
)
from narrative_core import AuthoritativeTimelineStateEngine
from narrative_ledger_core import (
    LedgerWriteConflict,
    NarrativeLedgerService,
    NarrativePosition,
    ShotDependencyService,
    ShotDependencyUnresolved,
)
from platform_contracts import (
    TIMELINE_FENCE_METADATA_KEY,
    AuthoritativeTimelineFence,
    GenerationRequest,
    authoritative_timeline_state_hash,
)
from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    Asset,
    AssetKind,
    AssetVersion,
    BillingEvidenceSource,
    CandidateStatus,
    CharacterStateDecision,
    CharacterStateDelta,
    CharacterStateProposalSource,
    ContinuityMode,
    CostRecord,
    DecisionOutcomeRecord,
    DecisionRecord,
    GenerationCandidate,
    GenerationEvent,
    GenerationJob,
    GenerationPolicy,
    MediaAsset,
    ModelMetric,
    ProviderBillingEvidence,
    QADecision,
    QAResult,
    Shot,
    ShotStateSnapshot,
    ShotStatus,
    TimelineState,
    new_id,
)
from production_engine import ShotContinuityService
from provider_sdk import (
    AssetCriticality,
    ProviderTrustLevel,
    ProviderTrustViolation,
    assert_provider_can_handle,
)
from qa_core import QAPipeline
from skill_core import PromptCompilerService
from sqlalchemy import func, select, update
from style_core import ProjectStyleService, StyleCommitViolation

if TYPE_CHECKING:
    from entitlement_core import GenerationAdmissionService
    from production_engine.runtime import VisualProductionRuntime


class CandidateNotCommittable(RuntimeError):
    pass


_CHARACTER_STATE_PROPOSAL_SET_HASH_KEY = "character_state_proposal_set_hash"


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
        generation_admission: GenerationAdmissionService | None = None,
        character_states: PersistentCharacterStateService | None = None,
        styles: ProjectStyleService | None = None,
        frame_anchors: FrameAnchorPlanner | None = None,
        characters: CharacterIdentityService | None = None,
        narrative_ledger: NarrativeLedgerService | None = None,
        shot_dependencies: ShotDependencyService | None = None,
    ):
        self.database = database
        self.gateway = gateway
        self.prompts = prompts
        self.capability_resolver = capability_resolver
        self.qa = qa
        self.cost = cost
        self.continuity = continuity
        self.visual_runtime = visual_runtime
        self.generation_admission = generation_admission
        self.character_states = character_states or PersistentCharacterStateService(database)
        self.styles = styles
        self.frame_anchors = frame_anchors
        self.characters = characters
        self.narrative_ledger = narrative_ledger
        self.shot_dependencies = shot_dependencies
        self.timeline = AuthoritativeTimelineStateEngine(database)
        self.policy = GenerationPolicyEngine(database)

    @staticmethod
    def _embedding(value: str) -> list[float]:
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        return [round(byte / 255, 6) for byte in digest[:16]]

    def _shot_project_id(self, shot_id: str) -> str:
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if shot is None:
                raise LookupError("shot not found")
            return shot.scene.episode.project_id

    def _record_generation_review(
        self,
        shot_id: str,
        project_id: str,
        *,
        decision_type: str,
        model_version: str,
        policy_version: str,
        reason_codes: tuple[str, ...],
    ) -> None:
        """A generation preflight refused: the shot goes to review, on record."""

        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if shot is not None and shot.status != ShotStatus.COMMITTED.value:
                shot.status = ShotStatus.USER_REVIEW_REQUIRED.value
            session.add(
                DecisionRecord(
                    project_id=project_id,
                    shot_id=shot_id,
                    decision_type=decision_type,
                    input_features={"reason_codes": list(reason_codes)},
                    selected_action="REVIEW_REQUIRED",
                    reason_codes=list(reason_codes),
                    model_version=model_version,
                    policy_version=policy_version,
                )
            )
            session.flush()

    def _record_dependency_review(
        self, shot_id: str, project_id: str, reason_codes: tuple[str, ...]
    ) -> None:
        """An unresolved explicit dependency moves the shot to review.

        The refusal is recorded and visible; nothing may quietly continue with
        similarity-only context in place of material the shot is owed.
        """

        self._record_generation_review(
            shot_id,
            project_id,
            decision_type="SHOT_DEPENDENCY_RESOLUTION",
            model_version="shot-dependency-gate-v1",
            policy_version="dependency-gate-v1",
            reason_codes=reason_codes,
        )

    def _record_plan_review(
        self, shot_id: str, project_id: str, reason_codes: tuple[str, ...]
    ) -> None:
        """A frame anchor plan that cannot be executed moves the shot to review."""

        self._record_generation_review(
            shot_id,
            project_id,
            decision_type="FRAME_ANCHOR_PLAN_RESOLUTION",
            model_version="frame-anchor-gate-v1",
            policy_version="frame-anchor-gate-v1",
            reason_codes=reason_codes,
        )

    @staticmethod
    def _dot_state_path(path: str) -> str:
        normalized = path.strip()
        if normalized.startswith("/"):
            return ".".join(part.replace("~1", "/").replace("~0", "~") for part in normalized.split("/")[1:])
        return normalized.strip(".")

    def _state_evidence_from_evaluation(
        self,
        candidate_id: str,
        evidence: EvaluationEvidence,
    ) -> dict[str, dict[str, object]]:
        """Translate trusted evaluator facts; embedding output remains advisory."""

        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            shot = session.get(Shot, candidate.shot_id) if candidate else None
            if candidate is None or shot is None:
                raise LookupError("candidate shot not found during state evidence translation")
            rows = list(
                session.scalars(
                    select(CharacterStateDelta)
                    .where(CharacterStateDelta.candidate_id == candidate.id)
                    .order_by(
                        CharacterStateDelta.character_id,
                        CharacterStateDelta.proposal_revision.desc(),
                    )
                )
            )
            latest: dict[str, CharacterStateDelta] = {}
            for row in rows:
                latest.setdefault(row.character_id, row)
            output_asset_id = candidate.output_asset_id
            scene_sequence = shot.scene.sequence
        result: dict[str, dict[str, object]] = {}
        only_character_id = next(iter(latest)) if len(latest) == 1 else None
        for character_id, delta in latest.items():
            required = set(
                required_visual_state_paths(
                    delta.proposed_state_json,
                    scene_sequence=scene_sequence,
                    changed_paths=delta.changed_paths_json,
                )
            )
            observations: list[dict[str, object]] = []
            advisory = "voyage" in f"{evidence.judge_provider} {evidence.judge_model}".casefold()
            for raw in evidence.state_observations:
                path = self._dot_state_path(raw.path)
                prefix = f"characters.{character_id}.narrative_state."
                if path.startswith(prefix):
                    relative_path = path[len(prefix) :]
                elif only_character_id == character_id and path in required:
                    relative_path = path
                else:
                    continue
                if relative_path not in required:
                    continue
                advisory = advisory or "voyage" in raw.source.casefold()
                observations.append(
                    {
                        "path": relative_path,
                        "value": raw.value,
                        "confidence": raw.confidence if raw.observable else 0.0,
                        "sample_times": [],
                    }
                )
            result[character_id] = {
                "source": f"VISUAL_EVALUATOR:{evidence.judge_provider}:{evidence.judge_model}"[:120],
                "authority_level": "ADVISORY" if advisory else "FACT_OBSERVATION",
                "producer_version": "evaluation-state-observation-v1",
                "model_execution_record_id": evidence.model_execution_record_id,
                "observations": observations,
                "evidence_asset_id": output_asset_id,
            }
        return result

    def _assert_commit_provider_trust(
        self,
        session,  # type: ignore[no-untyped-def]
        candidate: GenerationCandidate,
        asset: MediaAsset,
    ) -> None:
        providers: set[str] = set()
        if asset.provider:
            providers.add(asset.provider)
        if candidate.generation_job_id:
            job = session.get(GenerationJob, candidate.generation_job_id)
            if not job:
                raise CandidateNotCommittable("candidate generation provenance is missing")
            if job.project_id != asset.project_id:
                raise CandidateNotCommittable("candidate generation provenance belongs to another project")
            providers.add(job.provider)
        for provider_name in providers:
            try:
                provider = self.gateway.providers.get(provider_name)
                trust = ProviderTrustLevel(getattr(provider, "trust_level", ProviderTrustLevel.PRODUCTION))
                assert_provider_can_handle(trust, AssetCriticality.IMPORTANT)
            except (LookupError, ValueError, ProviderTrustViolation) as exc:
                raise CandidateNotCommittable(
                    "low-trust generation output cannot enter the committed timeline"
                ) from exc

    @staticmethod
    def _assert_candidate_timeline_fence(
        session,  # type: ignore[no-untyped-def]
        candidate: GenerationCandidate,
        shot: Shot,
    ) -> None:
        raw_fence = (candidate.metadata_json or {}).get(TIMELINE_FENCE_METADATA_KEY)
        if candidate.generation_job_id:
            job = session.get(GenerationJob, candidate.generation_job_id)
            if job is None:
                raise CandidateNotCommittable("candidate generation provenance is missing")
            metadata = (job.request_json or {}).get("metadata") or {}
            raw_fence = metadata.get(TIMELINE_FENCE_METADATA_KEY) or raw_fence
        if raw_fence is None:
            return
        try:
            fence = AuthoritativeTimelineFence.model_validate(raw_fence)
        except ValueError as exc:
            raise CandidateNotCommittable("candidate timeline fence is invalid") from exc
        input_state = session.get(TimelineState, shot.input_state_id) if shot.input_state_id else None
        output_state = session.get(TimelineState, shot.output_state_id) if shot.output_state_id else None
        if (
            fence.shot_id != shot.id
            or fence.input_state_id != shot.input_state_id
            or fence.output_state_id != shot.output_state_id
            or input_state is None
            or output_state is None
            or authoritative_timeline_state_hash(
                input_state.state_json,
                previous_state_id=input_state.previous_state_id,
            )
            != fence.input_state_hash
            or authoritative_timeline_state_hash(
                output_state.state_json,
                previous_state_id=output_state.previous_state_id,
            )
            != fence.output_state_hash
        ):
            raise CandidateNotCommittable(
                "authoritative timeline changed after generation; regenerate the candidate"
            )

    def _assert_narrative_commitability(
        self,
        session,  # type: ignore[no-untyped-def]
        candidate: GenerationCandidate,
        shot: Shot,
    ) -> None:
        """Revalidate the narrative context this candidate was generated from.

        Two checks, both inside the commit transaction. The declared
        dependencies are resolved again — an obligation settled elsewhere or a
        referent that stopped existing since generation refuses the commit.
        And the ledger fence stored at candidate creation is recomputed — any
        change to the visible ledger slice or to the shot's declared
        dependency set means the candidate was generated from an expired
        context and may not become canon. A candidate created before fences
        existed carries no stored fence and skips only the digest comparison;
        dependency revalidation still runs.
        """

        if self.shot_dependencies is not None:
            try:
                self.shot_dependencies.resolve_for_generation_in_session(session, shot.id)
            except ShotDependencyUnresolved as exc:
                raise CandidateNotCommittable(
                    "explicit dependencies no longer resolve: "
                    + ", ".join(exc.reason_codes)
                ) from exc
        if self.narrative_ledger is None:
            return
        stored = (candidate.metadata_json or {}).get("narrative_context_fence")
        if not isinstance(stored, dict) or not stored.get("fence"):
            return
        position_data = stored.get("position") or {}
        try:
            position = NarrativePosition(
                int(position_data["episode"]),
                int(position_data["scene_sequence"]),
                int(position_data["shot_sequence"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CandidateNotCommittable("candidate narrative fence is invalid") from exc
        current = self.narrative_ledger.context_fence_in_session(
            session,
            shot.scene.episode.project_id,
            position=position,
            shot_id=shot.id,
        )
        if current != stored["fence"]:
            raise CandidateNotCommittable(
                "narrative context changed after generation (ledger or declared "
                "dependencies moved); regenerate the candidate"
            )

    @staticmethod
    def _assert_character_state_proposal_fence(
        session,  # type: ignore[no-untyped-def]
        candidate: GenerationCandidate,
    ) -> None:
        """Bind the frozen state proposal set to the dispatched generation job."""

        proposal_hash = (candidate.metadata_json or {}).get(_CHARACTER_STATE_PROPOSAL_SET_HASH_KEY)
        if proposal_hash is None:
            return
        if not candidate.generation_job_id:
            raise CandidateNotCommittable("character state proposals are not bound to a generation job")
        job = session.get(GenerationJob, candidate.generation_job_id)
        job_metadata = ((job.request_json or {}).get("metadata") or {}) if job else {}
        if (
            job is None
            or job.candidate_id != candidate.id
            or job_metadata.get(_CHARACTER_STATE_PROPOSAL_SET_HASH_KEY) != proposal_hash
        ):
            raise CandidateNotCommittable(
                "character state proposals changed after generation dispatch; regenerate the candidate"
            )

    def create_candidate(
        self,
        shot_id: str,
        *,
        idempotency_key: str,
        fallback_providers: list[str] | None = None,
        character_bindings: list[dict] | None = None,
        reference_asset_ids: list[str] | None = None,
        estimated_cost: float = 0.0,
        enforce_entitlements: bool = True,
        state_deltas: list[dict[str, object]] | None = None,
        proposed_by_user_id: str | None = None,
        state_delta_source: str = CharacterStateProposalSource.RULES.value,
    ) -> tuple[GenerationCandidate, bool]:
        # The Frame Anchor Planner is a system-level generation gate, not a
        # step a caller opts into. Every generatable shot — compiled or
        # manually created — passes through here: a still-current stored plan
        # is reused, anything stale or absent is planned now, and the shot
        # state read below already reflects the decision. Explicit
        # `plan-frame-anchors` calls remain an inspect/re-plan surface only.
        # A planner that cannot produce a plan is itself a review condition:
        # the failure is recorded and typed, never an anonymous error.
        anchor_plan: FrameAnchorPlan | None = None
        if self.frame_anchors is not None:
            try:
                anchor_plan = self.frame_anchors.ensure_plan(shot_id)
            except LookupError:
                # Shot not found — the 404 the caller expects, not a plan failure.
                raise
            except FrameAnchorPlanUnresolved as exc:
                self._record_plan_review(shot_id, self._shot_project_id(shot_id), exc.reason_codes)
                raise
            except (ValueError, KeyError) as exc:
                codes = (f"FRAME_ANCHOR_PLANNING_FAILED:{type(exc).__name__}",)
                self._record_plan_review(shot_id, self._shot_project_id(shot_id), codes)
                raise FrameAnchorPlanUnresolved(shot_id, list(codes)) from exc
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise LookupError("shot not found")
            project_id = shot.scene.episode.project_id
            desired_policy = shot.generation_policy or GenerationPolicy.TEXT_TO_VIDEO.value
            preferred = shot.preferred_provider or shot.provider
            model = shot.preferred_model or shot.model
            shot_type = shot.shot_type
            scene_sequence = shot.scene.sequence
            duration = shot.duration
            aspect_ratio = shot.scene.episode.project.default_aspect_ratio
            start_frame_asset_id = shot.start_frame_asset_id
            end_frame_asset_id = shot.end_frame_asset_id
            continuity_mode = shot.continuity_mode
            input_state_id = shot.input_state_id
            planning_input_state = (
                session.get(TimelineState, shot.input_state_id) if shot.input_state_id else None
            )
            planning_output_state = (
                session.get(TimelineState, shot.output_state_id) if shot.output_state_id else None
            )
            candidate_timeline_fence = (
                AuthoritativeTimelineFence(
                    shot_id=shot.id,
                    shot_status=shot.status,
                    input_state_id=planning_input_state.id,
                    input_state_hash=authoritative_timeline_state_hash(
                        planning_input_state.state_json,
                        previous_state_id=planning_input_state.previous_state_id,
                    ),
                    output_state_id=planning_output_state.id,
                    output_state_hash=authoritative_timeline_state_hash(
                        planning_output_state.state_json,
                        previous_state_id=planning_output_state.previous_state_id,
                    ),
                ).model_dump(mode="json")
                if planning_input_state is not None and planning_output_state is not None
                else None
            )
            previous_end_frame_context_id = None
            if shot.previous_shot_id:
                previous_shot = session.get(Shot, shot.previous_shot_id)
                if previous_shot:
                    previous_end_frame_context_id = previous_shot.end_frame_asset_id
            supplied_references = list(
                session.scalars(
                    select(MediaAsset).where(
                        MediaAsset.id.in_(reference_asset_ids or []),
                        MediaAsset.project_id == project_id,
                    )
                )
            )
            canonical_scene_reference_ids = tuple(
                media_id
                for media_id in session.scalars(
                    select(AssetVersion.primary_media_asset_id)
                    .join(Asset, Asset.canonical_version_id == AssetVersion.id)
                    .where(
                        Asset.project_id == project_id,
                        Asset.asset_type == AssetKind.SCENE.value,
                        AssetVersion.primary_media_asset_id.is_not(None),
                    )
                )
                if media_id
            )
            # The planner chose one scene anchor; resolve its canonical media
            # here so the reference set below can carry exactly that plate
            # instead of every scene the project owns.
            anchor_scene_media_id = (
                session.scalar(
                    select(AssetVersion.primary_media_asset_id)
                    .join(Asset, Asset.canonical_version_id == AssetVersion.id)
                    .where(Asset.id == anchor_plan.scene_asset_id)
                )
                if anchor_plan is not None and anchor_plan.scene_asset_id
                else None
            )
        # Automatic role selection: the planner named the characters this
        # shot's frame must carry, and a caller who supplied no binding for one
        # of them does not silently generate without it. Subjects with a
        # confirmed identity are bound here through the same service the API
        # route uses; a required subject that cannot be bound is a plan
        # failure, not a shrug.
        if anchor_plan is not None and self.characters is not None and anchor_plan.anchor_subjects:
            already_bound = {
                str(binding.get("character_id"))
                for binding in (character_bindings or [])
                if binding.get("character_id")
            }
            auto_bindings: list[dict] = []
            unbindable: list[str] = []
            for subject in anchor_plan.anchor_subjects:
                if not subject.character_id or subject.character_id in already_bound:
                    continue
                if not subject.identity_version_id:
                    # No confirmed identity — nothing exists to bind. Downgraded
                    # plans carry such subjects by design.
                    continue
                try:
                    auto_bindings.append(
                        self.characters.binding(
                            subject.character_id,
                            project_id=project_id,
                            timeline_state_id=input_state_id,
                        )
                    )
                except (LookupError, ValueError):
                    if anchor_plan.requires_keyframe_generation:
                        unbindable.append(f"ANCHOR_SUBJECT_UNBINDABLE:{subject.character_id}")
            if unbindable:
                self._record_plan_review(shot_id, project_id, tuple(unbindable))
                raise FrameAnchorPlanUnresolved(shot_id, unbindable)
            if auto_bindings:
                character_bindings = [*(character_bindings or []), *auto_bindings]

        effective_character_bindings = deepcopy(character_bindings or [])
        bindings_by_character = {
            str(binding.get("character_id")): binding for binding in effective_character_bindings
        }
        seen_preview_characters: set[str] = set()
        for raw_delta in state_deltas or []:
            if not isinstance(raw_delta, dict):
                raise ValueError("character state deltas must be objects")
            character_id = str(raw_delta.get("character_id") or "")
            if not character_id or character_id in seen_preview_characters:
                raise ValueError("each character may have one state delta per generation attempt")
            seen_preview_characters.add(character_id)
            binding = bindings_by_character.get(character_id)
            if binding is None or not binding.get("narrative_state_version_id"):
                raise ValueError("state delta character has no authoritative narrative-state binding")
            if str(raw_delta.get("base_state_version_id") or "") != binding.get("narrative_state_version_id"):
                raise ValueError("state delta base does not match the generated character binding")
            patch_json = raw_delta.get("patch")
            if not isinstance(patch_json, dict):
                raise ValueError("state delta patch must be an object")
            preview = preview_character_state_transition(
                binding.get("narrative_state") or {},
                patch_json,
                scene_sequence=scene_sequence,
            )
            binding.update(
                {
                    "proposed_narrative_state": preview.target_state,
                    "proposed_narrative_state_hash": canonical_json_hash(preview.target_state),
                    "proposed_state_patch": preview.normalized_patch,
                    "proposed_state_changed_paths": list(preview.changed_paths),
                    "proposed_state_required_visual_paths": list(preview.required_visual_paths),
                }
            )
        for binding in effective_character_bindings:
            target_state = binding.get("proposed_narrative_state") or binding.get("narrative_state")
            if not isinstance(target_state, dict):
                continue
            binding["generation_required_visual_state_paths"] = list(
                required_visual_state_paths(
                    target_state,
                    scene_sequence=scene_sequence,
                    changed_paths=binding.get("proposed_state_changed_paths") or [],
                )
            )
        character_bindings = effective_character_bindings
        character_state_context = [
            {
                "character_id": binding.get("character_id"),
                "narrative_state_version_id": binding.get("narrative_state_version_id"),
                "narrative_state_version": binding.get("narrative_state_version"),
                "narrative_state_hash": binding.get("narrative_state_hash"),
                "timeline_scope_key": binding.get("timeline_scope_key"),
            }
            for binding in (character_bindings or [])
            if binding.get("narrative_state_version_id")
        ]

        # What the ledger showed this generation, digested. Commit recomputes
        # and refuses on mismatch, so a candidate generated before the story
        # moved cannot be adopted against the story as it now stands.
        narrative_context_fence = (
            self.narrative_ledger.context_fence_for_shot(shot_id)
            if self.narrative_ledger is not None
            else None
        )

        def initialize_character_state(
            session,  # type: ignore[no-untyped-def]
            candidate: GenerationCandidate,
        ) -> None:
            candidate.metadata_json = {
                **candidate.metadata_json,
                "character_state_context": character_state_context,
                **(
                    {TIMELINE_FENCE_METADATA_KEY: candidate_timeline_fence}
                    if candidate_timeline_fence is not None
                    else {}
                ),
                **(
                    {"narrative_context_fence": narrative_context_fence}
                    if narrative_context_fence is not None
                    else {}
                ),
            }
            bindings_by_character = {
                str(binding.get("character_id")): binding for binding in (character_bindings or [])
            }
            seen_characters: set[str] = set()
            persisted_proposals: list[dict[str, object]] = []
            for raw_delta in state_deltas or []:
                if not isinstance(raw_delta, dict):
                    raise ValueError("character state deltas must be objects")
                character_id = str(raw_delta.get("character_id") or "")
                if not character_id or character_id in seen_characters:
                    raise ValueError("each character may have one state delta per generation attempt")
                seen_characters.add(character_id)
                binding = bindings_by_character.get(character_id)
                if binding is None or not binding.get("narrative_state_version_id"):
                    raise ValueError("state delta character has no authoritative narrative-state binding")
                base_state_version_id = str(raw_delta.get("base_state_version_id") or "")
                if base_state_version_id != binding.get("narrative_state_version_id"):
                    raise ValueError("state delta base does not match the generated character binding")
                patch_json = raw_delta.get("patch")
                if not isinstance(patch_json, dict):
                    raise ValueError("state delta patch must be an object")
                delta_key = hashlib.sha256(
                    f"{idempotency_key}:{candidate.id}:{character_id}".encode()
                ).hexdigest()
                persisted = self.character_states.propose_for_candidate_in_session(
                    session,
                    candidate=candidate,
                    character_id=character_id,
                    base_state_version_id=base_state_version_id,
                    patch_json=patch_json,
                    idempotency_key=f"candidate-state:{delta_key}",
                    source_kind=state_delta_source,
                    proposed_by_user_id=proposed_by_user_id,
                    model_execution_record_id=(
                        str(raw_delta["model_execution_record_id"])
                        if raw_delta.get("model_execution_record_id")
                        else None
                    ),
                )
                persisted_proposals.append(
                    {
                        "character_id": persisted.character_id,
                        "state_delta_id": persisted.id,
                        "base_state_version_id": persisted.base_state_version_id,
                        "target_version": persisted.target_version,
                        "target_state_hash": persisted.target_state_hash,
                        "changed_paths": persisted.changed_paths_json,
                    }
                )
            if persisted_proposals:
                candidate.metadata_json = {
                    **candidate.metadata_json,
                    "character_state_proposals": persisted_proposals,
                }

        policy_assets: AvailableGenerationAssets | None = None
        effective_reference_asset_ids = list(dict.fromkeys(reference_asset_ids or []))
        if continuity_mode in {
            "HARD_CONTINUITY",
            "HYBRID",
            "RE_ANCHOR",
        }:
            previous_end_frame_asset_id = (
                previous_end_frame_context_id
                if continuity_mode == ContinuityMode.HYBRID.value
                else start_frame_asset_id
            )
            # The planner decided *which* characters and scene anchor this
            # shot's frame. Those decisions govern the reference set: bindings
            # are narrowed to the anchor subjects (their identity masters
            # added), and the scene reference is the plate the planner chose —
            # not every character or scene the project happens to own. Without
            # a plan (planner unwired), the previous everything-canonical
            # behaviour stands.
            anchor_subject_ids = (
                {subject.character_id for subject in anchor_plan.anchor_subjects}
                if anchor_plan is not None
                else set()
            )
            anchor_master_asset_ids = (
                [
                    subject.master_asset_id
                    for subject in anchor_plan.anchor_subjects
                    if subject.master_asset_id
                ]
                if anchor_plan is not None
                else []
            )
            canonical_character_assets = tuple(
                dict.fromkeys(
                    [
                        *(
                            str(asset_id)
                            for binding in (character_bindings or [])
                            if not anchor_subject_ids
                            or str(binding.get("character_id")) in anchor_subject_ids
                            for asset_id in binding.get("canonical_assets", [])
                            if asset_id
                        ),
                        *anchor_master_asset_ids,
                    ]
                )
            )
            scene_references = tuple(
                dict.fromkeys(
                    [
                        *(
                            [anchor_scene_media_id]
                            if anchor_scene_media_id
                            else canonical_scene_reference_ids
                        ),
                        *[
                            asset.id
                            for asset in supplied_references
                            if asset.asset_type in {"LOCATION_REFERENCE", "LOCATION_MASTER"}
                        ],
                    ]
                )
            )
            if (
                anchor_plan is not None
                and anchor_plan.requires_keyframe_generation
                and not (anchor_master_asset_ids or anchor_scene_media_id)
            ):
                # The plan stood on canon that has since disappeared. Refusing
                # here beats submitting a keyframe reconstruction with nothing
                # to reconstruct from — and the refusal is a review state, on
                # record, not an anonymous error.
                codes = ("ANCHOR_REFERENCES_UNRESOLVED",)
                self._record_plan_review(shot_id, project_id, codes)
                raise FrameAnchorPlanUnresolved(shot_id, list(codes))
            policy_assets = AvailableGenerationAssets(
                previous_end_frame_asset_id=previous_end_frame_asset_id,
                start_frame_asset_id=start_frame_asset_id,
                end_frame_asset_id=end_frame_asset_id,
                character_reference_asset_ids=canonical_character_assets,
                scene_reference_asset_ids=scene_references,
                generic_reference_asset_ids=tuple(reference_asset_ids or []),
                character_binding=bool(character_bindings),
            )
            policy_decision = self.policy.decide(
                continuity_mode,
                policy_assets,
                project_id=project_id,
                shot_id=shot_id,
            )
            desired_policy = policy_decision.policy
            effective_reference_asset_ids = list(
                dict.fromkeys(
                    [
                        *effective_reference_asset_ids,
                        *(
                            [previous_end_frame_asset_id]
                            if continuity_mode == ContinuityMode.HYBRID.value and previous_end_frame_asset_id
                            else []
                        ),
                        *canonical_character_assets,
                        *scene_references,
                    ]
                )
            )
            if policy_decision.require_new_keyframe:
                start_frame_asset_id = None
            with self.database.session() as session:
                planned_shot = session.get(Shot, shot_id)
                if planned_shot is None:
                    raise LookupError("shot disappeared during generation policy planning")
                planned_shot.generation_policy = desired_policy
                if policy_decision.require_new_keyframe:
                    planned_shot.start_frame_asset_id = None
        if self.visual_runtime is not None:
            candidate_id = new_id()
            allowed_providers = list(
                dict.fromkeys(
                    [preferred, *(fallback_providers or ["google_flow", "seedance", "veo_official"])]
                )
            )
            if self.generation_admission is not None and enforce_entitlements:
                plan_context = self.generation_admission.workspace_models.context_for_project(project_id)
                if plan_context.plan_tier.value == "FREE":
                    allowed_providers = ["seedance"]
            try:
                prepared = self.visual_runtime.prepare_autopilot(
                    shot_id,
                    idempotency_key=idempotency_key,
                    candidate_id=candidate_id,
                    character_bindings=character_bindings,
                    reference_asset_ids=effective_reference_asset_ids,
                    estimated_cost=0.0,
                    allowed_providers=allowed_providers,
                    frame_anchor_plan=(anchor_plan.as_json() if anchor_plan is not None else None),
                )
            except ShotDependencyUnresolved as exc:
                self._record_dependency_review(shot_id, project_id, exc.reason_codes)
                raise
            admitted = (
                self.generation_admission.admit_autopilot(
                    prepared.request,
                    enforce_plan=enforce_entitlements,
                )
                if self.generation_admission is not None
                else None
            )
            if admitted is not None:
                prepared = replace(prepared, request=admitted.request)
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
                **(
                    {
                        "frame_anchor": anchor_plan.as_json(),
                        "requires_keyframe_generation": anchor_plan.requires_keyframe_generation,
                    }
                    if anchor_plan is not None
                    else {}
                ),
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
                # Runtime trace rows already reference this job. Persist the job
                # after its candidate FK exists and before state-service queries
                # can trigger an autoflush of those audit rows.
                session.add(job)
                session.flush([job])
                initialize_character_state(session, candidate)
                proposal_hash = candidate.metadata_json.get(_CHARACTER_STATE_PROPOSAL_SET_HASH_KEY)
                if proposal_hash is not None:
                    job.request_json = {
                        **job.request_json,
                        "metadata": {
                            **(job.request_json.get("metadata") or {}),
                            _CHARACTER_STATE_PROPOSAL_SET_HASH_KEY: proposal_hash,
                        },
                    }
                locked_shot.generation_policy = prepared.request.generation_policy
                locked_shot.preferred_provider = prepared.request.provider
                locked_shot.preferred_model = prepared.request.model

            job, replayed = self.visual_runtime.submit_autopilot(
                prepared,
                on_create=initialize_candidate,
                estimated_credits=(admitted.estimate.credits if admitted is not None else None),
                pricing_version=(
                    self.generation_admission.pricing.version if self.generation_admission is not None else ""
                ),
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
            available_inputs=policy_assets.available_inputs() if policy_assets else None,
        )
        try:
            compilation = self.prompts.compile_shot(
                shot_id,
                provider=plan.provider,
                model=model,
                character_bindings=character_bindings,
            )
        except ShotDependencyUnresolved as exc:
            self._record_dependency_review(shot_id, project_id, exc.reason_codes)
            raise
        generation_plan = {
            "policy": plan.policy,
            "required_inputs": plan.required_inputs,
            "provider": plan.provider,
            "fallback_providers": plan.fallback_providers,
            "degraded_from": plan.degraded_from,
            "reasons": plan.reasons,
            **(
                {
                    "frame_anchor": anchor_plan.as_json(),
                    "requires_keyframe_generation": anchor_plan.requires_keyframe_generation,
                }
                if anchor_plan is not None
                else {}
            ),
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
            reference_asset_ids=effective_reference_asset_ids,
            idempotency_key=idempotency_key,
            generation_policy=plan.policy,
            cost_estimate=estimated_cost,
            metadata={
                "generation_plan": generation_plan,
                **({"frame_anchor": anchor_plan.as_json()} if anchor_plan is not None else {}),
            },
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
            session.add(job)
            session.flush([job])
            initialize_character_state(session, candidate)
            proposal_hash = candidate.metadata_json.get(_CHARACTER_STATE_PROPOSAL_SET_HASH_KEY)
            if proposal_hash is not None:
                job.request_json = {
                    **job.request_json,
                    "metadata": {
                        **(job.request_json.get("metadata") or {}),
                        _CHARACTER_STATE_PROPOSAL_SET_HASH_KEY: proposal_hash,
                    },
                }
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
        validation_evidence = dict(evidence or {})
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if not candidate:
                raise LookupError("candidate not found")
            self._assert_character_state_proposal_fence(session, candidate)
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
            style_evaluation = (
                self.styles.evaluate_candidate(candidate_id) if self.styles is not None else None
            )
            auto_evaluation_enabled = bool(
                job
                and self.visual_runtime is not None
                and self.visual_runtime.flags.enabled("auto_evaluation", project_id=job.project_id)
            )
            evaluation_evidence: EvaluationEvidence | None = None
            if auto_evaluation_enabled:
                evaluation_payload = validation_evidence.get("evaluation_evidence")
                evaluation_evidence = (
                    EvaluationEvidence.model_validate(evaluation_payload)
                    if evaluation_payload is not None
                    else None
                )
                if evaluation_evidence is not None:
                    derived_state_evidence = self._state_evidence_from_evaluation(
                        candidate_id,
                        evaluation_evidence,
                    )
                    if derived_state_evidence:
                        validation_evidence["character_state_evidence"] = derived_state_evidence
            legacy_qa = self.qa.validate_candidate(
                candidate_id,
                validation_evidence,
                profile=shot_type or "DIALOGUE",
                defer_pass=auto_evaluation_enabled,
                style_evaluation=style_evaluation,
            )
            if auto_evaluation_enabled and legacy_qa.decision == QADecision.PASS.value:
                assert job is not None and self.visual_runtime is not None
                try:
                    visual_result, retry_plan, retry_job = self.visual_runtime.evaluate_job(
                        job.id, evaluation_evidence
                    )
                except (ShotDependencyUnresolved, FrameAnchorPlanUnresolved):
                    # A retry that cannot rebuild its owed context, or a plan
                    # that stopped being executable, is a review condition —
                    # not an anonymous evaluation error. The shot was already
                    # moved to review where the refusal was raised; the
                    # candidate follows it instead of hard-failing.
                    with self.database.session() as session:
                        held = session.execute(
                            update(GenerationCandidate)
                            .where(
                                GenerationCandidate.id == candidate_id,
                                GenerationCandidate.status == CandidateStatus.VALIDATING.value,
                                GenerationCandidate.qa_result_id == legacy_qa.id,
                            )
                            .values(status=CandidateStatus.USER_REVIEW_REQUIRED.value)
                        )
                        qa_result = session.get(QAResult, legacy_qa.id)
                        if affected_rows(held) == 1 and qa_result:
                            qa_result.decision = QADecision.USER_REVIEW_REQUIRED.value
                            qa_result.summary = (
                                "USER_REVIEW_REQUIRED: retry refused — dependencies or "
                                "frame anchor plan need review"
                            )
                    raise
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
                runtime_state_evidence = self._state_evidence_from_evaluation(
                    candidate_id,
                    EvaluationEvidence(
                        scores=visual_result.scores,
                        state_observations=visual_result.state_observations,
                        evidence_complete=visual_result.evidence_complete,
                        judge_provider=visual_result.judge_provider,
                        judge_model=visual_result.judge_model,
                        model_execution_record_id=(visual_result.model_execution_record_id),
                    ),
                )
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
                    if qa_result is not None and runtime_state_evidence:
                        qa_result.metrics_json = {
                            **qa_result.metrics_json,
                            "character_state_evidence": runtime_state_evidence,
                        }
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
            state_candidate = session.get(GenerationCandidate, candidate_id)
            state_qa_id = state_candidate.qa_result_id if state_candidate else None
            should_validate_state = bool(
                state_candidate and state_qa_id and state_candidate.status == CandidateStatus.PASSED.value
            )
        if should_validate_state and state_qa_id:
            state_validation = self.character_states.validate_candidate(candidate_id, state_qa_id)
            if state_validation.delta_ids:
                with self.database.session() as session:
                    candidate_for_state = session.get(GenerationCandidate, candidate_id)
                    qa_for_state = session.get(QAResult, state_qa_id)
                    if (
                        candidate_for_state is not None
                        and qa_for_state is not None
                        and candidate_for_state.qa_result_id == qa_for_state.id
                        and candidate_for_state.status == CandidateStatus.PASSED.value
                    ):
                        qa_for_state.metrics_json = {
                            **qa_for_state.metrics_json,
                            "character_state_validation": {
                                "decision": state_validation.decision,
                                "delta_ids": list(state_validation.delta_ids),
                                "reason_codes": list(state_validation.reason_codes),
                            },
                        }
                        if state_validation.decision == CharacterStateDecision.REJECT.value:
                            candidate_for_state.status = CandidateStatus.HARD_FAILED.value
                            qa_for_state.decision = QADecision.HARD_FAIL.value
                            qa_for_state.hard_failures = list(
                                dict.fromkeys(
                                    [
                                        *qa_for_state.hard_failures,
                                        "CHARACTER_STATE_EVIDENCE_MISMATCH",
                                    ]
                                )
                            )
                            qa_for_state.summary = "HARD_FAIL: character state evidence mismatch"
                        elif state_validation.decision == CharacterStateDecision.REVIEW_REQUIRED.value:
                            candidate_for_state.status = CandidateStatus.USER_REVIEW_REQUIRED.value
                            qa_for_state.decision = QADecision.USER_REVIEW_REQUIRED.value
                            qa_for_state.summary = "USER_REVIEW_REQUIRED: character state evidence incomplete"
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if candidate is None:
                raise LookupError("candidate disappeared after validation")
            shot = session.get(Shot, candidate.shot_id)
            qa_result = session.get(QAResult, candidate.qa_result_id) if candidate.qa_result_id else None
            if shot is not None and qa_result is not None:
                outcome = {
                    CandidateStatus.PASSED.value: "QA_PASS_PENDING_COMMIT",
                    CandidateStatus.HARD_FAILED.value: "QA_REJECTED",
                }.get(candidate.status, candidate.status)
                self._upsert_decision_outcome(
                    session,
                    candidate=candidate,
                    shot=shot,
                    qa=qa_result,
                    user_outcome=outcome,
                    accepted=False,
                )
            session.flush()
            return candidate

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
            self._assert_candidate_timeline_fence(session, candidate, shot)
            self._assert_character_state_proposal_fence(session, candidate)
            self._assert_narrative_commitability(session, candidate, shot)
            if self.styles is not None:
                try:
                    self.styles.assert_candidate_committable_in_session(session, candidate)
                except StyleCommitViolation as exc:
                    raise CandidateNotCommittable(str(exc)) from exc
            asset = session.get(MediaAsset, candidate.output_asset_id)
            if not asset or asset.project_id != shot.scene.episode.project_id:
                raise CandidateNotCommittable("candidate output does not belong to the shot project")
            self._assert_commit_provider_trust(session, candidate, asset)
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
            self._assert_candidate_timeline_fence(session, candidate, shot)
            self._assert_character_state_proposal_fence(session, candidate)
            self._assert_narrative_commitability(session, candidate, shot)
            if self.styles is not None:
                try:
                    self.styles.assert_candidate_committable_in_session(session, candidate)
                except StyleCommitViolation as exc:
                    raise CandidateNotCommittable(str(exc)) from exc
            self._assert_commit_provider_trust(session, candidate, asset)
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
            try:
                self.character_states.commit_candidate_in_session(
                    session,
                    candidate=candidate,
                    shot=shot,
                    qa=qa,
                    output_state=output_state,
                    committed_by_user_id=accepted_by,
                )
            except CharacterStateError as exc:
                raise CandidateNotCommittable(str(exc)) from exc
            # The narrative closed loop: the shot's declared ledger effects —
            # fact establishment, disclosure, obligation opening and
            # settlement — become canon in this same transaction, exactly
            # once, at the shot's complete narrative position. State
            # inheritance is written below by `timeline.propagate`. A ledger
            # conflict (an obligation settled elsewhere meanwhile, a fact key
            # colliding) refuses the commit rather than faking agreement.
            applied_effect_keys: list[str] = []
            if self.narrative_ledger is not None:
                try:
                    applied_effect_keys = self.narrative_ledger.apply_shot_effects_in_session(
                        session, shot, candidate_id=candidate.id
                    )
                except (LedgerWriteConflict, LookupError) as exc:
                    raise CandidateNotCommittable(str(exc)) from exc
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
            self.timeline.propagate(session, shot, output_state)
            candidate_ids = {item.id for item in other_candidates}
            for record in session.scalars(select(CostRecord).where(CostRecord.shot_id == shot.id)):
                if record.candidate_id == candidate_id:
                    record.accepted = True
                    record.wasted = False
                elif record.candidate_id in candidate_ids:
                    record.accepted = False
                    record.wasted = True
            self._upsert_decision_outcome(
                session,
                candidate=candidate,
                shot=shot,
                qa=qa,
                user_outcome="COMMITTED",
                accepted=True,
            )
            for other in other_candidates:
                if other.status != CandidateStatus.REJECTED.value:
                    continue
                other_qa = session.get(QAResult, other.qa_result_id) if other.qa_result_id else None
                self._upsert_decision_outcome(
                    session,
                    candidate=other,
                    shot=shot,
                    qa=other_qa,
                    user_outcome="REJECTED_BY_ALTERNATE_COMMIT",
                    accepted=False,
                )
            session.add(
                DecisionRecord(
                    project_id=shot.scene.episode.project_id,
                    shot_id=shot.id,
                    decision_type="CANDIDATE_COMMIT",
                    input_features={
                        "candidate_id": candidate_id,
                        "qa": "PASS",
                        # The narrative writes this commit performed, so "what
                        # did committing this shot make canon" reads from the
                        # decision record rather than from diffing the ledger.
                        "narrative_effects_applied": applied_effect_keys,
                        "state_inheritance": "TIMELINE_PROPAGATED",
                    },
                    selected_action="COMMIT",
                    reason_codes=["QA_PASS"],
                    model_version="commit-pipeline-v3-narrative",
                )
            )
            session.flush()
            return session.get(GenerationCandidate, candidate_id)

    def _upsert_decision_outcome(
        self,
        session,  # type: ignore[no-untyped-def]
        *,
        candidate: GenerationCandidate,
        shot: Shot,
        qa: QAResult | None,
        user_outcome: str,
        accepted: bool,
    ) -> DecisionOutcomeRecord:
        job = session.get(GenerationJob, candidate.generation_job_id) if candidate.generation_job_id else None
        cost = session.scalar(
            select(CostRecord)
            .where(CostRecord.candidate_id == candidate.id)
            .order_by(CostRecord.created_at.desc())
        )
        trusted_billing = None
        if job is not None:
            trusted_billing = session.scalar(
                select(ProviderBillingEvidence)
                .where(
                    ProviderBillingEvidence.generation_job_id == job.id,
                    ProviderBillingEvidence.source.in_(
                        [
                            BillingEvidenceSource.VERIFIED_PROVIDER.value,
                            BillingEvidenceSource.RECONCILED_MANUAL.value,
                        ]
                    ),
                    ProviderBillingEvidence.actual_cost_usd.is_not(None),
                )
                .order_by(ProviderBillingEvidence.verified_at.desc())
            )
        if trusted_billing is not None:
            billing_source = trusted_billing.source
            actual_cost = trusted_billing.actual_cost_usd
        else:
            billing_source = (
                BillingEvidenceSource.ESTIMATED.value
                if cost is not None and cost.estimated_cost > 0
                else BillingEvidenceSource.UNKNOWN.value
            )
            actual_cost = None
        estimated_cost = (
            trusted_billing.estimated_cost_usd
            if trusted_billing is not None and trusted_billing.estimated_cost_usd is not None
            else (cost.estimated_cost if cost is not None else None)
        )
        qa_json = (
            {
                "id": qa.id,
                "profile": qa.profile,
                "decision": qa.decision,
                "overall_score": qa.overall_score,
                "character_score": qa.character_score,
                "scene_score": qa.scene_score,
                "composition_score": qa.composition_score,
                "action_score": qa.action_score,
                "camera_score": qa.camera_score,
                "lighting_score": qa.lighting_score,
                "narrative_score": qa.narrative_score,
                "hard_failures": qa.hard_failures,
                "metrics": qa.metrics_json,
            }
            if qa is not None
            else {}
        )
        values = {
            "project_id": shot.scene.episode.project_id,
            "shot_id": shot.id,
            "generation_job_id": job.id if job is not None else None,
            "qa_result_id": qa.id if qa is not None else None,
            "continuity_decision": shot.continuity_mode,
            "generation_policy": shot.generation_policy,
            "provider": (
                job.provider
                if job is not None
                else (cost.provider if cost is not None else shot.preferred_provider)
            ),
            "model": (
                job.model if job is not None else (cost.model if cost is not None else shot.preferred_model)
            ),
            "shot_features_json": {
                "sequence": shot.sequence,
                "shot_type": shot.shot_type,
                "duration": shot.duration,
                "status": shot.status,
                "continuity_policy": shot.continuity_policy,
                "prompt_hash": hashlib.sha256(
                    (shot.compiled_prompt or shot.prompt).encode("utf-8")
                ).hexdigest(),
                "generation_plan": candidate.metadata_json.get("generation_plan", {}),
            },
            "qa_result_json": qa_json,
            "user_outcome": user_outcome,
            "accepted": accepted,
            "estimated_cost_usd": estimated_cost,
            "actual_cost_usd": actual_cost,
            "billing_source": billing_source,
        }
        outcome = session.scalar(
            select(DecisionOutcomeRecord).where(DecisionOutcomeRecord.candidate_id == candidate.id)
        )
        if outcome is None:
            outcome = DecisionOutcomeRecord(candidate_id=candidate.id, **values)
            session.add(outcome)
        else:
            for field, value in values.items():
                setattr(outcome, field, value)
        return outcome
