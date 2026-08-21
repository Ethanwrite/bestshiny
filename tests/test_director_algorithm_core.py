from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from continuity_core import ContinuityRiskVector
from generation_policy_core import (
    AvailableGenerationAssets,
    GenerationPolicyEngine,
    GenerationPolicyInputError,
)
from narrative_core import AuthoritativeTimelineStateEngine, TimelinePropagationError
from production_domain.models import (
    CandidateStatus,
    Character,
    ContinuityMode,
    CostRecord,
    DecisionRecord,
    Episode,
    GenerationCandidate,
    GenerationJob,
    GenerationPolicy,
    JobStatus,
    Location,
    MediaAsset,
    NarrativeEvent,
    QADecision,
    QAResult,
    Shot,
    ShotStateSnapshot,
    ShotStatus,
    TimelineState,
    utcnow,
)
from provider_sdk import GenerationProvider, ProviderHealth, ProviderJob, ProviderSubmission
from qa_core import RuleBasedDynamicIdentityQA, analyze_identity_drift
from sqlalchemy import func, select


def _compile(container, project, script: str, *, episode_number: int = 1):
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title=f"Episode {episode_number}",
            episode_number=episode_number,
            script_source=script,
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    return episode_id, container.narrative.compile_episode(episode_id)


class _CompletedVideoFixtureProvider(GenerationProvider):
    """Deterministic provider boundary for the offline three-shot acceptance path."""

    name = "google_flow"

    def __init__(self) -> None:
        self.submitted_requests: list[dict[str, Any]] = []
        self.uploaded_asset_ids: list[str] = []

    async def generate_image(
        self,
        request: dict[str, Any],
        *,
        account_id: str,
        worker_id: str,
    ) -> ProviderSubmission:
        return await self.generate_video(request, account_id=account_id, worker_id=worker_id)

    async def generate_video(
        self,
        request: dict[str, Any],
        *,
        account_id: str,
        worker_id: str,
    ) -> ProviderSubmission:
        del account_id, worker_id
        self.submitted_requests.append(deepcopy(request))
        return ProviderSubmission(f"fixture-provider-job-{len(self.submitted_requests)}")

    async def upload_asset(
        self,
        asset: dict[str, Any],
        *,
        account_id: str,
        worker_id: str,
    ) -> str:
        del account_id, worker_id
        asset_id = str(asset["asset_id"])
        self.uploaded_asset_ids.append(asset_id)
        return f"fixture-provider-media-{asset_id}"

    async def validate_asset(
        self,
        provider_media_id: str,
        *,
        account_id: str,
        worker_id: str,
    ) -> bool:
        del provider_media_id, account_id, worker_id
        return True

    async def get_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
        generation_type: str,
        poll_identity=None,  # type: ignore[no-untyped-def]
    ) -> ProviderJob:
        del account_id, worker_id, generation_type, poll_identity
        return ProviderJob(
            provider_job_id,
            "COMPLETED",
            progress=1.0,
            output_url=f"https://fixture.invalid/{provider_job_id}.mp4",
            output_mime_type="video/mp4",
        )

    async def cancel_job(
        self,
        provider_job_id: str,
        *,
        account_id: str,
        worker_id: str,
    ) -> bool:
        del provider_job_id, account_id, worker_id
        return True

    async def get_credits(self, *, account_id: str, worker_id: str) -> int:
        del account_id, worker_id
        return 100

    async def health(self) -> ProviderHealth:
        return ProviderHealth(True, "offline fixture ready")


def _passing_qa_evidence() -> dict[str, Any]:
    return {
        "identity_samples": [{"face_similarity": 0.92}] * 6,
        "character_score": 0.92,
        "scene_score": 0.92,
        "composition_score": 0.92,
        "action_score": 0.92,
        "camera_score": 0.92,
        "lighting_score": 0.92,
        "narrative_score": 0.92,
    }


def test_narrative_event_compilation(container, project):
    episode_id, result = _compile(
        container,
        project,
        """INT. 办公室 - NIGHT
赵凯举起手机给林烬看。
""",
    )
    assert len(result.shot_ids) == 1
    with container.database.session() as session:
        episode = session.get(Episode, episode_id)
        event = session.get(NarrativeEvent, result.event_ids[0])
        actor = session.get(Character, event.actor_id)
        target = session.get(Character, event.target_id)
        graph = episode.script_structured["entity_graph"]
        timeline_event = episode.script_structured["event_timeline"][0]
        assert actor.name == "赵凯"
        assert target.name == "林烬"
        assert event.action == "raise"
        assert event.object_id
        assert {node["type"] for node in graph["nodes"]}.issuperset(
            {"CHARACTER", "LOCATION", "PROP", "ACTION", "NARRATIVE_FACT", "RELATIONSHIP"}
        )
        assert timeline_event["pre_state"]["held_props"] == {}
        assert timeline_event["post_state"]["held_props"][event.actor_id]["right_hand"] == event.object_id
        assert timeline_event["primary_action"] == "raise"
        assert episode.script_structured["authoritative_state_source"] == "SQL_TIMELINE_STATE"


def test_one_primary_action_rule_splits_independent_actions(container, project):
    _, result = _compile(
        container,
        project,
        """INT. ROOM - NIGHT
LinJin opens the door and then LinJin enters the room.
""",
    )
    assert len(result.shot_ids) == 2
    with container.database.session() as session:
        events = list(
            session.scalars(
                select(NarrativeEvent)
                .where(NarrativeEvent.id.in_(result.event_ids))
                .order_by(NarrativeEvent.sequence)
            )
        )
        assert [event.action for event in events] == ["open", "enter"]
        assert all("and then" not in event.source_text.casefold() for event in events)


def test_entity_graph_ids_are_stable_across_recompile(container, project):
    episode_id, _ = _compile(
        container,
        project,
        """INT. ROOM - NIGHT
LinJin raises the phone.
""",
    )
    with container.database.session() as session:
        episode = session.get(Episode, episode_id)
        first_nodes = {
            (node["type"], node["name"]): node["id"]
            for node in episode.script_structured["entity_graph"]["nodes"]
            if node["type"] in {"CHARACTER", "LOCATION", "PROP", "ACTION"}
        }
        episode.script_source += "\nLinJin turns toward ZhaoKai."
    container.narrative.compile_episode(episode_id)
    with container.database.session() as session:
        episode = session.get(Episode, episode_id)
        second_nodes = {
            (node["type"], node["name"]): node["id"]
            for node in episode.script_structured["entity_graph"]["nodes"]
        }
        for key, entity_id in first_nodes.items():
            assert second_nodes[key] == entity_id


def test_timeline_state_propagation(container, project):
    _, result = _compile(
        container,
        project,
        """INT. ROOM - NIGHT
LinJin raises the phone.
LinJin turns toward ZhaoKai.
""",
    )
    first_id, second_id = result.shot_ids
    expected = {
        "scene": {"location": "ROOM", "time": "night"},
        "characters": {"lin": {"position": "left", "orientation": "right"}},
        "held_props": {"lin": {"right_hand": "phone"}},
        "camera": {"axis": "A", "angle": "eye_level", "shot_size": "medium"},
        "narrative_facts": [{"fact": "phone is raised"}],
    }
    with container.database.session() as session:
        first = session.get(Shot, first_id)
        first.status = ShotStatus.COMMITTED.value
        first_output = session.get(TimelineState, first.output_state_id)
        first_output.state_json = deepcopy(expected)
    result = AuthoritativeTimelineStateEngine(container.database).propagate_shot(first_id)
    assert result.propagated is True
    assert result.reason_code == "CONTINUOUS_TIMELINE"
    with container.database.session() as session:
        first = session.get(Shot, first_id)
        second = session.get(Shot, second_id)
        first_output = session.get(TimelineState, first.output_state_id)
        second_input = session.get(TimelineState, second.input_state_id)
        assert second_input.state_json == expected
        assert second_input.previous_state_id == first_output.id


def test_propagated_input_rebases_next_event_output(container, project):
    _, result = _compile(
        container,
        project,
        """INT. ROOM - NIGHT
LinJin raises the phone.
LinJin turns toward ZhaoKai.
""",
    )
    first_id, second_id = result.shot_ids
    with container.database.session() as session:
        first = session.get(Shot, first_id)
        second = session.get(Shot, second_id)
        first_event = session.get(NarrativeEvent, result.event_ids[0])
        second_event = session.get(NarrativeEvent, result.event_ids[1])
        first.status = ShotStatus.COMMITTED.value
        first_output = session.get(TimelineState, first.output_state_id)
        second_output = session.get(TimelineState, second.output_state_id)
        authoritative = deepcopy(first_output.state_json)
        authoritative["held_props"][first_event.actor_id] = {
            "left_hand": first_event.object_id,
        }
        authoritative["camera"] = {
            "axis": "B",
            "angle": "low",
            "shot_size": "close_up",
        }
        runtime_fact = {
            "event_id": "trusted-runtime-observation",
            "sequence": 1.5,
            "fact": "phone moved to the left hand",
        }
        authoritative["narrative_facts"].append(runtime_fact)
        first_output.state_json = authoritative
        planned_event_fact = deepcopy(second_output.state_json["narrative_facts"][-1])
        target_output_id = second_output.id
        second_event_id = second_event.id

    propagation = AuthoritativeTimelineStateEngine(container.database).propagate_shot(first_id)

    assert propagation.propagated is True
    assert propagation.output_rebased is True
    assert propagation.target_output_state_id == target_output_id
    with container.database.session() as session:
        second = session.get(Shot, second_id)
        second_input = session.get(TimelineState, second.input_state_id)
        second_output = session.get(TimelineState, second.output_state_id)
        assert second_input.state_json == authoritative
        assert second_output.state_json["held_props"] == authoritative["held_props"]
        assert second_output.state_json["camera"] == authoritative["camera"]
        assert second_output.state_json["primary_action"] == "turn"
        assert second_output.state_json["pose"][second_event.actor_id] == "turn"
        assert runtime_fact in second_output.state_json["narrative_facts"]
        assert second_output.state_json["narrative_facts"].count(planned_event_fact) == 1
        assert second_output.state_json["narrative_facts"][-1]["event_id"] == second_event_id
        decision = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.shot_id == first_id,
                DecisionRecord.decision_type == "TIMELINE_PROPAGATION",
            )
        )
        assert decision.input_features["output_rebased"] is True
        assert decision.input_features["target_output_state_id"] == target_output_id
        assert decision.model_version == "sql-timeline-propagation-v3"
        assert decision.policy_version == "timeline-v3"


def test_timeline_state_propagation_rejects_uncommitted_source(container, project):
    _, result = _compile(
        container,
        project,
        """INT. ROOM - NIGHT
LinJin raises the phone.
LinJin turns toward ZhaoKai.
""",
    )

    with pytest.raises(TimelinePropagationError, match="only a committed shot"):
        AuthoritativeTimelineStateEngine(container.database).propagate_shot(result.shot_ids[0])


@pytest.mark.parametrize(
    "target_status",
    [
        ShotStatus.QUEUED.value,
        ShotStatus.GENERATING.value,
        ShotStatus.VALIDATING.value,
        ShotStatus.REPAIRING.value,
        ShotStatus.REGENERATING.value,
        ShotStatus.USER_REVIEW_REQUIRED.value,
        ShotStatus.COMMITTED.value,
        ShotStatus.FAILED.value,
    ],
)
def test_timeline_state_propagation_rejects_active_or_terminal_target(
    container,
    project,
    target_status,
):
    _, result = _compile(
        container,
        project,
        """INT. ROOM - NIGHT
LinJin raises the phone.
LinJin turns toward ZhaoKai.
""",
    )
    first_id, second_id = result.shot_ids
    with container.database.session() as session:
        first = session.get(Shot, first_id)
        second = session.get(Shot, second_id)
        first.status = ShotStatus.COMMITTED.value
        second.status = target_status
        second_input = session.get(TimelineState, second.input_state_id)
        second_output = session.get(TimelineState, second.output_state_id)
        original_input = deepcopy(second_input.state_json)
        original_output = deepcopy(second_output.state_json)

    with pytest.raises(TimelinePropagationError, match=f"a {target_status} next shot"):
        AuthoritativeTimelineStateEngine(container.database).propagate_shot(first_id)

    with container.database.session() as session:
        second = session.get(Shot, second_id)
        assert session.get(TimelineState, second.input_state_id).state_json == original_input
        assert session.get(TimelineState, second.output_state_id).state_json == original_output


def test_timeline_state_propagation_preserves_scene_reset(container, project):
    _, result = _compile(
        container,
        project,
        """INT. ROOM - NIGHT
LinJin raises the phone.
EXT. STREET - DAY
LinJin walks toward ZhaoKai.
""",
    )
    first_id, second_id = result.shot_ids
    with container.database.session() as session:
        session.get(Shot, first_id).status = ShotStatus.COMMITTED.value
        second = session.get(Shot, second_id)
        second_input = session.get(TimelineState, second.input_state_id)
        reset_state = deepcopy(second_input.state_json)
    propagation = AuthoritativeTimelineStateEngine(container.database).propagate_shot(first_id)
    assert propagation.propagated is False
    assert propagation.reason_code == "SCENE_CHANGE"
    with container.database.session() as session:
        second = session.get(Shot, second_id)
        assert session.get(TimelineState, second.input_state_id).state_json == reset_state


def test_hard_continuity_decision(container, project):
    decision = container.continuity_decision.decide(
        ContinuityRiskVector(
            camera_angle_delta=0.04,
            camera_axis_delta=0.03,
            action_continuity=0.98,
            previous_frame_quality=0.94,
        ),
        project_id=project.id,
    )
    assert decision.mode == ContinuityMode.HARD_CONTINUITY.value
    assert decision.required_context == (
        "previous_end_frame",
        "character_binding",
        "current_action_prompt",
    )


def test_hybrid_decision(container, project):
    decision = container.continuity_decision.decide(
        ContinuityRiskVector(camera_axis_delta=0.4, blocking_delta=0.3),
        project_id=project.id,
    )
    assert decision.mode == ContinuityMode.HYBRID.value
    assert decision.use_previous_end_frame is True
    assert decision.require_new_keyframe is False


def test_hybrid_previous_end_frame_is_context_not_strong_start(
    container,
    project,
    register_bytes,
):
    _, result = _compile(
        container,
        project,
        """INT. ROOM - NIGHT
LinJin raises the phone.
LinJin turns toward ZhaoKai.
""",
    )
    first_id, second_id = result.shot_ids
    previous_end_frame = register_bytes(container, project.id, "END_FRAME", b"previous-end-frame")
    with container.database.session() as session:
        session.get(Shot, first_id).end_frame_asset_id = previous_end_frame.id
        # A prior HARD plan may have installed this as a strong first frame;
        # replanning as HYBRID must clear it rather than silently retaining it.
        session.get(Shot, second_id).start_frame_asset_id = previous_end_frame.id

    result = container.orchestrator.plan_continuity(
        second_id,
        project.id,
        ContinuityRiskVector(camera_axis_delta=0.4, blocking_delta=0.3),
    )

    assert result.detail["mode"] == ContinuityMode.HYBRID.value
    assert result.detail["use_previous_end_frame"] is True
    assert "previous_end_frame_context" in result.detail["required_context"]
    with container.database.session() as session:
        second = session.get(Shot, second_id)
        assert second.continuity_mode == ContinuityMode.HYBRID.value
        assert second.start_frame_asset_id is None


def test_reverse_shot_re_anchor(container, project):
    decision = container.continuity_decision.decide(
        ContinuityRiskVector(reverse_shot=True, camera_axis_delta=0.82),
        project_id=project.id,
    )
    assert decision.mode == ContinuityMode.RE_ANCHOR.value
    assert decision.require_new_keyframe is True
    assert "REVERSE_SHOT" in decision.reasons
    assert "canonical_character_references" in decision.required_context


def test_generation_policy(container, project):
    engine = GenerationPolicyEngine(container.database)
    hard = engine.decide(
        ContinuityMode.HARD_CONTINUITY.value,
        AvailableGenerationAssets(
            previous_end_frame_asset_id="end-frame",
            character_reference_asset_ids=("character-master",),
            character_binding=True,
        ),
        project_id=project.id,
    )
    assert hard.policy == GenerationPolicy.CONTINUE_I2V.value
    assert hard.use_previous_end_frame_as_start is True
    reverse = engine.decide(
        ContinuityMode.RE_ANCHOR.value,
        AvailableGenerationAssets(
            character_reference_asset_ids=("character-master",),
            scene_reference_asset_ids=("scene-master",),
        ),
        project_id=project.id,
    )
    assert reverse.policy == GenerationPolicy.REANCHOR_FULL.value
    assert reverse.require_new_keyframe is True
    with pytest.raises(GenerationPolicyInputError, match="scene_reference"):
        engine.decide(
            ContinuityMode.RE_ANCHOR.value,
            AvailableGenerationAssets(character_reference_asset_ids=("character-master",)),
        )


def test_hybrid_end_frame_is_soft_context_not_start_frame(container, project):
    assets = AvailableGenerationAssets(
        previous_end_frame_asset_id="previous-end-frame",
        character_reference_asset_ids=("character-master",),
    )

    decision = GenerationPolicyEngine(container.database).decide(
        ContinuityMode.HYBRID.value,
        assets,
        project_id=project.id,
    )

    assert decision.policy == GenerationPolicy.HYBRID_REFERENCE.value
    assert decision.use_previous_end_frame_as_start is False
    assert "previous_end_frame" in assets.available_inputs()
    assert "start_frame" not in assets.available_inputs()


def test_identity_drift():
    samples = [
        {
            "track_id": "character-a",
            "view": view,
            "face_similarity": face,
            "appearance_similarity": body,
            "costume_similarity": costume,
            "hair_similarity": hair,
        }
        for view, face, body, costume, hair in (
            ("front", 0.94, 0.92, 0.96, 0.95),
            ("three_quarter", 0.90, 0.91, 0.95, 0.94),
            ("profile", 0.82, 0.89, 0.94, 0.91),
            ("occluded", 0.69, 0.86, 0.93, 0.88),
            ("three_quarter", 0.86, 0.90, 0.95, 0.93),
            ("front", 0.92, 0.93, 0.96, 0.95),
        )
    ]
    metrics = analyze_identity_drift(samples)
    assert metrics.average_identity == metrics.average_similarity
    assert metrics.minimum_identity == 0.69
    assert metrics.identity_p10 == 0.69
    assert metrics.appearance_similarity == pytest.approx(0.9017, abs=0.0001)
    assert metrics.costume_similarity == pytest.approx(0.9483, abs=0.0001)
    assert metrics.hair_similarity == pytest.approx(0.9267, abs=0.0001)
    assert metrics.reacquisition_score > metrics.minimum_identity
    positions = RuleBasedDynamicIdentityQA().sample_positions(motion_spikes=(0.33, 0.67))
    assert positions == (0.0, 0.2, 0.33, 0.4, 0.6, 0.67, 0.8, 0.98)


def test_decision_log(container, project):
    container.continuity_decision.decide(
        ContinuityRiskVector(reverse_shot=True),
        project_id=project.id,
    )
    GenerationPolicyEngine(container.database).decide(
        ContinuityMode.RE_ANCHOR.value,
        AvailableGenerationAssets(
            character_reference_asset_ids=("character-master",),
            scene_reference_asset_ids=("scene-master",),
        ),
        project_id=project.id,
    )
    with container.database.session() as session:
        records = list(
            session.scalars(
                select(DecisionRecord)
                .where(DecisionRecord.project_id == project.id)
                .order_by(DecisionRecord.created_at)
            )
        )
        assert {record.decision_type for record in records} == {
            "CONTINUITY_DECISION",
            "GENERATION_POLICY_DECISION",
        }
        assert all(record.input_features for record in records)
        assert all(record.reason_codes for record in records)
        assert all(record.policy_version for record in records)


def test_cost_per_accepted_shot(container, project):
    with container.database.session() as session:
        session.add_all(
            [
                CostRecord(
                    project_id=project.id,
                    provider="fixture",
                    model="fixture-v1",
                    actual_cost=cost,
                    accepted=accepted,
                    wasted=not accepted,
                )
                for cost, accepted in ((1.0, False), (1.5, True), (0.5, False))
            ]
        )
    metrics = container.cost.provider_metrics("fixture")
    assert metrics["attempts"] == 3
    assert metrics["accepted"] == 1
    assert metrics["cost_per_accepted_shot"] == 3.0


def test_three_shot_continuity_e2e(container, project, register_bytes):
    _, compiled = _compile(
        container,
        project,
        """INT. CONTROL ROOM - NIGHT
LinJin raises the phone.
LinJin turns slightly toward ZhaoKai.
ZhaoKai looks toward LinJin.
""",
    )
    shot1_id, shot2_id, shot3_id = compiled.shot_ids
    character_master = register_bytes(container, project.id, "CHARACTER_MASTER", b"character-master")
    scene_master = register_bytes(container, project.id, "LOCATION_MASTER", b"scene-master")
    end_frame_1 = register_bytes(container, project.id, "END_FRAME", b"shot-one-end")
    end_frame_2 = register_bytes(container, project.id, "END_FRAME", b"shot-two-end")
    with container.database.session() as session:
        character = session.scalar(
            select(Character).where(Character.project_id == project.id, Character.name == "LinJin")
        )
        location = session.scalar(select(Location).where(Location.project_id == project.id))
        character_id = character.id
        location.canonical_asset_id = scene_master.id
        shot1 = session.get(Shot, shot1_id)
        shot1.status = ShotStatus.COMMITTED.value
        shot1.end_frame_asset_id = end_frame_1.id
        shot1_output = session.get(TimelineState, shot1.output_state_id)
        shot1_output.state_json = {
            **shot1_output.state_json,
            "committed": True,
            "end_frame_asset_id": end_frame_1.id,
        }
    identity = container.characters.confirm_identity(character_id, character_master.id)
    binding = container.characters.binding(character_id)

    propagated_1 = AuthoritativeTimelineStateEngine(container.database).propagate_shot(shot1_id)
    assert propagated_1.propagated is True
    continuity_2 = container.orchestrator.plan_continuity(
        shot2_id,
        project.id,
        ContinuityRiskVector(camera_axis_delta=0.04, action_continuity=0.97),
    )
    assert continuity_2.detail["mode"] == ContinuityMode.HARD_CONTINUITY.value
    generation_2 = container.orchestrator.plan_generation(
        shot2_id,
        project.id,
        AvailableGenerationAssets(
            previous_end_frame_asset_id=end_frame_1.id,
            character_reference_asset_ids=tuple(binding["canonical_assets"]),
            character_binding=True,
        ),
    )
    assert generation_2.detail["policy"] == GenerationPolicy.CONTINUE_I2V.value
    with container.database.session() as session:
        shot2 = session.get(Shot, shot2_id)
        assert shot2.start_frame_asset_id == end_frame_1.id
        shot2.status = ShotStatus.COMMITTED.value
        shot2.end_frame_asset_id = end_frame_2.id
        shot2_output = session.get(TimelineState, shot2.output_state_id)
        shot2_output.state_json = {
            **shot2_output.state_json,
            "committed": True,
            "end_frame_asset_id": end_frame_2.id,
        }
    assert AuthoritativeTimelineStateEngine(container.database).propagate_shot(shot2_id).propagated

    continuity_3 = container.orchestrator.plan_continuity(
        shot3_id,
        project.id,
        ContinuityRiskVector(reverse_shot=True, camera_axis_delta=0.9),
    )
    assert continuity_3.detail["mode"] == ContinuityMode.RE_ANCHOR.value
    generation_3 = container.orchestrator.plan_generation(
        shot3_id,
        project.id,
        AvailableGenerationAssets(
            previous_end_frame_asset_id=end_frame_2.id,
            character_reference_asset_ids=(identity.master_asset_id,),
            scene_reference_asset_ids=(scene_master.id,),
        ),
    )
    assert generation_3.detail["policy"] == GenerationPolicy.REANCHOR_FULL.value
    plan_3 = container.capability_resolver.resolve(
        GenerationPolicy.REANCHOR_FULL.value,
        "seedance",
        ["google_flow"],
        project_id=project.id,
        shot_id=shot3_id,
        available_inputs=AvailableGenerationAssets(
            character_reference_asset_ids=(identity.master_asset_id,),
            scene_reference_asset_ids=(scene_master.id,),
        ).available_inputs(),
    )
    assert plan_3.provider == "google_flow"
    assert set(plan_3.required_inputs).issuperset(
        {"character_reference", "scene_reference", "narrative_state", "current_camera_prompt"}
    )
    with container.database.session() as session:
        shot2 = session.get(Shot, shot2_id)
        shot3 = session.get(Shot, shot3_id)
        shot2_output = session.get(TimelineState, shot2.output_state_id)
        shot3_input = session.get(TimelineState, shot3.input_state_id)
        assert shot3_input.state_json == shot2_output.state_json
        assert shot3.continuity_mode == ContinuityMode.RE_ANCHOR.value
        assert shot3.generation_policy == GenerationPolicy.REANCHOR_FULL.value
        assert shot3.start_frame_asset_id is None
        assert (
            session.scalar(select(func.count(GenerationJob.id)).where(GenerationJob.project_id == project.id))
            == 0
        )


@pytest.mark.asyncio
async def test_three_shot_fixture_generation_qa_commit_e2e(
    container,
    project,
    account_worker,
    register_bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Exercise three complete offline generation lifecycles through the production services."""

    account_id, _ = account_worker
    container.flow_affinity.bind_existing(
        local_project_id=project.id,
        provider_account_id=account_id,
        provider_project_id="flow-project-test",
    )
    fixture_provider = _CompletedVideoFixtureProvider()
    monkeypatch.setitem(container.providers._providers, "google_flow", fixture_provider)
    container.providers.register_model("google_flow", "flow-veo-3.1", "video")

    fixture_video = tmp_path / "fixture-provider-output.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x240:d=1",
            "-pix_fmt",
            "yuv420p",
            str(fixture_video),
        ],
        check=True,
        capture_output=True,
    )

    async def download_fixture_output(
        project_id: str,
        asset_type: str,
        url: str,
        *,
        filename: str,
        provider: str,
        provider_media_id: str,
        shot_id: str | None = None,
        generation_candidate_id: str | None = None,
    ) -> MediaAsset:
        del url
        with fixture_video.open("rb") as stream:
            asset, _ = container.media.register(
                project_id,
                asset_type,
                stream,
                filename=filename,
                mime_type="video/mp4",
                shot_id=shot_id,
                generation_candidate_id=generation_candidate_id,
                metadata={"source": "offline-completed-provider-fixture"},
            )
        with container.database.session() as session:
            stored = session.get(MediaAsset, asset.id)
            assert stored is not None
            stored.provider = provider
            stored.provider_media_id = provider_media_id
            session.flush()
            return stored

    monkeypatch.setattr(container.media, "download_and_register", download_fixture_output)

    _, compiled = _compile(
        container,
        project,
        """INT. CONTROL ROOM - NIGHT
LinJin raises the phone.
LinJin turns slightly toward ZhaoKai.
ZhaoKai looks toward LinJin.
""",
    )
    shot1_id, shot2_id, shot3_id = compiled.shot_ids
    character_master = register_bytes(
        container,
        project.id,
        "CHARACTER_MASTER",
        b"canonical-character-master",
    )
    scene_master = register_bytes(
        container,
        project.id,
        "LOCATION_MASTER",
        b"canonical-scene-master",
    )
    with container.database.session() as session:
        character = session.scalar(
            select(Character).where(
                Character.project_id == project.id,
                Character.name == "LinJin",
            )
        )
        location = session.scalar(select(Location).where(Location.project_id == project.id))
        assert character is not None
        assert location is not None
        character_id = character.id
        location.canonical_asset_id = scene_master.id
    container.characters.confirm_identity(character_id, character_master.id)
    binding = container.characters.binding(character_id)

    async def generate_validate_commit(shot_id: str, sequence: int) -> tuple[str, str]:
        candidate, replayed = container.candidates.create_candidate(
            shot_id,
            idempotency_key=f"three-shot-offline-{sequence}",
            character_bindings=[binding],
            reference_asset_ids=[scene_master.id],
            enforce_entitlements=False,
        )
        assert replayed is False
        assert candidate.generation_job_id is not None

        submitted = await container.gateway.process(candidate.generation_job_id)
        assert submitted.status == JobStatus.SUBMITTED.value
        with container.database.session() as session:
            stored_job = session.get(GenerationJob, submitted.id)
            assert stored_job is not None
            stored_job.next_retry_at = utcnow()
        completed = await container.gateway.process(submitted.id)
        assert completed.status == JobStatus.COMPLETED.value
        assert completed.output_asset_id is not None

        validated = container.candidates.sync_candidate(candidate.id, _passing_qa_evidence())
        assert validated.status == CandidateStatus.PASSED.value
        assert validated.qa_result_id is not None
        committed = container.candidates.commit(candidate.id)
        assert committed.status == CandidateStatus.COMMITTED.value
        with container.database.session() as session:
            shot = session.get(Shot, shot_id)
            assert shot is not None
            assert shot.status == ShotStatus.COMMITTED.value
            assert shot.committed_candidate_id == candidate.id
            assert shot.output_video_asset_id == completed.output_asset_id
            assert shot.end_frame_asset_id is not None
            end_frame = session.get(MediaAsset, shot.end_frame_asset_id)
            assert end_frame is not None
            assert end_frame.asset_type == "END_FRAME"
            assert end_frame.parent_asset_id == completed.output_asset_id
            assert end_frame.generation_candidate_id == candidate.id
            return candidate.id, shot.end_frame_asset_id

    candidate1_id, end_frame1_id = await generate_validate_commit(shot1_id, 1)
    with container.database.session() as session:
        shot1 = session.get(Shot, shot1_id)
        shot2 = session.get(Shot, shot2_id)
        assert shot1 is not None and shot2 is not None
        shot1_output = session.get(TimelineState, shot1.output_state_id)
        shot2_input = session.get(TimelineState, shot2.input_state_id)
        assert shot1_output is not None and shot2_input is not None
        assert shot2_input.previous_state_id == shot1_output.id
        assert shot2_input.state_json == shot1_output.state_json

    continuity2 = container.orchestrator.plan_continuity(
        shot2_id,
        project.id,
        ContinuityRiskVector(
            camera_angle_delta=0.04,
            camera_axis_delta=0.03,
            action_continuity=0.98,
            previous_frame_quality=0.94,
        ),
    )
    assert continuity2.detail["mode"] == ContinuityMode.HARD_CONTINUITY.value
    candidate2_id, end_frame2_id = await generate_validate_commit(shot2_id, 2)
    with container.database.session() as session:
        shot2 = session.get(Shot, shot2_id)
        shot2_candidate = session.get(GenerationCandidate, candidate2_id)
        assert shot2 is not None and shot2_candidate is not None
        shot2_job = session.get(GenerationJob, shot2_candidate.generation_job_id)
        assert shot2_job is not None
        assert shot2.continuity_mode == ContinuityMode.HARD_CONTINUITY.value
        assert shot2.generation_policy == GenerationPolicy.CONTINUE_I2V.value
        assert shot2_job.policy == GenerationPolicy.CONTINUE_I2V.value
        assert shot2_job.request_json["start_frame_asset_id"] == end_frame1_id

        shot3 = session.get(Shot, shot3_id)
        shot2_output = session.get(TimelineState, shot2.output_state_id)
        shot3_input = session.get(TimelineState, shot3.input_state_id) if shot3 else None
        assert shot2_output is not None and shot3_input is not None
        assert shot3_input.previous_state_id == shot2_output.id
        assert shot3_input.state_json == shot2_output.state_json

    continuity3 = container.orchestrator.plan_continuity(
        shot3_id,
        project.id,
        ContinuityRiskVector(reverse_shot=True, camera_axis_delta=0.9),
    )
    assert continuity3.detail["mode"] == ContinuityMode.RE_ANCHOR.value
    candidate3_id, _ = await generate_validate_commit(shot3_id, 3)

    with container.database.session() as session:
        shot3 = session.get(Shot, shot3_id)
        shot3_candidate = session.get(GenerationCandidate, candidate3_id)
        assert shot3 is not None and shot3_candidate is not None
        shot3_job = session.get(GenerationJob, shot3_candidate.generation_job_id)
        assert shot3_job is not None
        assert shot3.continuity_mode == ContinuityMode.RE_ANCHOR.value
        assert shot3.generation_policy == GenerationPolicy.REANCHOR_FULL.value
        assert shot3.start_frame_asset_id is None
        assert shot3_job.policy == GenerationPolicy.REANCHOR_FULL.value
        assert shot3_job.request_json["start_frame_asset_id"] is None
        assert set(shot3_job.request_json["reference_asset_ids"]).issuperset(
            {character_master.id, scene_master.id}
        )

        jobs = list(session.scalars(select(GenerationJob).where(GenerationJob.project_id == project.id)))
        candidates = list(
            session.scalars(
                select(GenerationCandidate).where(
                    GenerationCandidate.shot_id.in_([shot1_id, shot2_id, shot3_id])
                )
            )
        )
        qa_results = list(
            session.scalars(
                select(QAResult).where(
                    QAResult.candidate_id.in_([candidate1_id, candidate2_id, candidate3_id])
                )
            )
        )
        snapshots = list(
            session.scalars(
                select(ShotStateSnapshot).where(ShotStateSnapshot.shot_id.in_([shot1_id, shot2_id, shot3_id]))
            )
        )
        costs = list(session.scalars(select(CostRecord).where(CostRecord.project_id == project.id)))
        assert len(jobs) == 3
        assert all(job.status == JobStatus.COMPLETED.value and job.output_asset_id for job in jobs)
        assert len(candidates) == 3
        assert all(candidate.status == CandidateStatus.COMMITTED.value for candidate in candidates)
        assert len(qa_results) == 3
        assert all(result.decision == QADecision.PASS.value for result in qa_results)
        assert len(snapshots) == 3
        assert {snapshot.candidate_id for snapshot in snapshots} == {
            candidate1_id,
            candidate2_id,
            candidate3_id,
        }
        assert len(costs) == 3
        assert all(record.generation_job_id and record.accepted and not record.wasted for record in costs)
        assert end_frame2_id == session.get(Shot, shot2_id).end_frame_asset_id

    assert len(fixture_provider.submitted_requests) == 3
    assert {character_master.id, scene_master.id}.issubset(fixture_provider.uploaded_asset_ids)
