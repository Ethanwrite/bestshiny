"""Structural gate for the twelve installed Skill bodies.

Skill text is authored by the user, one Skill at a time. These tests never
assert what a Skill should say — that is the user's decision — only that every
installed body satisfies the invariants ``SkillRegistry`` and the platform
depend on, so a submitted Skill fails here rather than at runtime.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from skill_core import SkillRegistry

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
EXPECTED_SKILLS = (
    "camera-movement",
    "character-consistency",
    "cinematography",
    "commercial",
    "composition",
    "continuity",
    "director",
    "image-prompt-corrector",
    "lighting",
    "model-prompting",
    "prompt-compiler",
    "short-drama",
)


@pytest.fixture(scope="module")
def installed():  # type: ignore[no-untyped-def]
    return SkillRegistry(SKILLS_ROOT).list_skills()


def test_every_installed_skill_parses_under_the_shared_registry(installed) -> None:  # type: ignore[no-untyped-def]
    assert tuple(sorted(skill.name for skill in installed)) == EXPECTED_SKILLS


def test_skill_directories_and_declared_names_agree() -> None:
    for directory in sorted(SKILLS_ROOT.glob("*/")):
        body = directory / "SKILL.md"
        assert body.is_file(), f"{directory.name} has no SKILL.md"
        # SkillRegistry resolves by directory glob, so a mismatch would make a
        # Skill unresolvable by the name every caller uses.
        assert SkillRegistry(SKILLS_ROOT).resolve(directory.name).path == str(body)


def test_installed_skills_expose_the_metadata_the_api_returns(installed) -> None:  # type: ignore[no-untyped-def]
    for skill in installed:
        assert skill.description.strip(), f"{skill.name} has an empty description"
        assert skill.system_prompt.strip(), f"{skill.name} has an empty body"
        assert skill.category.strip(), f"{skill.name} has an empty category"
        assert skill.version.startswith("sha256:")
        assert len(skill.content_hash) == 64


def test_skill_versions_are_content_addressed_and_unique(installed) -> None:  # type: ignore[no-untyped-def]
    hashes = {skill.name: skill.content_hash for skill in installed}
    assert len(set(hashes.values())) == len(hashes), "two Skills share a content hash"
    # Resolving twice must return the same version; the audit record stores it.
    again = SkillRegistry(SKILLS_ROOT).resolve("prompt-compiler")
    assert again.content_hash == hashes["prompt-compiler"]


def test_frontmatter_is_never_leaked_into_the_system_prompt(installed) -> None:  # type: ignore[no-untyped-def]
    for skill in installed:
        assert not skill.system_prompt.startswith("---")
        assert f"name: {skill.name}" not in skill.system_prompt


def test_prompt_compiler_stays_resolvable_for_the_compiler(container) -> None:  # type: ignore[no-untyped-def]
    """The one Skill on an execution path must always resolve and record a hash."""

    contract = container.prompts.skill_contract()
    assert contract["name"] == "prompt-compiler"
    assert contract["content_hash"] == container.skills.resolve("prompt-compiler").content_hash
    assert contract["input_schema"]["title"] == "PromptCompilerInput"
    assert contract["output_schema"]["title"] == "PromptCompilerOutput"
