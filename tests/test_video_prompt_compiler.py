from __future__ import annotations

import json
import uuid

from production_domain.models import Episode, Scene, Shot, TimelineState


def test_compiler_resolves_uuid_keyed_characters_and_preserves_prop_maps(container, project):
    character_id = str(uuid.uuid4())
    identity_version_id = str(uuid.uuid4())
    prop_id = str(uuid.uuid4())
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Mira", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Platform three")
        session.add(scene)
        session.flush()
        start = TimelineState(
            project_id=project.id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_INPUT",
            state_json={
                "characters": {
                    character_id: {
                        "name": "Mira Okonkwo",
                        "position": "support column",
                        "orientation": "toward tunnel",
                        "gaze_target": "dark tunnel mouth",
                    }
                },
                "props": {
                    prop_id: {
                        "name": "signal flare",
                        "state": "unlit",
                        "holder": character_id,
                    }
                },
            },
        )
        end = TimelineState(
            project_id=project.id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_OUTPUT",
            state_json={"characters": {character_id: {"position": "tunnel edge"}}},
        )
        session.add_all([start, end])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=13,
            shot_type="ACTION",
            prompt="Mira walks toward the tunnel edge.",
            input_state_id=start.id,
            output_state_id=end.id,
        )
        session.add(shot)
        session.flush()
        shot_id = shot.id

    compiled = container.video_prompt_compiler.compile(
        shot_id,
        character_bindings=[
            {
                "character_id": character_id,
                "identity_version_id": identity_version_id,
                "name": "Mira Okonkwo",
                "hair_signature": "short braids with silver highlights",
                "costume_signature": "charcoal field jacket, torn left sleeve",
            }
        ],
    )

    assert len(compiled.spec.subjects) == 1
    subject = compiled.spec.subjects[0]
    assert subject.name == "Mira Okonkwo"
    assert subject.asset_id == character_id
    assert subject.asset_version_id == identity_version_id
    assert subject.eyeline_target == "dark tunnel mouth"
    assert compiled.spec.props == [
        {
            "asset_id": prop_id,
            "name": "signal flare",
            "state": "unlit",
            "holder": character_id,
        }
    ]
    assert json.loads(compiled.neutral_prompt)["props"] == compiled.spec.props
