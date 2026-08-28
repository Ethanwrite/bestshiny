"""Semantic consistency across Retrieval → Compiler → Planner → Generation → Retry.

The invariants pinned here:
1. Retrieval context survives model fallback (retry reuses it verbatim).
2. SERIES_FACT reaches the model input on every prompt surface.
3. FrameAnchorPlanner decisions control the actual generation request.
4. No generatable shot bypasses the planner — it is a generation preflight.
5. A retry may switch provider/model representation only; assembled_text,
   continuity facts, subject anchors, scene anchors and the plan are fixed.
"""

from __future__ import annotations

from typing import Any

from evaluation_core import EvaluationDecision, RetryPlan
from narrative_ledger_core import (
    NarrativeLedgerService,
    ShotDependencyService,
)
from production_domain.models import (
    AssetKind,
    Character,
    CharacterIdentityVersion,
    ContinuityMode,
    DecisionRecord,
    Episode,
    GenerationJob,
    Location,
    MediaAsset,
    Scene,
    Shot,
    ShotDependencyType,
    ShotStatus,
    TimelineState,
)
from provider_sdk import GenerationProvider, ProviderJob, ProviderSubmission
from sqlalchemy import select

SCRIPT = """INT. KITCHEN - DAY
LinJin picks up the phone.
LinJin turns toward the door.
INT. HALLWAY - NIGHT
LinJin walks toward the door.
"""

FACT_SUMMARY = "The phone is bugged."
PROMISE = "Reveal who bugged the phone."
DEP_SUMMARY = "the kitchen phone pays off in the hallway"


def _compile(container, project, script: str = SCRIPT):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="Episode 1",
            episode_number=1,
            script_source=script,
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    return episode_id, container.narrative.compile_episode(episode_id)


def _media(container, project_id: str, name: str, sha_seed: str) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        media = MediaAsset(
            project_id=project_id,
            asset_type="CHARACTER_MASTER",
            sha256=(sha_seed * 64)[:64],
            storage_key=f"tests/{name}.png",
            mime_type="image/png",
        )
        session.add(media)
        session.flush()
        return media.id


def _full_canon(container, project):  # type: ignore[no-untyped-def]
    """LinJin identity master + a canonical hallway plate, via sanctioned paths."""

    lin_master = _media(container, project.id, "lin-master", "a")
    with container.database.session() as session:
        lin = session.scalar(
            select(Character).where(Character.project_id == project.id, Character.name == "LinJin")
        )
        identity = CharacterIdentityVersion(character_id=lin.id, version=1, master_asset_id=lin_master)
        session.add(identity)
        session.flush()
        lin.current_identity_version_id = identity.id
        lin_id = lin.id
        hallway = session.scalar(
            select(Location).where(Location.project_id == project.id, Location.name == "HALLWAY")
        )
        hallway_id = hallway.id
    plate = _media(container, project.id, "hallway-plate", "b")
    scene_asset = container.asset_registry.create(
        project.id, AssetKind.SCENE, "Hallway plate", canonical_metadata={"location_id": hallway_id}
    )
    version = container.asset_registry.add_version(scene_asset.id, primary_media_asset_id=plate)
    container.asset_registry.promote(scene_asset.id, version.id, reason="approved plate")
    return lin_id, lin_master, plate


def _narrative_material(container, project, third_shot_id: str, first_shot_id: str) -> None:  # type: ignore[no-untyped-def]
    ledger = NarrativeLedgerService(container.database)
    dependencies = ShotDependencyService(container.database)
    ledger.establish_fact(project.id, fact_key="phone_is_bugged", summary=FACT_SUMMARY, episode=1)
    ledger.open_obligation(project.id, obligation_key="who_bugged_it", promise=PROMISE, episode=1)
    dependencies.declare(
        project.id,
        target_shot_id=third_shot_id,
        dependency_type=ShotDependencyType.FORESHADOWING.value,
        source_shot_id=first_shot_id,
        summary=DEP_SUMMARY,
    )


def test_series_fact_reaches_every_prompt_surface_exactly_once(container, project):  # type: ignore[no-untyped-def]
    """Invariant 2: SERIES_FACT is compiled into the model prompt, not just metadata."""

    _, result = _compile(container, project)
    first, _, third = result.shot_ids
    _narrative_material(container, project, third, first)

    compilation = container.prompts.compile(third)
    positive = compilation.output.positive_prompt or ""
    fact_line = f"known_fact[AUDIENCE]: {FACT_SUMMARY}"
    assert positive.count(fact_line) == 1
    assert f"open_obligation: {PROMISE}" in positive
    assert DEP_SUMMARY in positive
    # The structured entries still travel as assertions metadata, unchanged.
    assert any(FACT_SUMMARY in item for item in compilation.output.continuity_assertions)

    # The legacy generation prompt is the same neutral prompt, persisted.
    with container.database.session() as session:
        shot = session.get(Shot, third)
        assert fact_line in shot.compiled_prompt

    # The visual/autopilot path: the adapter prompt that actually reaches a
    # provider carries the fact through the spec's continuity line.
    prepared = container.visual_runtime.prepare_autopilot(
        third,
        idempotency_key="series-fact-prompt",
        allowed_providers=["google_flow"],
    )
    assert fact_line in prepared.model_request.prompt
    assert fact_line in prepared.request.prompt
    assert prepared.model_request.prompt.count(fact_line) == 1


def test_planner_anchors_control_the_generation_request(container, project):  # type: ignore[no-untyped-def]
    """Invariants 3 and 4: the plan's anchors shape the request; the gate ran."""

    _, result = _compile(container, project)
    first, _, third = result.shot_ids
    _narrative_material(container, project, third, first)
    lin_id, lin_master, hallway_plate = _full_canon(container, project)

    # Unrelated canon that used to leak into every re-anchor request: a second
    # scene plate and a bound character the frame does not carry.
    kitchen_plate = _media(container, project.id, "kitchen-plate", "c")
    kitchen_asset = container.asset_registry.create(project.id, AssetKind.SCENE, "Kitchen plate")
    kitchen_version = container.asset_registry.add_version(
        kitchen_asset.id, primary_media_asset_id=kitchen_plate
    )
    container.asset_registry.promote(kitchen_asset.id, kitchen_version.id, reason="approved")
    zhao_master = _media(container, project.id, "zhao-master", "d")
    with container.database.session() as session:
        zhao = Character(project_id=project.id, name="ZhaoKai", status="DISCOVERED")
        session.add(zhao)
        session.flush()
        zhao_id = zhao.id

    candidate, replayed = container.candidates.create_candidate(
        third,
        idempotency_key="planner-anchors-govern",
        character_bindings=[
            {"character_id": lin_id, "canonical_assets": [lin_master]},
            {"character_id": zhao_id, "canonical_assets": [zhao_master]},
        ],
        enforce_entitlements=False,
    )
    assert replayed is False

    with container.database.session() as session:
        job = session.scalar(select(GenerationJob).where(GenerationJob.shot_id == third))
        request = dict(job.request_json)
        metadata = dict(request.get("metadata") or {})
        shot = session.get(Shot, third)
        plan_record = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.shot_id == third,
                DecisionRecord.decision_type == "FRAME_ANCHOR_PLAN",
            )
        )

    # The gate ran without anyone calling plan-frame-anchors (invariant 4).
    assert plan_record is not None
    assert shot.continuity_mode == ContinuityMode.RE_ANCHOR.value

    # The plan's anchors govern the reference set (invariant 3): the anchor
    # subject's master and the planner's scene plate are in; the unrelated
    # character and the unrelated scene plate are out.
    references = list(request.get("reference_asset_ids") or [])
    assert lin_master in references
    assert hallway_plate in references
    assert zhao_master not in references
    assert kitchen_plate not in references

    # requires_keyframe_generation is consumed, not decorative: the plan
    # travels on the request and the candidate's generation plan, and the
    # inherited start frame is gone because the frame is reconstructed.
    anchor = metadata.get("frame_anchor") or {}
    assert anchor.get("requires_keyframe_generation") is True
    assert [s["character_id"] for s in anchor.get("anchor_subjects", [])] == [lin_id]
    generation_plan = candidate.metadata_json["generation_plan"]
    assert generation_plan["requires_keyframe_generation"] is True
    assert generation_plan["frame_anchor"]["scene_asset_id"]
    assert request.get("start_frame_asset_id") is None


class _InertVideoProvider(GenerationProvider):
    """A configured transport that accepts submissions and does nothing else."""

    name = "seedance"

    async def generate_image(  # type: ignore[override]
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        return ProviderSubmission("inert-image")

    async def generate_video(  # type: ignore[override]
        self, request: dict[str, Any], *, account_id: str, worker_id: str
    ) -> ProviderSubmission:
        return ProviderSubmission("inert-video")

    async def upload_asset(self, asset: dict[str, Any], *, account_id: str, worker_id: str) -> str:  # type: ignore[override]
        return "inert-media"

    async def validate_asset(self, provider_media_id: str, *, account_id: str, worker_id: str) -> bool:  # type: ignore[override]
        return True

    async def get_job(  # type: ignore[no-untyped-def,override]
        self, provider_job_id: str, *, account_id: str, worker_id: str,
        generation_type: str, poll_identity=None,
    ):
        return ProviderJob(provider_job_id, "QUEUED")

    async def cancel_job(self, provider_job_id: str, *, account_id: str, worker_id: str) -> bool:  # type: ignore[override]
        return False

    async def get_credits(self, *, account_id: str, worker_id: str):  # type: ignore[no-untyped-def,override]
        return None

    async def health(self):  # type: ignore[no-untyped-def,override]
        return None


def test_retry_preserves_retrieval_context_and_anchors(container, project, monkeypatch):  # type: ignore[no-untyped-def]
    """Invariants 1 and 5: fallback switches the model, never the context."""

    _, result = _compile(container, project)
    first, _, third = result.shot_ids
    _narrative_material(container, project, third, first)
    lin_id, lin_master, hallway_plate = _full_canon(container, project)
    zhao_master = _media(container, project.id, "zhao-master", "e")

    container.candidates.create_candidate(
        third,
        idempotency_key="retry-preserves-context",
        character_bindings=[{"character_id": lin_id, "canonical_assets": [lin_master]}],
        enforce_entitlements=False,
    )
    with container.database.session() as session:
        job = session.scalar(select(GenerationJob).where(GenerationJob.shot_id == third))
        job_id = job.id
        request = dict(job.request_json)
        original_provider = job.provider
        original_model = job.model
    metadata = dict(request.get("metadata") or {})
    original_context = metadata["retrieval_context"]
    assert "EXPLICIT_DEPENDENCY[" in original_context["assembled_text"]
    assert "OPEN_OBLIGATION[" in original_context["assembled_text"]

    switch = next(
        profile
        for profile in container.video_router.registry.all()
        if profile.modality == "video"
        and profile.provider == "seedance"
        and profile.adapter in {"kling", "veo", "seedance", "grok", "wan"}
        and str(profile.version).strip()
    )
    assert switch.provider != original_provider
    # The switch target needs a configured transport and a registered route;
    # the fixture transport accepts the retry submission and does nothing.
    monkeypatch.setitem(container.providers._providers, "seedance", _InertVideoProvider())
    container.providers.register_model("seedance", switch.model_id, "video", available=True)
    plan = RetryPlan(
        action=EvaluationDecision.SWITCH_MODEL,
        attempt_number=1,
        terminal=False,
        next_provider=switch.provider,
        next_model=switch.model_id,
        inject_stronger_references=True,
        reasons=["identity"],
    )
    retry_job = container.visual_runtime._execute_retry(
        job_id, request, metadata, metadata["canonical_shot_spec"], plan
    )

    with container.database.session() as session:
        retry = session.get(GenerationJob, retry_job.id)
        retry_request = dict(retry.request_json)
    retry_metadata = dict(retry_request.get("metadata") or {})

    # The model moved; the retrieval outcome did not (invariants 1 and 5).
    assert retry_request["provider"] == switch.provider
    assert retry_request["model"] != original_model or switch.provider != original_provider
    assert retry_metadata["retrieval_context"]["assembled_text"] == (
        original_context["assembled_text"]
    )
    assert retry_metadata["frame_anchor"] == metadata["frame_anchor"]

    # The recompiled prompt still carries the forced retrieval context and the
    # series fact — fallback did not shed what stage one forced in.
    assert "EXPLICIT_DEPENDENCY[" in retry_request["prompt"]
    assert DEP_SUMMARY in retry_request["prompt"]
    assert PROMISE in retry_request["prompt"]
    assert f"known_fact[AUDIENCE]: {FACT_SUMMARY}" in retry_request["prompt"]

    # Strengthening reinforces the plan's anchors only — never assets outside
    # the anchor set (invariant 5).
    retry_references = list(retry_request.get("reference_asset_ids") or [])
    assert lin_master in retry_references
    assert hallway_plate in retry_references
    assert zhao_master not in retry_references


def test_manual_shot_generation_runs_the_planner_automatically(container, project):  # type: ignore[no-untyped-def]
    """Invariant 4: a hand-created shot cannot bypass the planner gate."""

    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Manual", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Studio")
        session.add(scene)
        session.flush()
        input_state = TimelineState(
            project_id=project.id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_INPUT",
            state_json={"characters": {"Mina": {"position": "center"}}},
        )
        output_state = TimelineState(
            project_id=project.id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_OUTPUT",
            state_json={"characters": {"Mina": {"position": "left"}}},
        )
        session.add_all([input_state, output_state])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=1,
            prompt="Mina raises one hand.",
            user_prompt="Mina raises one hand.",
            input_state_id=input_state.id,
            output_state_id=output_state.id,
            status=ShotStatus.PLANNED.value,
            # A manually saved mode the planner never decided: the gate must
            # not trust it.
            continuity_mode=ContinuityMode.HARD_CONTINUITY.value,
        )
        session.add(shot)
        session.flush()
        input_state.shot_id = shot.id
        output_state.shot_id = shot.id
        shot_id = shot.id

    container.candidates.create_candidate(
        shot_id,
        idempotency_key="manual-shot-gate",
        enforce_entitlements=False,
    )
    with container.database.session() as session:
        shot = session.get(Shot, shot_id)
        records = list(
            session.scalars(
                select(DecisionRecord).where(
                    DecisionRecord.shot_id == shot_id,
                    DecisionRecord.decision_type == "FRAME_ANCHOR_PLAN",
                )
            )
        )
    # The planner overrode the hand-saved mode with first-shot semantics
    # (no predecessor, no canon → fresh start), and recorded its decision.
    assert len(records) == 1
    assert "FIRST_SHOT" in records[0].reason_codes
    assert shot.continuity_mode == ContinuityMode.NONE.value

    # A replay of the same request reuses the still-current plan: the gate
    # runs, the decision does not multiply.
    _, replayed = container.candidates.create_candidate(
        shot_id,
        idempotency_key="manual-shot-gate",
        enforce_entitlements=False,
    )
    assert replayed is True
    with container.database.session() as session:
        count = len(
            list(
                session.scalars(
                    select(DecisionRecord.id).where(
                        DecisionRecord.shot_id == shot_id,
                        DecisionRecord.decision_type == "FRAME_ANCHOR_PLAN",
                    )
                )
            )
        )
    assert count == 1


def test_retry_rebuilds_stage_one_context_for_jobs_planned_before_it_was_stored(  # type: ignore[no-untyped-def]
    container, project, monkeypatch
):
    """Invariant 1 for old jobs: stage one is deterministic, so it is rebuilt."""

    _, result = _compile(container, project)
    first, _, third = result.shot_ids
    _narrative_material(container, project, third, first)
    lin_id, lin_master, _hallway_plate = _full_canon(container, project)

    container.candidates.create_candidate(
        third,
        idempotency_key="legacy-retry-rebuild",
        character_bindings=[{"character_id": lin_id, "canonical_assets": [lin_master]}],
        enforce_entitlements=False,
    )
    with container.database.session() as session:
        job = session.scalar(select(GenerationJob).where(GenerationJob.shot_id == third))
        job_id = job.id
        request = dict(job.request_json)
    metadata = dict(request.get("metadata") or {})
    # Simulate a job planned before retrieval context was persisted.
    metadata.pop("retrieval_context", None)

    switch = next(
        profile
        for profile in container.video_router.registry.all()
        if profile.modality == "video"
        and profile.provider == "seedance"
        and profile.adapter in {"kling", "veo", "seedance", "grok", "wan"}
        and str(profile.version).strip()
    )
    monkeypatch.setitem(container.providers._providers, "seedance", _InertVideoProvider())
    container.providers.register_model("seedance", switch.model_id, "video", available=True)
    plan = RetryPlan(
        action=EvaluationDecision.SWITCH_MODEL,
        attempt_number=1,
        terminal=False,
        next_provider=switch.provider,
        next_model=switch.model_id,
        reasons=["identity"],
    )
    retry_job = container.visual_runtime._execute_retry(
        job_id, request, metadata, metadata["canonical_shot_spec"], plan
    )

    with container.database.session() as session:
        retry_request = dict(session.get(GenerationJob, retry_job.id).request_json)
    # The forced material was re-derived, not lost: the recompiled fallback
    # prompt carries the dependency, the obligation, and the series fact.
    assert "EXPLICIT_DEPENDENCY[" in retry_request["prompt"]
    assert DEP_SUMMARY in retry_request["prompt"]
    assert PROMISE in retry_request["prompt"]
    assert f"known_fact[AUDIENCE]: {FACT_SUMMARY}" in retry_request["prompt"]


def test_anchor_subjects_are_bound_automatically_when_the_caller_supplies_none(  # type: ignore[no-untyped-def]
    container, project
):
    """Automatic role selection: the plan's subjects become bound roles."""

    _, result = _compile(container, project)
    first, _, third = result.shot_ids
    _narrative_material(container, project, third, first)
    lin_id, lin_master, hallway_plate = _full_canon(container, project)

    container.candidates.create_candidate(
        third,
        idempotency_key="auto-role-selection",
        enforce_entitlements=False,
    )
    with container.database.session() as session:
        job = session.scalar(select(GenerationJob).where(GenerationJob.shot_id == third))
        request = dict(job.request_json)
        metadata = dict(request.get("metadata") or {})

    # The plan's subject was bound without the caller naming anyone: their
    # identity reached the compiled spec, and their master anchors the request.
    subjects = metadata["canonical_shot_spec"]["subjects"]
    lin_subjects = [item for item in subjects if item.get("asset_id") == lin_id]
    assert lin_subjects and lin_subjects[0].get("asset_version_id")
    references = list(request.get("reference_asset_ids") or [])
    assert lin_master in references
    assert hallway_plate in references


def test_an_unexecutable_plan_enters_review_on_record(container, project, monkeypatch):  # type: ignore[no-untyped-def]
    """The plan failure mechanism: refusal is a review state, not a bare error."""

    import pytest
    from continuity_core import FrameAnchorPlan, FrameAnchorPlanUnresolved
    from continuity_core.frame_anchor import AnchorSubject

    _, result = _compile(container, project)
    third = result.shot_ids[2]

    dangling = FrameAnchorPlan(
        target_shot_id=third,
        source_shot_id=result.shot_ids[1],
        strategy="RECONSTRUCT_FIRST_FRAME",
        continuity_mode=ContinuityMode.RE_ANCHOR.value,
        transition_type="SCENE_CUT",
        risk_score=1.0,
        reasons=("SCENE_CHANGE",),
        anchor_subjects=(
            AnchorSubject(
                character_id="ghost",
                name="Ghost",
                identity_version_id=None,
                master_asset_id=None,
            ),
        ),
        scene_asset_id="asset-that-no-longer-exists",
        requires_keyframe_generation=True,
    )
    monkeypatch.setattr(
        container.candidates.frame_anchors, "ensure_plan", lambda shot_id: dangling
    )
    # The mode the real gate would have applied when this plan was made; the
    # canon behind it has since vanished.
    with container.database.session() as session:
        session.get(Shot, third).continuity_mode = ContinuityMode.RE_ANCHOR.value

    with pytest.raises(FrameAnchorPlanUnresolved, match="review required"):
        container.candidates.create_candidate(
            third,
            idempotency_key="plan-failure-review",
            enforce_entitlements=False,
        )
    with container.database.session() as session:
        shot = session.get(Shot, third)
        record = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.shot_id == third,
                DecisionRecord.decision_type == "FRAME_ANCHOR_PLAN_RESOLUTION",
            )
        )
        no_job = session.scalar(select(GenerationJob.id).where(GenerationJob.shot_id == third))
    assert shot.status == ShotStatus.USER_REVIEW_REQUIRED.value
    assert record is not None
    assert record.selected_action == "REVIEW_REQUIRED"
    assert "ANCHOR_REFERENCES_UNRESOLVED" in record.reason_codes
    assert no_job is None


def test_manual_continuity_decision_is_a_plan_the_gate_reuses(container, project):  # type: ignore[no-untyped-def]
    """An operator's risk override survives generation instead of being re-derived away."""

    from continuity_core import ContinuityRiskVector

    _, result = _compile(container, project)
    second = result.shot_ids[1]
    _full_canon(container, project)

    # The derived risk for this pair says continuous same-scene (inherit); the
    # operator knows it is a reverse shot the structured states cannot see.
    outcome = container.orchestrator.plan_continuity(
        second, project.id, ContinuityRiskVector(reverse_shot=True, camera_axis_delta=0.9)
    )
    assert outcome.detail["mode"] == ContinuityMode.RE_ANCHOR.value

    plan = container.frame_anchors.ensure_plan(second)
    assert plan is not None
    assert plan.continuity_mode == ContinuityMode.RE_ANCHOR.value
    assert "MANUAL_RISK_OVERRIDE" in plan.reasons
    with container.database.session() as session:
        shot = session.get(Shot, second)
        assert shot.continuity_mode == ContinuityMode.RE_ANCHOR.value


def test_a_planner_failure_during_preflight_enters_review_on_record(  # type: ignore[no-untyped-def]
    container, project, monkeypatch
):
    """Loop closure: a planner that cannot plan is a review state, not a 500."""

    import pytest
    from continuity_core import FrameAnchorPlanUnresolved

    _, result = _compile(container, project)
    third = result.shot_ids[2]

    def _explode(shot_id: str):  # type: ignore[no-untyped-def]
        raise ValueError("planner exploded")

    monkeypatch.setattr(container.candidates.frame_anchors, "plan_pair", _explode)
    with pytest.raises(FrameAnchorPlanUnresolved, match="review required"):
        container.candidates.create_candidate(
            third,
            idempotency_key="planner-failure-preflight",
            enforce_entitlements=False,
        )
    with container.database.session() as session:
        shot = session.get(Shot, third)
        record = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.shot_id == third,
                DecisionRecord.decision_type == "FRAME_ANCHOR_PLAN_RESOLUTION",
            )
        )
        no_job = session.scalar(select(GenerationJob.id).where(GenerationJob.shot_id == third))
    assert shot.status == ShotStatus.USER_REVIEW_REQUIRED.value
    assert record is not None
    assert any(
        code.startswith("FRAME_ANCHOR_PLANNING_FAILED") for code in record.reason_codes
    )
    assert no_job is None


def test_non_anchor_character_registry_material_is_filtered_from_the_request(  # type: ignore[no-untyped-def]
    container, project
):
    """Loop closure: canonical CHARACTER assets follow the anchor set too."""

    _, result = _compile(container, project)
    first, _, third = result.shot_ids
    _narrative_material(container, project, third, first)
    lin_id, lin_master, hallway_plate = _full_canon(container, project)

    # Registry-level character material: one asset recorded for the anchor
    # subject, one for a character the frame does not carry.
    lin_plate = _media(container, project.id, "lin-registry-plate", "f")
    lin_asset = container.asset_registry.create(
        project.id, AssetKind.CHARACTER, "LinJin sheet", canonical_metadata={"character_id": lin_id}
    )
    lin_version = container.asset_registry.add_version(
        lin_asset.id, primary_media_asset_id=lin_plate
    )
    container.asset_registry.promote(lin_asset.id, lin_version.id, reason="approved")
    with container.database.session() as session:
        zhao = Character(project_id=project.id, name="ZhaoKai", status="DISCOVERED")
        session.add(zhao)
        session.flush()
        zhao_id = zhao.id
    zhao_plate = _media(container, project.id, "zhao-registry-plate", "9")
    zhao_asset = container.asset_registry.create(
        project.id,
        AssetKind.CHARACTER,
        "ZhaoKai sheet",
        canonical_metadata={"character_id": zhao_id},
    )
    zhao_version = container.asset_registry.add_version(
        zhao_asset.id, primary_media_asset_id=zhao_plate
    )
    container.asset_registry.promote(zhao_asset.id, zhao_version.id, reason="approved")

    container.candidates.create_candidate(
        third,
        idempotency_key="character-material-filter",
        enforce_entitlements=False,
    )
    with container.database.session() as session:
        job = session.scalar(select(GenerationJob).where(GenerationJob.shot_id == third))
        references = list(dict(job.request_json).get("reference_asset_ids") or [])
    assert lin_master in references
    assert lin_plate in references
    assert hallway_plate in references
    assert zhao_plate not in references


def test_an_old_task_retry_with_unresolvable_dependencies_forces_review(  # type: ignore[no-untyped-def]
    container, project
):
    """Loop closure: a pre-storage job whose owed material broke cannot retry."""

    import pytest
    from narrative_ledger_core import ShotDependencyUnresolved

    _, result = _compile(container, project)
    first, second, third = result.shot_ids
    _narrative_material(container, project, third, first)
    ledger = NarrativeLedgerService(container.database)
    dependencies = ShotDependencyService(container.database)
    ledger.open_obligation(
        project.id, obligation_key="paid_elsewhere", promise="A promise.", episode=1
    )
    dependencies.declare(
        project.id,
        target_shot_id=third,
        dependency_type=ShotDependencyType.OBLIGATION_FULFILLMENT.value,
        obligation_key="paid_elsewhere",
    )
    lin_id, lin_master, _hallway_plate = _full_canon(container, project)

    container.candidates.create_candidate(
        third,
        idempotency_key="old-task-retry-review",
        character_bindings=[{"character_id": lin_id, "canonical_assets": [lin_master]}],
        enforce_entitlements=False,
    )
    with container.database.session() as session:
        job = session.scalar(select(GenerationJob).where(GenerationJob.shot_id == third))
        job_id = job.id
        request = dict(job.request_json)
        original_provider = job.provider
    metadata = dict(request.get("metadata") or {})
    # An old task: no stored retrieval context — and its owed material has
    # since broken: the obligation was settled by a different shot.
    metadata.pop("retrieval_context", None)
    ledger.settle_obligation(project.id, obligation_key="paid_elsewhere", episode=1, shot_id=second)

    switch = next(
        profile
        for profile in container.video_router.registry.all()
        if profile.modality == "video"
        and profile.provider == "seedance"
        and profile.adapter in {"kling", "veo", "seedance", "grok", "wan"}
        and str(profile.version).strip()
    )
    assert switch.provider != original_provider
    plan = RetryPlan(
        action=EvaluationDecision.SWITCH_MODEL,
        attempt_number=1,
        terminal=False,
        next_provider=switch.provider,
        next_model=switch.model_id,
        reasons=["identity"],
    )
    with pytest.raises(ShotDependencyUnresolved, match="review required"):
        container.visual_runtime._execute_retry(
            job_id, request, metadata, metadata["canonical_shot_spec"], plan
        )
    with container.database.session() as session:
        shot = session.get(Shot, third)
        record = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.shot_id == third,
                DecisionRecord.decision_type == "SHOT_DEPENDENCY_RESOLUTION",
            )
        )
    assert shot.status == ShotStatus.USER_REVIEW_REQUIRED.value
    assert record is not None
    assert record.input_features.get("stage") == "RETRY"
    assert any(
        code.startswith("DEPENDENCY_OBLIGATION_ALREADY_SETTLED") for code in record.reason_codes
    )
