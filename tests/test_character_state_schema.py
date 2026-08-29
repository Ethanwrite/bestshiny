from __future__ import annotations

import io

import pytest
from production_domain.models import (
    CharacterStateCommit,
    CharacterStateDelta,
    CharacterStateHead,
    CharacterStateValidation,
    CharacterStateVersion,
    Episode,
    GenerationCandidate,
    ModelDefinition,
    ModelExecutionRecord,
    Scene,
    Shot,
    TimelineState,
    User,
)
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, OperationalError


def _committed_initial_state(
    container,
    project,
    *,
    visual_decision: str = "PASS",
    visual_validator: str = "VLM",
    include_human_override: bool = False,
    timeline_scope_key: str = "main",
    narrative_state: dict | None = None,
):  # type: ignore[no-untyped-def]
    master, _ = container.media.register(
        project.id,
        "CHARACTER_MASTER",
        io.BytesIO(b"persistent-character-master"),
        filename="master.png",
        mime_type="image/png",
    )
    character = container.characters.create_character(project.id, "Mira Okonkwo")
    identity = container.characters.confirm_identity(
        character.id,
        master.id,
        hair_signature="short braids with silver highlights",
        costume_signature="charcoal field jacket",
    )
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="Persistent state",
            episode_number=1,
            script_source="Mira moves toward the tunnel.",
        )
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Platform 3")
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
            sequence=13,
            prompt="Mira moves toward the tunnel.",
            input_state_id=input_state.id,
            output_state_id=output_state.id,
        )
        session.add(shot)
        session.flush()
        input_state.shot_id = shot.id
        output_state.shot_id = shot.id
        candidate = GenerationCandidate(
            shot_id=shot.id,
            attempt_number=1,
            status="COMMITTED",
        )
        session.add(candidate)
        session.flush()
        shot.committed_candidate_id = candidate.id
        model = session.scalar(select(ModelDefinition).order_by(ModelDefinition.id))
        if model is None:
            model = ModelDefinition(
                logical_name="schema-test-vlm",
                provider="offline-test",
                provider_model_id="schema-test-vlm",
                modality="multimodal",
            )
            session.add(model)
            session.flush()
        execution = ModelExecutionRecord(
            project_id=project.id,
            role="VLM_REVIEWER",
            model_definition_id=model.id,
            provider=model.provider,
            provider_model_id=model.provider_model_id,
            request_hash="a" * 64,
            latency_ms=1,
            status="COMPLETED",
        )
        session.add(execution)
        session.flush()
        reviewer = None
        if visual_validator == "HUMAN" or include_human_override:
            reviewer = User(
                email=f"state-reviewer-{visual_validator.lower()}-{visual_decision.lower()}@example.com",
                display_name="State reviewer",
            )
            session.add(reviewer)
            session.flush()
        delta = CharacterStateDelta(
            project_id=project.id,
            character_id=character.id,
            timeline_scope_key=timeline_scope_key,
            shot_id=shot.id,
            candidate_id=candidate.id,
            base_state_version_id=None,
            identity_version_id=identity.id,
            input_timeline_state_id=input_state.id,
            planned_output_timeline_state_id=output_state.id,
            proposal_revision=1,
            proposal_kind="INITIALIZE",
            source_kind="RULES",
            patch_json=[
                {
                    "op": "add",
                    "path": "/injuries/right_brow",
                    "value": {"status": "unhealed", "blood_state": "dried"},
                }
            ],
            changed_paths_json=["/injuries/right_brow"],
            proposed_state_json=narrative_state
            or {
                "injuries": {"right_brow": {"status": "unhealed", "blood_state": "dried"}},
                "wardrobe_state": {"field_jacket": {"left_sleeve": "torn"}},
                "props": {"flare": {"state": "unlit", "location": "waist"}},
            },
            target_state_hash="b" * 64,
            input_timeline_state_hash="c" * 64,
            planned_output_timeline_state_hash="d" * 64,
            target_version=1,
            idempotency_key="mira-shot-13-state-v1",
        )
        session.add(delta)
        session.flush()
        state_version = CharacterStateVersion(
            project_id=project.id,
            character_id=character.id,
            timeline_scope_key=timeline_scope_key,
            version=1,
            identity_version_id=identity.id,
            source_shot_id=shot.id,
            source_candidate_id=candidate.id,
            narrative_state_json=delta.proposed_state_json,
            identity_fingerprint="e" * 64,
            state_hash=delta.target_state_hash,
        )
        session.add(state_version)
        session.flush()
        policy_validation = CharacterStateValidation(
            project_id=project.id,
            state_delta_id=delta.id,
            stage="POLICY",
            attempt=1,
            decision="PASS",
            validator_kind="RULE_ENGINE",
            validated_target_hash=delta.target_state_hash,
            evidence_hash="f" * 64,
            evidence_json={"identity_paths_mutated": False},
        )
        visual_validation = CharacterStateValidation(
            project_id=project.id,
            state_delta_id=delta.id,
            stage="VISUAL",
            attempt=1,
            decision=visual_decision,
            validator_kind=visual_validator,
            model_execution_record_id=execution.id if visual_validator == "VLM" else None,
            validated_target_hash=delta.target_state_hash,
            evidence_hash="1" * 64,
            observed_state_json=delta.proposed_state_json,
            evidence_json={"sampled_frames": [0, 12, 24]},
            validated_by_user_id=reviewer.id if visual_validator == "HUMAN" else None,
        )
        session.add_all([policy_validation, visual_validation])
        session.flush()
        human_validation = None
        if include_human_override:
            human_validation = CharacterStateValidation(
                project_id=project.id,
                state_delta_id=delta.id,
                stage="HUMAN_OVERRIDE",
                attempt=1,
                decision="PASS",
                validator_kind="HUMAN",
                validated_target_hash=delta.target_state_hash,
                evidence_hash="4" * 64,
                evidence_json={"explicit_confirmation": True},
                validated_by_user_id=reviewer.id,
            )
            session.add(human_validation)
            session.flush()
        commit = CharacterStateCommit(
            project_id=project.id,
            character_id=character.id,
            timeline_scope_key=timeline_scope_key,
            shot_id=shot.id,
            candidate_id=candidate.id,
            state_delta_id=delta.id,
            to_state_version_id=state_version.id,
            policy_validation_id=policy_validation.id,
            visual_validation_id=visual_validation.id,
            human_validation_id=human_validation.id if human_validation else None,
            expected_head_version=0,
            commit_actor="SYSTEM",
            reason="policy and visual evidence passed",
            commit_hash="2" * 64,
        )
        session.add(commit)
        session.flush()
        head = CharacterStateHead(
            project_id=project.id,
            character_id=character.id,
            timeline_scope_key=timeline_scope_key,
            state_version_id=state_version.id,
            lock_version=1,
        )
        session.add(head)
        session.flush()
        return {
            "character_id": character.id,
            "identity_id": identity.id,
            "shot_id": shot.id,
            "candidate_id": candidate.id,
            "input_state_id": input_state.id,
            "output_state_id": output_state.id,
            "delta_id": delta.id,
            "version_id": state_version.id,
            "head_id": head.id,
        }


def test_character_state_commit_is_versioned_and_append_only(container, project) -> None:  # type: ignore[no-untyped-def]
    ids = _committed_initial_state(container, project)
    with container.database.session() as session:
        head = session.get(CharacterStateHead, ids["head_id"])
        version = session.get(CharacterStateVersion, ids["version_id"])
        assert head.lock_version == 1
        assert head.state_version_id == version.id
        assert version.narrative_state_json["props"]["flare"]["state"] == "unlit"

    with pytest.raises(IntegrityError, match="append-only"):
        with container.database.session() as session:
            session.execute(
                update(CharacterStateVersion)
                .where(CharacterStateVersion.id == ids["version_id"])
                .values(narrative_state_json={"props": {"flare": {"state": "lit"}}})
            )

    with pytest.raises(IntegrityError, match="append-only"):
        with container.database.session() as session:
            session.execute(delete(CharacterStateDelta).where(CharacterStateDelta.id == ids["delta_id"]))

    # The head fence is the one guard that deliberately does *not* raise a
    # constraint violation on PostgreSQL: it declares SQLSTATE 40001
    # (serialization_failure), because a stale fence means "someone else
    # committed first, re-read and retry", not "this data is invalid".
    # SQLAlchemy maps that to OperationalError, while SQLite's RAISE(ABORT)
    # can carry no SQLSTATE at all and always arrives as IntegrityError. The
    # message is the part both engines agree on.
    with pytest.raises((IntegrityError, OperationalError), match="fresh commit"):
        with container.database.session() as session:
            session.execute(
                update(CharacterStateHead)
                .where(CharacterStateHead.id == ids["head_id"])
                .values(lock_version=2)
            )

    with container.database.session() as session:
        stale_cas = session.execute(
            update(CharacterStateHead)
            .where(CharacterStateHead.id == ids["head_id"], CharacterStateHead.lock_version == 0)
            .values(lock_version=2)
        )
        assert stale_cas.rowcount == 0


@pytest.mark.parametrize(
    ("path", "patch_value", "proposed_state"),
    [
        ("/canonical_hair", "red", {"canonical_hair": "red"}),
        ("/appearance/hair", "red", {"appearance": {"hair": "red"}}),
        (
            "/appearance",
            {"injury": {"right_brow": {"status": "healed"}}},
            {"appearance": {"injury": {"right_brow": {"status": "healed"}}}},
        ),
        (
            "/appearance/outfit",
            {"damage": {"left_sleeve": "repaired"}},
            {"appearance": {"outfit": {"damage": {"left_sleeve": "repaired"}}}},
        ),
    ],
)
def test_character_state_delta_rejects_identity_mutation(
    container,
    project,
    path: str,
    patch_value: object,
    proposed_state: dict[str, object],
) -> None:  # type: ignore[no-untyped-def]
    ids = _committed_initial_state(container, project)
    with pytest.raises(IntegrityError, match="delta is inconsistent"):
        with container.database.session() as session:
            session.add(
                CharacterStateDelta(
                    project_id=project.id,
                    character_id=ids["character_id"],
                    timeline_scope_key="main",
                    shot_id=ids["shot_id"],
                    candidate_id=ids["candidate_id"],
                    base_state_version_id=ids["version_id"],
                    identity_version_id=ids["identity_id"],
                    input_timeline_state_id=ids["input_state_id"],
                    planned_output_timeline_state_id=ids["output_state_id"],
                    proposal_revision=2,
                    supersedes_delta_id=ids["delta_id"],
                    proposal_kind="NARRATIVE",
                    source_kind="RULES",
                    patch_json=[{"op": "replace", "path": path, "value": patch_value}],
                    changed_paths_json=[path],
                    proposed_state_json=proposed_state,
                    base_state_hash="b" * 64,
                    target_state_hash="3" * 64,
                    input_timeline_state_hash="c" * 64,
                    planned_output_timeline_state_hash="d" * 64,
                    target_version=2,
                    idempotency_key=f"mira-shot-13-illegal-identity:{path}",
                )
            )


@pytest.mark.parametrize(
    "narrative_state",
    [
        {"appearance": {"hair": "red"}},
        {"appearance": {"outfit": {"type": "different jacket"}}},
    ],
)
def test_character_state_version_rejects_nested_identity_state(
    container,
    project,
    narrative_state: dict[str, object],
) -> None:  # type: ignore[no-untyped-def]
    ids = _committed_initial_state(container, project)
    with pytest.raises(IntegrityError, match="version is inconsistent"):
        with container.database.session() as session:
            session.add(
                CharacterStateVersion(
                    project_id=project.id,
                    character_id=ids["character_id"],
                    timeline_scope_key="main",
                    version=2,
                    previous_state_version_id=ids["version_id"],
                    identity_version_id=ids["identity_id"],
                    narrative_state_json=narrative_state,
                    identity_fingerprint="e" * 64,
                    previous_state_hash="b" * 64,
                    state_hash="5" * 64,
                )
            )


def test_character_state_delta_allows_mutable_appearance_descendants(container, project) -> None:  # type: ignore[no-untyped-def]
    ids = _committed_initial_state(container, project)
    with container.database.session() as session:
        delta = CharacterStateDelta(
            project_id=project.id,
            character_id=ids["character_id"],
            timeline_scope_key="main",
            shot_id=ids["shot_id"],
            candidate_id=ids["candidate_id"],
            base_state_version_id=ids["version_id"],
            identity_version_id=ids["identity_id"],
            input_timeline_state_id=ids["input_state_id"],
            planned_output_timeline_state_id=ids["output_state_id"],
            proposal_revision=2,
            supersedes_delta_id=ids["delta_id"],
            proposal_kind="NARRATIVE",
            source_kind="RULES",
            patch_json=[
                {
                    "op": "add",
                    "path": "/appearance/injury/right_brow",
                    "value": {"status": "unhealed", "blood_state": "dried"},
                },
                {
                    "op": "add",
                    "path": "/appearance/outfit/damage/left_sleeve",
                    "value": "torn",
                },
            ],
            changed_paths_json=[
                "/appearance/injury/right_brow",
                "/appearance/outfit/damage/left_sleeve",
            ],
            proposed_state_json={
                "appearance": {
                    "injury": {"right_brow": {"status": "unhealed", "blood_state": "dried"}},
                    "outfit": {"damage": {"left_sleeve": "torn"}},
                }
            },
            base_state_hash="b" * 64,
            target_state_hash="6" * 64,
            input_timeline_state_hash="c" * 64,
            planned_output_timeline_state_hash="d" * 64,
            target_version=2,
            idempotency_key="mira-shot-13-mutable-appearance",
        )
        session.add(delta)
        session.flush()
        assert delta.id


def test_character_state_visual_validation_accepts_human_baseline(container, project) -> None:  # type: ignore[no-untyped-def]
    ids = _committed_initial_state(
        container,
        project,
        visual_validator="HUMAN",
    )
    with container.database.session() as session:
        validation = session.scalar(
            select(CharacterStateValidation).where(
                CharacterStateValidation.state_delta_id == ids["delta_id"],
                CharacterStateValidation.stage == "VISUAL",
            )
        )
        assert validation is not None
        assert validation.validator_kind == "HUMAN"
        assert validation.validated_by_user_id is not None


def test_character_state_review_can_commit_only_with_human_override(container, project) -> None:  # type: ignore[no-untyped-def]
    ids = _committed_initial_state(
        container,
        project,
        visual_decision="REVIEW_REQUIRED",
        include_human_override=True,
    )
    with container.database.session() as session:
        commit = session.scalar(
            select(CharacterStateCommit).where(CharacterStateCommit.state_delta_id == ids["delta_id"])
        )
        assert commit is not None
        assert commit.human_validation_id is not None


def test_character_state_review_without_override_is_not_committable(container, project) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(IntegrityError, match="commit is inconsistent"):
        _committed_initial_state(
            container,
            project,
            visual_decision="REVIEW_REQUIRED",
        )
