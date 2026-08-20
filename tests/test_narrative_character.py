from __future__ import annotations

import io

import pytest
from character_core import IdentityLocked
from production_domain.models import (
    Character,
    CharacterIdentityVersion,
    Episode,
    Project,
    Scene,
    Shot,
    TimelineState,
)
from sqlalchemy import func, select


def test_narrative_compiler_creates_events_shots_and_state_chain(container, project):
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="Pilot",
            episode_number=1,
            script_source="""EXT. HOTEL CANOPY - NIGHT
ZhaoKai: Look at this phone.
LinJin turns and walks two steps toward the canopy edge.
""",
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    result = container.narrative.compile_episode(episode_id)
    assert len(result.scene_ids) == 1
    assert len(result.shot_ids) == 2
    assert result.entities["characters"] == ["LinJin", "ZhaoKai"]
    with container.database.session() as session:
        first = session.get(Shot, result.shot_ids[0])
        second = session.get(Shot, result.shot_ids[1])
        first_output = session.get(TimelineState, first.output_state_id)
        second_input = session.get(TimelineState, second.input_state_id)
        assert first.next_shot_id == second.id
        assert second.previous_shot_id == first.id
        assert second_input.previous_state_id == first_output.id
        assert first_output.state_json["last_event"] == "speak"
        assert session.scalar(
            select(Character).where(Character.project_id == project.id, Character.name == "ZhaoKai")
        )


def test_narrative_recompile_is_idempotent(container, project):
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="Pilot",
            episode_number=1,
            script_source="LinJin turns toward the door.",
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    first = container.narrative.compile_episode(episode_id)
    second = container.narrative.compile_episode(episode_id)
    assert second.shot_ids == first.shot_ids


def test_character_identity_version_is_locked_and_versioned(container, project):
    asset, _ = container.media.register(
        project.id,
        "CHARACTER_MASTER",
        io.BytesIO(b"canonical-character"),
        filename="character.png",
        mime_type="image/png",
    )
    character = container.characters.create_character(project.id, "Lin Jin")
    version = container.characters.confirm_identity(
        character.id,
        asset.id,
        hair_signature="black shoulder-length hair",
        costume_signature="blue delivery jacket",
    )
    assert version.version == 1
    assert version.status == "LOCKED"
    with pytest.raises(IdentityLocked):
        container.characters.update_locked_version(version.id, {"hair_signature": "red hair"})
    version_two = container.characters.confirm_identity(character.id, asset.id)
    assert version_two.version == 2
    binding = container.characters.binding(character.id)
    assert binding["identity_version_id"] == version_two.id


def test_character_identity_rejects_cross_project_reference_assets(container, project):
    master, _ = container.media.register(
        project.id,
        "CHARACTER_MASTER",
        io.BytesIO(b"canonical-character"),
        filename="character.png",
        mime_type="image/png",
    )
    with container.database.session() as session:
        other_project = Project(title="Other tenant project")
        session.add(other_project)
        session.flush()
        other_project_id = other_project.id
    foreign_reference, _ = container.media.register(
        other_project_id,
        "CHARACTER_REFERENCE",
        io.BytesIO(b"foreign-profile"),
        filename="profile.png",
        mime_type="image/png",
    )
    character = container.characters.create_character(project.id, "Lin Jin")

    with pytest.raises(ValueError, match="different project"):
        container.characters.confirm_identity(
            character.id,
            master.id,
            references={"left_profile_asset_id": foreign_reference.id},
        )

    with container.database.session() as session:
        assert (
            session.scalar(
                select(func.count(CharacterIdentityVersion.id)).where(
                    CharacterIdentityVersion.character_id == character.id
                )
            )
            == 0
        )


def test_scene_and_shot_have_authoritative_states(container, project):
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Pilot", episode_number=1, script_source="One action")
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Room")
        session.add(scene)
        session.flush()
        state = TimelineState(project_id=project.id, episode_id=episode.id, scene_id=scene.id)
        session.add(state)
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=1,
            prompt="One action",
            user_prompt="One action",
            input_state_id=state.id,
            output_state_id=state.id,
        )
        session.add(shot)
        session.flush()
        assert shot.input_state_id == state.id
