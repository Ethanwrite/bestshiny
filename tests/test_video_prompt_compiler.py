from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from platform_contracts import PromptCompilerInput, PromptCompilerOutput, PromptContinuityContext
from production_domain.models import Episode, PromptCompilation, Scene, Shot, TimelineState
from skill_core import PromptCompilerService, SkillRegistry
from video_prompt_core import VideoShotPromptCompiler


def _write_skill(path: Path, body: str) -> None:
    skill_dir = path / "prompt-compiler"
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: prompt-compiler\n"
        "description: Compile approved canonical shots.\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


def test_skill_registry_parses_frontmatter_and_versions_content(tmp_path):
    _write_skill(tmp_path, "# Prompt Compiler\n\nFirst approved contract.")
    registry = SkillRegistry(tmp_path)
    first = registry.resolve("prompt-compiler")

    assert first.name == "prompt-compiler"
    assert first.description == "Compile approved canonical shots."
    assert first.system_prompt.startswith("# Prompt Compiler")
    assert "name: prompt-compiler" not in first.system_prompt
    assert first.version.startswith("sha256:")

    _write_skill(tmp_path, "# Prompt Compiler\n\nSecond approved contract.")
    second = registry.resolve("prompt-compiler")
    assert second.version != first.version
    assert second.content_hash != first.content_hash


def test_prompt_compiler_output_rejects_partial_failed_prompt():
    with pytest.raises(ValueError, match="cannot contain partial prompts"):
        PromptCompilerOutput(
            status="NOT_COMPILABLE",
            positive_prompt="unsafe partial prompt",
            review_reason="missing approved end state",
        )


def test_unified_compiler_returns_structured_failure_for_invalid_shot(container):
    result = container.prompts.compile_input(
        PromptCompilerInput(
            shot_spec={"aspect_ratio": "9:16"},
            asset_bindings=[],
            continuity_context=PromptContinuityContext(),
        )
    )

    assert result.status == "NOT_COMPILABLE"
    assert result.positive_prompt is None
    assert result.negative_prompt is None
    assert result.missing_fields == ["dominant_action", "intent"]
    assert result.review_reason


def test_legacy_video_compiler_name_is_the_unified_service(container):
    assert VideoShotPromptCompiler is PromptCompilerService
    assert container.video_prompt_compiler is container.prompts
    contract = container.prompts.skill_contract()
    assert contract["version"] == container.skills.resolve("prompt-compiler").version
    assert contract["input_schema"]["title"] == "PromptCompilerInput"
    assert contract["output_schema"]["title"] == "PromptCompilerOutput"
    assert contract["system_prompt"].startswith("# Prompt Compiler")


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
    assert compiled.output.status == "COMPILED"
    assert compiled.output.positive_prompt == compiled.neutral_prompt
    assert compiled.skill_version == container.skills.resolve("prompt-compiler").version
    with container.database.session() as session:
        record = session.get(PromptCompilation, compiled.record_id)
        assert record.skill_versions == {"prompt-compiler": compiled.skill_version}
        assert record.diff_json["prompt_compiler_output"]["status"] == "COMPILED"
