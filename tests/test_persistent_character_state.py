from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import pytest
from character_core import (
    CharacterStateConflict,
    CharacterStateEvidenceRequired,
    CharacterStatePolicyViolation,
    normalize_and_apply_patch,
    normalize_initial_state,
    preview_character_state_transition,
)
from evaluation_core import EvaluationEvidence
from production_domain.models import (
    CandidateStatus,
    CharacterStateCommit,
    CharacterStateDecision,
    CharacterStateDelta,
    CharacterStateHead,
    CharacterStateValidation,
    CharacterStateValidationStage,
    CharacterStateValidatorKind,
    CharacterStateVersion,
    Episode,
    GenerationCandidate,
    GenerationJob,
    ModelDefinition,
    ModelExecutionRecord,
    QADecision,
    QAResult,
    Scene,
    Shot,
    ShotStatus,
    TimelineState,
    TimelineTransition,
    TimelineTransitionType,
    User,
)
from sqlalchemy import func, select


@dataclass(frozen=True)
class _MiraStory:
    project_id: str
    user_id: str
    character_id: str
    identity_id: str
    version_one_id: str
    shot_12_id: str
    shot_13_id: str
    shot_14_id: str
    shot_13_output_asset_id: str
    trusted_vlm_execution_id: str
    voyage_execution_id: str


_SHOT_13_PATCH = {
    "operations": [
        {
            "op": "REPLACE",
            "path": "appearance.injury.blood_state",
            "from": "fresh",
            "to": "dried",
        },
        {
            "op": "REPLACE",
            "path": "props.flare.location",
            "from": "hand",
            "to": "waist",
        },
        {
            "op": "REPLACE",
            "path": "narrative_state.location",
            "from": "platform_3_support_column",
            "to": "platform_3_tunnel_edge",
        },
    ]
}

_SHOT_13_OBSERVATIONS: dict[str, Any] = {
    "appearance.injury.blood_state": "dried",
    "appearance.injury.status": "unhealed",
    "appearance.outfit.damage.left_sleeve": "torn",
    "props.flare.state": "unlit",
    "props.flare.location": "waist",
    "narrative_state.location": "platform_3_tunnel_edge",
    "narrative_state.lighting": "cold_blue_gray_dusk",
}


def _register_media(
    container: Any,
    project_id: str,
    asset_type: str,
    payload: bytes,
    filename: str,
    mime_type: str,
) -> str:
    asset, _ = container.media.register(
        project_id,
        asset_type,
        io.BytesIO(payload),
        filename=filename,
        mime_type=mime_type,
    )
    return str(asset.id)


def _add_model_execution(
    session: Any,
    *,
    project_id: str,
    logical_name: str,
    provider: str,
    evidence_asset_id: str,
) -> str:
    model = ModelDefinition(
        logical_name=logical_name,
        provider=provider,
        provider_model_id=f"{logical_name}-v1",
        modality="multimodal",
        capabilities=["state_observation"],
    )
    session.add(model)
    session.flush()
    execution = ModelExecutionRecord(
        project_id=project_id,
        role="VLM_REVIEWER",
        model_definition_id=model.id,
        provider=provider,
        provider_model_id=model.provider_model_id,
        request_hash=("a" if "voyage" not in provider else "b") * 64,
        latency_ms=1,
        status="SUCCEEDED",
        metadata_json={
            "evidence_purpose": "CHARACTER_STATE_FACT_OBSERVATION",
            "evidence_asset_id": evidence_asset_id,
        },
    )
    session.add(execution)
    session.flush()
    return str(execution.id)


def _build_mira_story(
    container: Any,
    project: Any,
    *,
    baseline_state: dict[str, Any] | None = None,
) -> _MiraStory:
    master_asset_id = _register_media(
        container,
        project.id,
        "CHARACTER_MASTER",
        b"mira-canonical-master",
        "mira-master.png",
        "image/png",
    )
    shot_12_asset_id = _register_media(
        container,
        project.id,
        "VIDEO",
        b"mira-shot-12",
        "mira-shot-12.mp4",
        "video/mp4",
    )
    shot_13_asset_id = _register_media(
        container,
        project.id,
        "VIDEO",
        b"mira-shot-13",
        "mira-shot-13.mp4",
        "video/mp4",
    )
    character = container.characters.create_character(
        project.id,
        "米拉·奥孔科沃",
        canonical_facts={"role": "protagonist"},
    )
    identity = container.characters.confirm_identity(
        character.id,
        master_asset_id,
        hair_signature="short braids with silver highlights",
        costume_signature="charcoal field jacket canonical design",
    )

    with container.database.session() as session:
        confirmer = User(
            email=f"mira-state-confirmer-{project.id}@example.com",
            display_name="Mira continuity confirmer",
        )
        episode = Episode(
            project_id=project.id,
            title="Mira at Platform 3",
            episode_number=1,
            script_source="Mira recovers, then approaches the dark tunnel.",
        )
        session.add_all([confirmer, episode])
        session.flush()

        shots: dict[int, Shot] = {}
        states: dict[int, tuple[TimelineState, TimelineState]] = {}
        for sequence in (12, 13, 14):
            scene = Scene(
                episode_id=episode.id,
                sequence=sequence,
                description=f"Platform 3 continuity scene {sequence}",
                time_context="dusk",
            )
            session.add(scene)
            session.flush()
            input_state = TimelineState(
                project_id=project.id,
                episode_id=episode.id,
                scene_id=scene.id,
                state_kind="SHOT_INPUT",
                state_json={},
            )
            output_state = TimelineState(
                project_id=project.id,
                episode_id=episode.id,
                scene_id=scene.id,
                state_kind="SHOT_OUTPUT",
                state_json={},
            )
            session.add_all([input_state, output_state])
            session.flush()
            shot = Shot(
                scene_id=scene.id,
                sequence=sequence,
                prompt=f"Mira continuity shot {sequence}",
                input_state_id=input_state.id,
                output_state_id=output_state.id,
                status=(ShotStatus.COMMITTED.value if sequence == 12 else ShotStatus.DRAFT.value),
            )
            session.add(shot)
            session.flush()
            input_state.shot_id = shot.id
            output_state.shot_id = shot.id
            shots[sequence] = shot
            states[sequence] = (input_state, output_state)

        shots[12].next_shot_id = shots[13].id
        shots[13].previous_shot_id = shots[12].id
        shots[13].next_shot_id = shots[14].id
        shots[14].previous_shot_id = shots[13].id
        session.add_all(
            [
                TimelineTransition(
                    project_id=project.id,
                    source_shot_id=shots[12].id,
                    target_shot_id=shots[13].id,
                    transition_type=TimelineTransitionType.CONTINUOUS.value,
                    metadata_json={"propagation_semantics": "FULL"},
                ),
                TimelineTransition(
                    project_id=project.id,
                    source_shot_id=shots[13].id,
                    target_shot_id=shots[14].id,
                    transition_type=TimelineTransitionType.CONTINUOUS.value,
                    metadata_json={"propagation_semantics": "FULL"},
                ),
            ]
        )
        shot_12_candidate = GenerationCandidate(
            shot_id=shots[12].id,
            attempt_number=1,
            output_asset_id=shot_12_asset_id,
            status=CandidateStatus.COMMITTED.value,
            accepted_by=confirmer.id,
        )
        session.add(shot_12_candidate)
        session.flush()
        shots[12].committed_candidate_id = shot_12_candidate.id
        trusted_vlm_execution_id = _add_model_execution(
            session,
            project_id=project.id,
            logical_name=f"mira-trusted-vlm-{project.id}",
            provider="test-trusted-character-vlm",
            evidence_asset_id=shot_13_asset_id,
        )
        voyage_execution_id = _add_model_execution(
            session,
            project_id=project.id,
            logical_name=f"mira-voyage-advisory-{project.id}",
            provider="voyage",
            evidence_asset_id=shot_13_asset_id,
        )
        session.flush()
        ids = {
            "user_id": confirmer.id,
            "shot_12_id": shots[12].id,
            "shot_13_id": shots[13].id,
            "shot_14_id": shots[14].id,
            "shot_12_candidate_id": shot_12_candidate.id,
            "trusted_vlm_execution_id": trusted_vlm_execution_id,
            "voyage_execution_id": voyage_execution_id,
        }

    version_one = container.character_states.initialize_from_committed_candidate(
        project_id=project.id,
        character_id=character.id,
        shot_id=ids["shot_12_id"],
        candidate_id=ids["shot_12_candidate_id"],
        committed_by_user_id=ids["user_id"],
        reason="Human confirmed the committed shot 12 baseline.",
        narrative_state=(
            baseline_state
            if baseline_state is not None
            else {
                "appearance": {
                    "injury": {
                        "location": "right_eyebrow",
                        "severity": "minor",
                        "status": "unhealed",
                        "blood_state": "fresh",
                    },
                    "outfit": {"damage": {"left_sleeve": "torn"}},
                },
                "props": {"flare": {"state": "unlit", "location": "hand"}},
                "narrative_state": {
                    "location": "platform_3_support_column",
                    "time_of_day": "dusk",
                    "lighting": "cold_blue_gray_dusk",
                    "emotional_beat": "guarded_recovering",
                    "props_in_hand": "flare_unlit",
                },
                "continuity_constraints": [
                    {
                        "id": "injury-visible",
                        "path": "appearance.injury.status",
                        "rule": "MUST_EQUAL",
                        "value": "unhealed",
                        "evidence_required": True,
                    },
                    {
                        "id": "left-sleeve-torn",
                        "path": "appearance.outfit.damage.left_sleeve",
                        "rule": "MUST_EQUAL",
                        "value": "torn",
                        "evidence_required": True,
                    },
                    {
                        "id": "flare-unlit-until-scene-14",
                        "path": "props.flare.state",
                        "rule": "LOCK_UNTIL_SCENE",
                        "value": "unlit",
                        "release_scene_sequence": 14,
                        "evidence_required": True,
                    },
                    {
                        "id": "cold-lighting",
                        "path": "narrative_state.lighting",
                        "rule": "MUST_EQUAL",
                        "value": "cold_blue_gray_dusk",
                        "evidence_required": True,
                    },
                ],
            }
        ),
    )

    return _MiraStory(
        project_id=str(project.id),
        user_id=ids["user_id"],
        character_id=str(character.id),
        identity_id=str(identity.id),
        version_one_id=str(version_one.id),
        shot_12_id=ids["shot_12_id"],
        shot_13_id=ids["shot_13_id"],
        shot_14_id=ids["shot_14_id"],
        shot_13_output_asset_id=shot_13_asset_id,
        trusted_vlm_execution_id=ids["trusted_vlm_execution_id"],
        voyage_execution_id=ids["voyage_execution_id"],
    )


def _create_shot_13_candidate(container: Any, story: _MiraStory) -> str:
    with container.database.session() as session:
        candidate = GenerationCandidate(
            shot_id=story.shot_13_id,
            attempt_number=1,
            status=CandidateStatus.CREATED.value,
            metadata_json={"test_case": "mira-shot-13-state-transition"},
        )
        session.add(candidate)
        session.flush()
        return str(candidate.id)


def _propose_shot_13(
    container: Any,
    story: _MiraStory,
    candidate_id: str,
    *,
    patch: dict[str, Any] = _SHOT_13_PATCH,
    suffix: str = "approved",
) -> str:
    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        assert candidate is not None
        delta = container.character_states.propose_for_candidate_in_session(
            session,
            candidate=candidate,
            character_id=story.character_id,
            base_state_version_id=story.version_one_id,
            patch_json=patch,
            idempotency_key=f"mira-shot-13-{suffix}-{candidate_id}",
        )
        # Mirror the real allocation boundary: the proposal is frozen while
        # CREATED, then dispatch/output move the candidate forward.
        candidate.status = CandidateStatus.PASSED.value
        candidate.output_asset_id = story.shot_13_output_asset_id
        return str(delta.id)


def _evaluation_evidence(
    story: _MiraStory,
    *,
    voyage: bool = False,
    overrides: dict[str, Any] | None = None,
) -> EvaluationEvidence:
    values = {**_SHOT_13_OBSERVATIONS, **(overrides or {})}
    return EvaluationEvidence(
        evidence_complete=True,
        judge_provider=("voyage" if voyage else "test-trusted-character-vlm"),
        judge_model=("voyage-multimodal-3.5" if voyage else "offline-character-vlm-v1"),
        model_execution_record_id=(story.voyage_execution_id if voyage else story.trusted_vlm_execution_id),
        state_observations=[
            {
                "path": path,
                "value": value,
                "confidence": 0.98,
                "source": ("voyage-multimodal-3.5" if voyage else "local-frame-state-observation"),
            }
            for path, value in values.items()
        ],
    )


def _attach_state_qa(
    container: Any,
    story: _MiraStory,
    candidate_id: str,
    evidence: EvaluationEvidence,
) -> str:
    character_evidence = container.candidates._state_evidence_from_evaluation(
        candidate_id,
        evidence,
    )
    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        assert candidate is not None
        qa = QAResult(
            candidate_id=candidate.id,
            profile="DIALOGUE",
            level_reached=4,
            decision=QADecision.PASS.value,
            overall_score=0.98,
            metrics_json={"character_state_evidence": character_evidence},
            summary="Trusted visual state evidence collected.",
        )
        session.add(qa)
        session.flush()
        candidate.qa_result_id = qa.id
        return str(qa.id)


def _validate_shot_13(
    container: Any,
    story: _MiraStory,
    candidate_id: str,
    evidence: EvaluationEvidence,
) -> tuple[str, str]:
    qa_id = _attach_state_qa(container, story, candidate_id, evidence)
    summary = container.character_states.validate_candidate(candidate_id, qa_id)
    assert len(summary.delta_ids) == 1
    return summary.decision, qa_id


def _commit_and_propagate(
    container: Any,
    story: _MiraStory,
    candidate_id: str,
    qa_id: str,
) -> str:
    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        shot = session.get(Shot, story.shot_13_id)
        qa = session.get(QAResult, qa_id)
        assert candidate is not None and shot is not None and qa is not None
        output_state = session.get(TimelineState, shot.output_state_id)
        assert output_state is not None
        candidate.status = CandidateStatus.COMMITTED.value
        shot.status = ShotStatus.COMMITTED.value
        shot.committed_candidate_id = candidate.id
        versions = container.character_states.commit_candidate_in_session(
            session,
            candidate=candidate,
            shot=shot,
            qa=qa,
            output_state=output_state,
            committed_by_user_id=None,
        )
        assert len(versions) == 1
        propagation = container.character_states.timeline.propagate(session, shot, output_state)
        assert propagation.propagated is True
        assert propagation.next_shot_id == story.shot_14_id
        return str(versions[0].id)


def test_parent_object_replacement_derives_changed_visual_leaf_paths() -> None:
    base_state = {
        "narrative_state": {
            "location": "platform_3_support_column",
            "lighting": "cold_blue_gray_dusk",
            "emotional_beat": "guarded_recovering",
        },
        "continuity_constraints": [],
    }
    replacement = {
        "location": "platform_3_tunnel_edge",
        "lighting": "emergency_lights_only",
        "emotional_beat": "alert",
    }

    preview = preview_character_state_transition(
        base_state,
        {
            "operations": [
                {
                    "op": "REPLACE",
                    "path": "narrative_state",
                    "from": base_state["narrative_state"],
                    "to": replacement,
                }
            ]
        },
        scene_sequence=13,
    )

    assert preview.changed_paths == (
        "narrative_state.emotional_beat",
        "narrative_state.lighting",
        "narrative_state.location",
    )
    assert preview.required_visual_paths == (
        "narrative_state.lighting",
        "narrative_state.location",
    )


def test_initial_baseline_rejects_constraint_conflict_at_source_scene(
    container: Any,
    project: Any,
) -> None:
    with pytest.raises(
        CharacterStatePolicyViolation,
        match="STATE_LOCK_ACTIVE:props.flare.state",
    ):
        _build_mira_story(
            container,
            project,
            baseline_state={
                "props": {"flare": {"state": "lit"}},
                "continuity_constraints": [
                    {
                        "id": "flare-unlit-until-scene-14",
                        "path": "props.flare.state",
                        "rule": "LOCK_UNTIL_SCENE",
                        "value": "unlit",
                        "release_scene_sequence": 14,
                    }
                ],
            },
        )

    with container.database.session() as session:
        assert (
            session.scalar(
                select(func.count(CharacterStateVersion.id)).where(
                    CharacterStateVersion.project_id == project.id
                )
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(CharacterStateDelta.id)).where(CharacterStateDelta.project_id == project.id)
            )
            == 0
        )


def test_initial_baseline_rejects_duplicate_constraint_ids(
    container: Any,
    project: Any,
) -> None:
    with pytest.raises(
        CharacterStatePolicyViolation,
        match="duplicate continuity constraint id: duplicate-lock",
    ):
        _build_mira_story(
            container,
            project,
            baseline_state={
                "props": {"flare": {"state": "unlit"}},
                "continuity_constraints": [
                    {
                        "id": "duplicate-lock",
                        "path": "props.flare.state",
                        "rule": "MUST_EXIST",
                    },
                    {
                        "id": "duplicate-lock",
                        "path": "props.flare.state",
                        "rule": "MUST_EQUAL",
                        "value": "unlit",
                    },
                ],
            },
        )


def test_mira_state_delta_evidence_commit_and_future_propagation(container: Any, project: Any) -> None:
    story = _build_mira_story(container, project)
    candidate_id = _create_shot_13_candidate(container, story)
    delta_id = _propose_shot_13(container, story, candidate_id)

    with container.database.session() as session:
        version_one = session.get(CharacterStateVersion, story.version_one_id)
        delta = session.get(CharacterStateDelta, delta_id)
        policy = session.scalar(
            select(CharacterStateValidation).where(
                CharacterStateValidation.state_delta_id == delta_id,
                CharacterStateValidation.stage == CharacterStateValidationStage.POLICY.value,
            )
        )
        assert version_one is not None and delta is not None and policy is not None
        assert version_one.version == 1
        assert version_one.identity_version_id == story.identity_id
        assert version_one.narrative_state_json["appearance"]["injury"]["blood_state"] == "fresh"
        assert policy.decision == CharacterStateDecision.PASS.value
        assert delta.patch_json == [
            {"op": "replace", "path": "/appearance/injury/blood_state", "value": "dried"},
            {"op": "replace", "path": "/props/flare/location", "value": "waist"},
            {
                "op": "replace",
                "path": "/narrative_state/location",
                "value": "platform_3_tunnel_edge",
            },
        ]

    decision, qa_id = _validate_shot_13(
        container,
        story,
        candidate_id,
        _evaluation_evidence(story),
    )
    assert decision == CharacterStateDecision.PASS.value

    with container.database.session() as session:
        visual = session.scalar(
            select(CharacterStateValidation).where(
                CharacterStateValidation.state_delta_id == delta_id,
                CharacterStateValidation.stage == CharacterStateValidationStage.VISUAL.value,
            )
        )
        assert visual is not None
        assert visual.decision == CharacterStateDecision.PASS.value
        assert visual.validator_kind == CharacterStateValidatorKind.VLM.value
        assert visual.model_execution_record_id == story.trusted_vlm_execution_id
        assert visual.evidence_asset_id == story.shot_13_output_asset_id

    version_two_id = _commit_and_propagate(container, story, candidate_id, qa_id)

    with container.database.session() as session:
        version_one = session.get(CharacterStateVersion, story.version_one_id)
        version_two = session.get(CharacterStateVersion, version_two_id)
        head = session.scalar(
            select(CharacterStateHead).where(
                CharacterStateHead.project_id == story.project_id,
                CharacterStateHead.character_id == story.character_id,
                CharacterStateHead.timeline_scope_key == "main",
            )
        )
        commit = session.scalar(
            select(CharacterStateCommit).where(CharacterStateCommit.state_delta_id == delta_id)
        )
        shot_14 = session.get(Shot, story.shot_14_id)
        assert version_one is not None and version_two is not None
        assert head is not None and commit is not None and shot_14 is not None
        shot_14_input = session.get(TimelineState, shot_14.input_state_id)
        shot_14_output = session.get(TimelineState, shot_14.output_state_id)
        assert shot_14_input is not None and shot_14_output is not None

        state = version_two.narrative_state_json
        assert version_two.version == 2
        assert version_two.previous_state_version_id == version_one.id
        assert version_two.previous_state_hash == version_one.state_hash
        assert version_two.identity_version_id == story.identity_id
        assert state["appearance"]["injury"]["blood_state"] == "dried"
        assert state["appearance"]["outfit"]["damage"]["left_sleeve"] == "torn"
        assert state["props"]["flare"] == {"state": "unlit", "location": "waist"}
        assert state["narrative_state"]["lighting"] == "cold_blue_gray_dusk"
        assert head.state_version_id == version_two.id
        assert head.lock_version == 2
        assert commit.from_state_version_id == version_one.id
        assert commit.to_state_version_id == version_two.id

        propagated_ref = shot_14_input.state_json["character_state_refs"][story.character_id]
        assert propagated_ref["state_version_id"] == version_two.id
        assert propagated_ref["identity_version_id"] == story.identity_id
        assert (
            shot_14_input.previous_state_id
            == session.get(
                TimelineState,
                session.get(Shot, story.shot_13_id).output_state_id,
            ).id
        )
        assert (
            shot_14_output.state_json["characters"][story.character_id]["narrative_state"]
            == version_two.narrative_state_json
        )

    binding = container.characters.binding(
        story.character_id,
        project_id=story.project_id,
        timeline_state_id=shot_14_input.id,
    )
    assert binding["narrative_state_version_id"] == version_two_id
    assert binding["narrative_state"]["appearance"]["injury"]["blood_state"] == "dried"


def test_state_proposal_freezes_at_dispatch_but_exact_replay_is_idempotent(
    container: Any,
    project: Any,
) -> None:
    story = _build_mira_story(container, project)
    candidate_id = _create_shot_13_candidate(container, story)
    delta_id = _propose_shot_13(container, story, candidate_id, suffix="frozen")

    # A transport retry of the exact allocation is safe after dispatch.
    replay_id = _propose_shot_13(container, story, candidate_id, suffix="frozen")
    assert replay_id == delta_id

    # A new proposal revision would not have conditioned the generated pixels.
    with container.database.session() as session:
        candidate = session.get(GenerationCandidate, candidate_id)
        assert candidate is not None
        with pytest.raises(CharacterStateConflict, match="freeze when generation is dispatched"):
            container.character_states.propose_for_candidate_in_session(
                session,
                candidate=candidate,
                character_id=story.character_id,
                base_state_version_id=story.version_one_id,
                patch_json=_SHOT_13_PATCH,
                idempotency_key=f"late-revision-{candidate_id}",
            )


def test_generation_job_is_bound_to_the_frozen_state_proposal_set(
    container: Any,
    project: Any,
) -> None:
    story = _build_mira_story(container, project)
    with container.database.session() as session:
        shot = session.get(Shot, story.shot_13_id)
        assert shot is not None
        input_state_id = shot.input_state_id
    binding = container.characters.binding(
        story.character_id,
        project_id=story.project_id,
        timeline_state_id=input_state_id,
    )

    candidate, replayed = container.candidates.create_candidate(
        story.shot_13_id,
        idempotency_key=f"mira-state-generation-{story.shot_13_id}",
        character_bindings=[binding],
        state_deltas=[
            {
                "character_id": story.character_id,
                "base_state_version_id": story.version_one_id,
                "patch": _SHOT_13_PATCH,
            }
        ],
        enforce_entitlements=False,
    )
    assert replayed is False

    with container.database.session() as session:
        stored = session.get(GenerationCandidate, candidate.id)
        assert stored is not None and stored.generation_job_id is not None
        proposal_hash = stored.metadata_json.get("character_state_proposal_set_hash")
        job = session.get(GenerationJob, stored.generation_job_id)
        assert isinstance(proposal_hash, str) and len(proposal_hash) == 64
        assert job is not None
        assert job.request_json["metadata"]["character_state_proposal_set_hash"] == proposal_hash
        assert "dried" in job.request_json["prompt"]


def test_state_payload_limits_and_visual_removal_are_fail_closed() -> None:
    too_deep: dict[str, Any] = {}
    cursor = too_deep
    for index in range(14):
        child: dict[str, Any] = {}
        cursor[f"level_{index}"] = child
        cursor = child
    with pytest.raises(CharacterStatePolicyViolation, match="depth limit"):
        normalize_initial_state(too_deep)

    with pytest.raises(CharacterStatePolicyViolation, match="cannot be removed"):
        normalize_and_apply_patch(
            {"props": {"flare": {"state": "unlit"}}},
            {
                "operations": [
                    {
                        "op": "REMOVE",
                        "path": "props.flare",
                        "from": {"state": "unlit"},
                    }
                ]
            },
        )


def test_branch_transition_forks_v1_without_advancing_main_head(
    container: Any,
    project: Any,
) -> None:
    story = _build_mira_story(container, project)
    branch_key = f"dream:{story.shot_13_id}"
    with container.database.session() as session:
        transition = session.scalar(
            select(TimelineTransition).where(TimelineTransition.target_shot_id == story.shot_13_id)
        )
        assert transition is not None
        transition.transition_type = TimelineTransitionType.DREAM.value
        transition.branch_key = branch_key
        transition.metadata_json = {
            "timeline_branch": "NEW_BRANCH",
            "propagation_semantics": "RESET_BOUNDARY",
        }

    candidate_id = _create_shot_13_candidate(container, story)
    delta_id = _propose_shot_13(container, story, candidate_id, suffix="dream-branch")
    decision, qa_id = _validate_shot_13(
        container,
        story,
        candidate_id,
        _evaluation_evidence(story),
    )
    assert decision == CharacterStateDecision.PASS.value
    branch_version_id = _commit_and_propagate(container, story, candidate_id, qa_id)

    with container.database.session() as session:
        delta = session.get(CharacterStateDelta, delta_id)
        branch_version = session.get(CharacterStateVersion, branch_version_id)
        main_head = session.scalar(
            select(CharacterStateHead).where(
                CharacterStateHead.character_id == story.character_id,
                CharacterStateHead.timeline_scope_key == "main",
            )
        )
        branch_head = session.scalar(
            select(CharacterStateHead).where(
                CharacterStateHead.character_id == story.character_id,
                CharacterStateHead.timeline_scope_key == branch_key,
            )
        )
        shot_14 = session.get(Shot, story.shot_14_id)
        assert delta is not None and branch_version is not None
        assert main_head is not None and branch_head is not None and shot_14 is not None
        assert delta.timeline_scope_key == branch_key
        assert delta.target_version == 1
        assert branch_version.version == 1
        assert branch_version.previous_state_version_id == story.version_one_id
        assert branch_head.state_version_id == branch_version.id
        assert main_head.state_version_id == story.version_one_id
        shot_14_input = session.get(TimelineState, shot_14.input_state_id)
        assert shot_14_input is not None
        propagated = shot_14_input.state_json["character_state_refs"][story.character_id]
        assert propagated["timeline_scope_key"] == branch_key
        assert propagated["state_version_id"] == branch_version.id


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        (
            {
                "operations": [
                    {
                        "op": "ADD",
                        "path": "appearance.hair",
                        "to": "different long red hair",
                    }
                ]
            },
            "immutable identity path cannot change",
        ),
        (
            {
                "operations": [
                    {
                        "op": "REPLACE",
                        "path": "props.flare.state",
                        "from": "unlit",
                        "to": "lit",
                    }
                ]
            },
            "STATE_LOCK_ACTIVE:props.flare.state",
        ),
    ],
    ids=["immutable-identity", "flare-lock-before-scene-14"],
)
def test_policy_rejects_identity_mutation_and_early_flare_ignition(
    container: Any,
    project: Any,
    patch: dict[str, Any],
    message: str,
) -> None:
    story = _build_mira_story(container, project)
    candidate_id = _create_shot_13_candidate(container, story)

    with pytest.raises(CharacterStatePolicyViolation, match=message):
        _propose_shot_13(
            container,
            story,
            candidate_id,
            patch=patch,
            suffix="policy-rejected",
        )

    with container.database.session() as session:
        assert (
            session.scalar(
                select(func.count(CharacterStateDelta.id)).where(
                    CharacterStateDelta.candidate_id == candidate_id
                )
            )
            == 0
        )
        current = container.character_states.current(story.project_id, story.character_id)
        assert current is not None and current.id == story.version_one_id


def test_voyage_state_observations_are_advisory_and_require_review(container: Any, project: Any) -> None:
    story = _build_mira_story(container, project)
    candidate_id = _create_shot_13_candidate(container, story)
    _propose_shot_13(container, story, candidate_id)

    decision, qa_id = _validate_shot_13(
        container,
        story,
        candidate_id,
        _evaluation_evidence(story, voyage=True),
    )
    assert decision == CharacterStateDecision.REVIEW_REQUIRED.value

    with pytest.raises(CharacterStateEvidenceRequired, match="explicit human review"):
        with container.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            shot = session.get(Shot, story.shot_13_id)
            qa = session.get(QAResult, qa_id)
            assert candidate is not None and shot is not None and qa is not None
            output_state = session.get(TimelineState, shot.output_state_id)
            assert output_state is not None
            candidate.status = CandidateStatus.COMMITTED.value
            shot.status = ShotStatus.COMMITTED.value
            shot.committed_candidate_id = candidate.id
            container.character_states.commit_candidate_in_session(
                session,
                candidate=candidate,
                shot=shot,
                qa=qa,
                output_state=output_state,
                committed_by_user_id=None,
            )

    current = container.character_states.current(story.project_id, story.character_id)
    assert current is not None and current.id == story.version_one_id


def test_confident_visual_state_mismatch_rejects_candidate_state(container: Any, project: Any) -> None:
    story = _build_mira_story(container, project)
    candidate_id = _create_shot_13_candidate(container, story)
    delta_id = _propose_shot_13(container, story, candidate_id)

    decision, _qa_id = _validate_shot_13(
        container,
        story,
        candidate_id,
        _evaluation_evidence(
            story,
            overrides={"appearance.injury.blood_state": "fresh"},
        ),
    )
    assert decision == CharacterStateDecision.REJECT.value

    with container.database.session() as session:
        validation = session.scalar(
            select(CharacterStateValidation).where(
                CharacterStateValidation.state_delta_id == delta_id,
                CharacterStateValidation.stage == CharacterStateValidationStage.VISUAL.value,
            )
        )
        assert validation is not None
        assert validation.decision == CharacterStateDecision.REJECT.value
        assert {item["code"] for item in validation.violations_json} == {
            "STATE_EVIDENCE_MISMATCH:appearance.injury.blood_state"
        }
        assert (
            session.scalar(
                select(func.count(CharacterStateCommit.id)).where(
                    CharacterStateCommit.state_delta_id == delta_id
                )
            )
            == 0
        )


def test_stale_base_and_timeline_fence_prevent_state_commit(container: Any, project: Any) -> None:
    story = _build_mira_story(container, project)
    stale_candidate_id = _create_shot_13_candidate(container, story)
    with container.database.session() as session:
        stale_candidate = session.get(GenerationCandidate, stale_candidate_id)
        assert stale_candidate is not None
        with pytest.raises(CharacterStateConflict, match="does not explicitly select"):
            container.character_states.propose_for_candidate_in_session(
                session,
                candidate=stale_candidate,
                character_id=story.character_id,
                base_state_version_id="00000000-0000-0000-0000-000000000000",
                patch_json=_SHOT_13_PATCH,
                idempotency_key=f"mira-stale-base-{stale_candidate_id}",
            )

    candidate_id = stale_candidate_id
    delta_id = _propose_shot_13(container, story, candidate_id, suffix="timeline-fence")
    decision, qa_id = _validate_shot_13(
        container,
        story,
        candidate_id,
        _evaluation_evidence(story),
    )
    assert decision == CharacterStateDecision.PASS.value

    with pytest.raises(
        CharacterStateConflict, match="timeline state changed after this candidate was planned"
    ):
        with container.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            shot = session.get(Shot, story.shot_13_id)
            qa = session.get(QAResult, qa_id)
            assert candidate is not None and shot is not None and qa is not None
            output_state = session.get(TimelineState, shot.output_state_id)
            assert output_state is not None
            output_state.state_json = {
                **output_state.state_json,
                "concurrent_planner_write": "new-plan-fence",
            }
            candidate.status = CandidateStatus.COMMITTED.value
            shot.status = ShotStatus.COMMITTED.value
            shot.committed_candidate_id = candidate.id
            session.flush()
            container.character_states.commit_candidate_in_session(
                session,
                candidate=candidate,
                shot=shot,
                qa=qa,
                output_state=output_state,
                committed_by_user_id=None,
            )

    with container.database.session() as session:
        current = session.scalar(
            select(CharacterStateVersion)
            .where(
                CharacterStateVersion.project_id == story.project_id,
                CharacterStateVersion.character_id == story.character_id,
            )
            .order_by(CharacterStateVersion.version.desc())
        )
        assert current is not None and current.id == story.version_one_id
        assert (
            session.scalar(
                select(func.count(CharacterStateCommit.id)).where(
                    CharacterStateCommit.state_delta_id == delta_id
                )
            )
            == 0
        )
