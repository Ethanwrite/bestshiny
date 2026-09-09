"""A script-compiled first shot presents its actor, and the thing it acts on.

The narrative compiler adds a character (and a prop) to the timeline only when
it acts, so the first shot of a scripted episode has an empty input cast and
its actor exists only in the output state. The envelope carried ``subjects=[]``
and ``props=[]`` for exactly that shot, and the prompt-compiler Skill rightly
refused to compile an empty cast. These tests pin the fallback: the actor and
the prop come from the output state, the compilation record says so (the
prompt itself carries no claim about where they were at the shot's start), the
timeline state is not rewritten, and nothing is added on top of a start state
that has a cast or of explicit character bindings.
"""

from __future__ import annotations

import json
import uuid

from production_domain.models import Episode, PromptCompilation, Scene, Shot, TimelineState

EPISODE_SCRIPT = "INT. Rooftop - NIGHT\nMira picks up the phone.\nMira: You finally came."


def _compiled_episode(container, project) -> list[str]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="EP01", episode_number=1, script_source=EPISODE_SCRIPT)
        session.add(episode)
        session.flush()
        episode_id = episode.id
    result = container.orchestrator.compile_episode(episode_id)
    return list(result.detail["shot_ids"])


def _provenance_lines(constraints: list[str]) -> list[str]:
    """Any prompt-facing claim about where a derived subject or prop came from or started."""

    markers = ("entered by this shot", "present at the shot's start", "first recorded")
    return [item for item in constraints if any(marker in item for marker in markers)]


def test_the_first_shot_takes_its_actor_and_prop_from_the_output_state(container, project):  # type: ignore[no-untyped-def]
    first_shot_id, second_shot_id = _compiled_episode(container, project)
    spec = container.prompts._envelope(first_shot_id).spec

    # The timeline state is read, not rewritten: the input state still has no cast.
    assert spec.start_state["characters"] == {} and spec.start_state["props"] == {}
    assert [subject.name for subject in spec.subjects] == ["Mira"]
    subject = spec.subjects[0]
    assert subject.asset_id in spec.end_state["characters"]
    assert subject.eyeline_target.strip()
    assert subject.eyeline_target == spec.end_state["characters"][subject.asset_id]["gaze_target"]
    assert subject.screen_position == "midground_center" and subject.body_orientation == "scene_partner"
    assert subject.identity_constraints == []
    # No prompt-facing claim about the subject's motion or the prop's place at
    # the shot's start: provenance lives on the record, not in the frame.
    assert _provenance_lines(spec.constraints) == []
    envelope = container.prompts._envelope(first_shot_id)
    assert envelope.derived_from_output_state == {"subjects": ["Mira"], "props": ["phone"]}

    phones = [prop for prop in spec.props if prop.get("name") == "phone"]
    assert len(phones) == 1 and len(spec.props) == 1
    phone = phones[0]
    assert phone["asset_id"] in spec.end_state["props"]
    assert phone["holder"] == subject.asset_id
    assert "state" not in phone

    # The second shot already has Mira in its input state: one Mira, nothing
    # derived, and the prop rides through as the state recorded it.
    second = container.prompts._envelope(second_shot_id)
    assert [subject.name for subject in second.spec.subjects] == ["Mira"]
    assert second.derived_from_output_state == {}
    assert [prop["name"] for prop in second.spec.props] == ["phone"]
    assert "state" not in second.spec.props[0]


def test_the_first_shot_compiles_with_its_subject_in_the_neutral_prompt(container, project):  # type: ignore[no-untyped-def]
    first_shot_id = _compiled_episode(container, project)[0]
    compiled = container.prompts.compile(first_shot_id)
    assert compiled.output.status == "COMPILED"
    prompt = json.loads(compiled.neutral_prompt)
    assert [subject["name"] for subject in prompt["subjects"]] == ["Mira"]
    assert prompt["subjects"][0]["eyeline_target"].strip()
    assert [prop["name"] for prop in prompt["props"]] == ["phone"]
    assert _provenance_lines(prompt["constraints"]) == []
    assert prompt["start_state"]["characters"] == {}
    assert any(item.startswith("subject_identity=Mira:") for item in compiled.output.qc_checklist)
    with container.database.session() as session:
        record = session.get(PromptCompilation, compiled.record_id)
        assert record.diff_json["derived_from_output_state"] == {"subjects": ["Mira"], "props": ["phone"]}


def test_character_bindings_keep_their_own_fallback_for_an_empty_start_state(container, project):  # type: ignore[no-untyped-def]
    first_shot_id = _compiled_episode(container, project)[0]
    character_id = str(uuid.uuid4())
    spec = container.prompts._envelope(
        first_shot_id,
        character_bindings=[{"character_id": character_id, "name": "Mira Okonkwo"}],
    ).spec
    assert [(subject.name, subject.asset_id) for subject in spec.subjects] == [("Mira Okonkwo", character_id)]
    assert _provenance_lines(spec.constraints) == []


def test_a_start_state_with_a_cast_is_not_widened_from_the_end_state(container, project):  # type: ignore[no-untyped-def]
    present = str(uuid.uuid4())
    newcomer = str(uuid.uuid4())
    prop_id = str(uuid.uuid4())
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Mira", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, description="Rooftop")
        session.add(scene)
        session.flush()
        start = TimelineState(
            project_id=project.id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_INPUT",
            state_json={"characters": {present: {"name": "Mira", "gaze_target": "the door"}}, "props": {}},
        )
        end = TimelineState(
            project_id=project.id,
            episode_id=episode.id,
            scene_id=scene.id,
            state_kind="SHOT_OUTPUT",
            state_json={
                "characters": {present: {"name": "Mira"}, newcomer: {"name": "Theo"}},
                "props": {prop_id: {"name": "phone", "state": "ringing", "holder": present}},
            },
        )
        session.add_all([start, end])
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            sequence=2,
            shot_type="ACTION",
            prompt="Mira picks up the phone.",
            input_state_id=start.id,
            output_state_id=end.id,
        )
        session.add(shot)
        session.flush()
        shot_id = shot.id

    spec = container.prompts._envelope(shot_id).spec
    assert [(subject.name, subject.eyeline_target) for subject in spec.subjects] == [("Mira", "the door")]
    assert _provenance_lines(spec.constraints) == []
    # An empty start prop map still reads the end state, and a row's own state stands.
    assert spec.props == [{"name": "phone", "state": "ringing", "holder": present, "asset_id": prop_id}]
