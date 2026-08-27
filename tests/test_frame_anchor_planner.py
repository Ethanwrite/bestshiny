"""The Frame Anchor Planner: one frame-strategy decision per adjacent shot pair.

Derived from structured rows — transitions, timeline states, declared
dependencies — and executed through the existing ContinuityDecisionEngine and
GenerationPolicyEngine, which stay untouched.
"""

from __future__ import annotations

import pytest
from continuity_core import FrameAnchorStrategy
from production_domain.models import (
    AssetKind,
    Character,
    CharacterIdentityVersion,
    ContinuityMode,
    DecisionRecord,
    Episode,
    Location,
    MediaAsset,
    Shot,
    ShotStatus,
    TimelineTransition,
    TimelineTransitionType,
)
from sqlalchemy import select

SCRIPT = """INT. KITCHEN - DAY
LinJin picks up the phone.
LinJin turns toward the door.
INT. HALLWAY - NIGHT
LinJin walks toward the door.
"""


def _compile(container, project, script: str = SCRIPT, *, episode_number: int = 1):  # type: ignore[no-untyped-def]
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


def test_every_adjacent_pair_gets_exactly_one_decision(container, project):  # type: ignore[no-untyped-def]
    episode_id, result = _compile(container, project)
    first, second, third = result.shot_ids

    plans = container.frame_anchors.plan_episode(episode_id)
    assert [plan.target_shot_id for plan in plans] == [first, second, third]

    # First shot of the episode: nothing to inherit, reconstruct. This project
    # owns no canonical references, so the mode downgrades to a fresh start
    # rather than demanding canon that does not exist.
    assert plans[0].strategy == FrameAnchorStrategy.RECONSTRUCT_FIRST_FRAME
    assert plans[0].source_shot_id is None
    assert "FIRST_SHOT" in plans[0].reasons
    assert "NO_CANONICAL_CHARACTER_REFERENCE" in plans[0].reasons
    assert plans[0].continuity_mode == ContinuityMode.NONE.value
    assert plans[0].requires_keyframe_generation is False

    # Continuous same-scene pair with a declared STATE_INHERITANCE: inherit.
    assert plans[1].strategy == FrameAnchorStrategy.INHERIT_LAST_FRAME
    assert plans[1].continuity_mode == ContinuityMode.HARD_CONTINUITY.value
    assert plans[1].source_shot_id == first
    assert "EXPLICIT_STATE_INHERITANCE" in plans[1].reasons
    assert plans[1].transition_type == TimelineTransitionType.CONTINUOUS.value

    # Scene cut: reconstruct, again downgraded for want of canon.
    assert plans[2].strategy == FrameAnchorStrategy.RECONSTRUCT_FIRST_FRAME
    assert plans[2].continuity_mode == ContinuityMode.NONE.value
    assert plans[2].transition_type == TimelineTransitionType.SCENE_CUT.value
    assert "SCENE_CHANGE" in plans[2].reasons

    with container.database.session() as session:
        modes = {
            shot_id: session.get(Shot, shot_id).continuity_mode
            for shot_id in (first, second, third)
        }
        assert modes[first] == ContinuityMode.NONE.value
        assert modes[second] == ContinuityMode.HARD_CONTINUITY.value
        assert modes[third] == ContinuityMode.NONE.value
        records = list(
            session.scalars(
                select(DecisionRecord).where(
                    DecisionRecord.decision_type == "FRAME_ANCHOR_PLAN",
                    DecisionRecord.shot_id.in_([first, second, third]),
                )
            )
        )
        assert len(records) == 3
        transition = session.scalar(
            select(TimelineTransition).where(TimelineTransition.target_shot_id == second)
        )
        assert transition.metadata_json["frame_anchor_plan"]["strategy"] == (
            FrameAnchorStrategy.INHERIT_LAST_FRAME
        )


def test_reconstruction_names_the_characters_and_scene_that_anchor_the_frame(container, project):  # type: ignore[no-untyped-def]
    """With real canon, RE_ANCHOR stands and names its anchors precisely."""

    episode_id, result = _compile(container, project)

    with container.database.session() as session:
        character = session.scalar(
            select(Character).where(
                Character.project_id == project.id, Character.name == "LinJin"
            )
        )
        master = MediaAsset(
            project_id=project.id,
            asset_type="CHARACTER_MASTER",
            sha256="0" * 64,
            storage_key="tests/linjin-master.png",
            mime_type="image/png",
        )
        session.add(master)
        session.flush()
        identity = CharacterIdentityVersion(
            character_id=character.id,
            version=1,
            master_asset_id=master.id,
        )
        session.add(identity)
        session.flush()
        character.current_identity_version_id = identity.id
        character_id = character.id
        master_id = master.id
        identity_id = identity.id

        hallway = session.scalar(
            select(Location).where(
                Location.project_id == project.id, Location.name == "HALLWAY"
            )
        )
        plate = MediaAsset(
            project_id=project.id,
            asset_type="LOCATION_REFERENCE",
            sha256="2" * 64,
            storage_key="tests/hallway-plate.png",
            mime_type="image/png",
        )
        session.add(plate)
        session.flush()
        hallway_id = hallway.id
        plate_id = plate.id

    # Canonical promotion goes through the registry — a trigger refuses a
    # canonical_version_id written without its promotion record.
    scene_asset = container.asset_registry.create(
        project.id,
        AssetKind.SCENE,
        "Hallway master plate",
        canonical_metadata={"location_id": hallway_id},
    )
    scene_version = container.asset_registry.add_version(
        scene_asset.id, primary_media_asset_id=plate_id
    )
    container.asset_registry.promote(
        scene_asset.id, scene_version.id, reason="approved location plate"
    )
    scene_asset_id = scene_asset.id

    plans = container.frame_anchors.plan_episode(episode_id)
    reconstruct = plans[2]
    # Full canon exists, so the reconstruction is a real canonical re-anchor.
    assert reconstruct.continuity_mode == ContinuityMode.RE_ANCHOR.value
    assert reconstruct.requires_keyframe_generation is True
    assert [subject.character_id for subject in reconstruct.anchor_subjects] == [character_id]
    assert reconstruct.anchor_subjects[0].identity_version_id == identity_id
    assert reconstruct.anchor_subjects[0].master_asset_id == master_id
    assert reconstruct.scene_asset_id == scene_asset_id
    # The inherit decision carries no anchor set: the previous frame is the anchor.
    assert plans[1].anchor_subjects == ()


def test_timeline_breaks_force_reconstruction_even_within_a_scene(container, project):  # type: ignore[no-untyped-def]
    episode_id, result = _compile(container, project)
    second = result.shot_ids[1]
    with container.database.session() as session:
        transition = session.scalar(
            select(TimelineTransition).where(TimelineTransition.target_shot_id == second)
        )
        transition.transition_type = TimelineTransitionType.FLASHBACK.value

    plan = container.frame_anchors.plan_pair(second)
    assert plan.strategy == FrameAnchorStrategy.RECONSTRUCT_FIRST_FRAME
    assert "FLASHBACK" in plan.reasons
    # No canon in this project, so the mode is a fresh start rather than a
    # canonical re-anchor — but nothing is inherited either way.
    assert plan.continuity_mode == ContinuityMode.NONE.value


def test_inherit_wires_the_previous_end_frame_when_it_exists(container, project):  # type: ignore[no-untyped-def]
    episode_id, result = _compile(container, project)
    first, second, _ = result.shot_ids
    with container.database.session() as session:
        end_frame = MediaAsset(
            project_id=project.id,
            asset_type="END_FRAME",
            sha256="1" * 64,
            storage_key="tests/shot-one-end.jpg",
            mime_type="image/jpeg",
        )
        session.add(end_frame)
        session.flush()
        session.get(Shot, first).end_frame_asset_id = end_frame.id
        end_frame_id = end_frame.id

    plan = container.frame_anchors.plan_pair(second)
    assert plan.strategy == FrameAnchorStrategy.INHERIT_LAST_FRAME
    with container.database.session() as session:
        assert session.get(Shot, second).start_frame_asset_id == end_frame_id


def test_committed_shots_are_never_replanned(container, project):  # type: ignore[no-untyped-def]
    episode_id, result = _compile(container, project)
    second = result.shot_ids[1]
    with container.database.session() as session:
        shot = session.get(Shot, second)
        shot.status = ShotStatus.COMMITTED.value

    plans = container.frame_anchors.plan_episode(episode_id)
    assert second not in [plan.target_shot_id for plan in plans]
    with pytest.raises(ValueError, match="committed"):
        container.frame_anchors.plan_pair(second)


def test_script_compilation_plans_frame_anchors_in_one_operation(container, project):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="Episode 1",
            episode_number=1,
            script_source=SCRIPT,
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id

    result = container.orchestrator.compile_episode(episode_id)
    plans = result.detail["frame_anchor_plans"]
    assert len(plans) == 3
    assert plans[1]["strategy"] == FrameAnchorStrategy.INHERIT_LAST_FRAME
    assert plans[2]["strategy"] == FrameAnchorStrategy.RECONSTRUCT_FIRST_FRAME
    with container.database.session() as session:
        shot = session.get(Shot, plans[1]["target_shot_id"])
        assert shot.continuity_mode == ContinuityMode.HARD_CONTINUITY.value
