"""The Skill runtime: registered is not resolved, resolved is not invoked, invoked is not Skill-driven.

What these tests pin, one per defect class the runtime exists to prevent:

1. every installed Skill declares its binding machine-readably (role, stage,
   operations, model role, authority, forbidden authority, the ten sections);
2. each runtime operation resolves to exactly one Skill, and the platform's
   expected role for it - never the whole catalogue, never the Director by
   default;
3. an invocation injects the bound Skill's body alone, under its model role,
   and records the exact version and content hash of what was injected;
4. every fallback is tracked - resolved / loaded / model_invoked /
   execution_mode / fallback_reason - and never labelled as the Skill's work;
5. authority boundaries hold: the Director's shot and photographic fields are
   stripped, the Shot Planner cannot change a line or invent a character,
   Cinematography cannot add an action or name a provider, Continuity cannot
   redesign a frame, the Compiler cannot resolve an unresolved field;
6. the five runtime-bound Skills reach a model through their real call sites
   (creative turn, story, shot plan, cinematography, continuity, compilation)
   with their bodies, and the seven reference Skills are reported as bound to
   nothing;
7. the Director's invariant classes are versioned: a revision that moves one
   records what it supersedes and never rewrites the previous row.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from creative_director_core.schemas import StoryDraft
from creative_director_core.screenplay import (
    deterministic_shot_plan,
    invariant_record,
    merge_story_and_shot_plan,
    shot_plan_violations,
    story_from_screenplay,
    validate_shot_plan,
    validate_story,
)
from fastapi.testclient import TestClient
from platform_contracts import PromptCompilerInput, PromptContinuityContext
from production_domain.models import (
    CreativeScreenplayRevision,
    DecisionRecord,
    Episode,
    PromptCompilation,
    Shot,
)
from provider_sdk import ProviderError
from skill_core import (
    CALL_SITES,
    EXPECTED_BINDINGS,
    REQUIRED_SECTIONS,
    AuthorityViolation,
    SkillOperation,
    SkillRegistry,
    SkillRegistryError,
    SkillRuntime,
    SkillRuntimeError,
)
from sqlalchemy import select
from test_creative_director import (
    RICH_IDEA,
    SCREENPLAY,
    ScriptedDirector,
    _approve_brief,
    _client,
    _execution,
    _latest_state_block,
    _rich_turn,
    _start,
    _state,
)
from video_platform_api.main import create_app

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
RUNTIME_SKILLS = {
    "director": "director",
    "short-drama": "shot_planner",
    "cinematography": "cinematography",
    "continuity": "continuity",
    "prompt-compiler": "prompt_compiler",
}
REFERENCE_SKILLS = (
    "camera-movement",
    "character-consistency",
    "commercial",
    "composition",
    "image-prompt-corrector",
    "lighting",
    "model-prompting",
)
EPISODE_SCRIPT = "INT. Rooftop - NIGHT\nMira picks up the phone.\nMira: You finally came."


@pytest.fixture(scope="module")
def registry() -> SkillRegistry:
    return SkillRegistry(SKILLS_ROOT)


@pytest.fixture(scope="module")
def bodies(registry: SkillRegistry) -> dict[str, str]:
    """A distinctive probe of every installed body, to count injections."""

    return {skill.name: skill.system_prompt[:400] for skill in registry.list_skills()}


def _bodies_in(text: str, bodies: dict[str, str]) -> set[str]:
    return {name for name, probe in bodies.items() if probe in text}


class StageModel:
    """A model double for the stage Skills: answers by role from a handler."""

    def __init__(self, handler):  # type: ignore[no-untyped-def]
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    async def execute_chat(self, project_id, role, *, messages, parameters=None, **_extra):  # type: ignore[no-untyped-def]
        self.calls.append(
            {
                "project_id": project_id,
                "role": str(role),
                "messages": list(messages),
                "parameters": parameters,
            }
        )
        request = json.loads(messages[-1]["content"])
        payload = self.handler(str(role), request)
        if isinstance(payload, Exception):
            raise payload
        return _execution(payload, record_id=f"exec-stage-{len(self.calls)}")


def _cinematography_plan(framing: str = "wide establishing") -> dict[str, Any]:
    return {
        "camera": {
            "framing": framing,
            "angle": "low angle",
            "height": "knee height",
            "position": "behind Mira, three metres back",
            "dominant_movement": "slow dolly in",
            "speed": "eases in",
            "path": "toward the ledge",
            "focus": "Mira",
            "screen_axis": "A",
            "lens_intent": "natural perspective",
            "depth_of_field": "deep",
        },
        "lighting": {
            "direction": "city glow from below, camera left",
            "quality": "soft",
            "contrast": "high",
            "color_temperature": "teal night with an amber practical",
            "practicals": ["phone screen"],
            "motivation": "the city below",
            "exposure_intent": "protect the phone screen",
        },
        "subject_positions": {"Mira": "centre, midground"},
        "atmosphere": "rain",
        "start_composition": "Mira small against the skyline",
        "end_composition": "Mira at the ledge, phone glowing",
        "continuity_checks": ["axis A held", "eyeline to the phone"],
        "unresolved": [],
    }


def _continuity_pass() -> dict[str, Any]:
    return {
        "verdict": "PASS",
        "matched_state": ["characters.mira.costume", "scene.location"],
        "mismatches": [],
        "evidence": ["timeline states compared under CONTINUOUS"],
        "minimal_repair": None,
        "approval_required": False,
        "unresolved": [],
    }


def _compiled_package(
    envelope: dict[str, Any], *, drop_action: bool = False, extra: str = ""
) -> dict[str, Any]:
    spec = envelope["shot_spec"]
    names = [subject["name"] for subject in spec["subjects"]]
    positive = (
        ", ".join(names)
        + (". " if names else "")
        + ("a quiet moment on the rooftop." if drop_action else spec["dominant_action"])
        + (f' Line: "{spec["dialogue"]}"' if spec.get("dialogue") else "")
        + f" Camera: {spec['camera']['framing']}, {spec['camera']['dominant_movement']}."
        + extra
    )
    return {
        "status": "COMPILED",
        "positive_prompt": positive,
        "negative_prompt": "identity drift, a second action, an unintended cut",
        "asset_bindings": list(dict.fromkeys(envelope["asset_bindings"])),
        "continuity_assertions": [f"holds: {item}" for item in envelope["continuity_context"]["facts"]],
        "qc_checklist": ["one action", "one movement"],
        "missing_fields": [],
        "review_reason": None,
    }


def _compiled_episode(container, project) -> tuple[str, list[str]]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="EP01", episode_number=1, script_source=EPISODE_SCRIPT)
        session.add(episode)
        session.flush()
        episode_id = episode.id
    result = container.orchestrator.compile_episode(episode_id)
    return episode_id, list(result.detail["shot_ids"])


# ------------------------------------------------------------ 1. registry
def test_every_installed_skill_declares_a_machine_readable_binding(registry: SkillRegistry) -> None:
    installed = {skill.name: skill for skill in registry.list_skills()}
    assert set(installed) == set(RUNTIME_SKILLS) | set(REFERENCE_SKILLS)
    for name, role in RUNTIME_SKILLS.items():
        skill = installed[name]
        assert skill.runtime_kind == "model" and skill.role == role and skill.operations, name
        assert skill.model_role, f"{name} declares no model role"
        assert skill.authority and skill.forbidden_authority, name
        assert not set(skill.authority) & set(skill.forbidden_authority), name
        assert skill.output_contract, name
    for name in REFERENCE_SKILLS:
        skill = installed[name]
        assert skill.runtime_kind == "reference" and not skill.operations and not skill.is_runtime, name
        assert skill.forbidden_authority, name
    for skill in installed.values():
        assert skill.missing_sections == (), f"{skill.name} lacks {skill.missing_sections}"
        assert [section for section in skill.sections if section in REQUIRED_SECTIONS] == list(
            REQUIRED_SECTIONS
        ), f"{skill.name}: sections out of the contract's order"


def test_the_platform_boundaries_are_declared_not_only_written(registry: SkillRegistry) -> None:
    director = registry.resolve("director")
    for forbidden in ("shot_count", "lens", "lighting", "camera_movement", "model", "provider"):
        assert forbidden in director.forbidden_authority
    planner = registry.resolve("short-drama")
    assert {"dialogue_text", "ending", "identity", "framing", "lens", "model"} <= set(
        planner.forbidden_authority
    )
    cinematography = registry.resolve("cinematography")
    assert {"plot", "action", "dialogue", "provider"} <= set(cinematography.forbidden_authority)
    assert {"lighting", "framing", "camera_movement"} <= set(cinematography.authority)
    compiler = registry.resolve("prompt-compiler")
    assert {"unresolved_creative_fields", "model", "provider"} <= set(compiler.forbidden_authority)
    for name in ("lighting", "camera-movement", "composition"):
        assert registry.resolve(name).bound_to == "cinematography"


# ----------------------------------------------------------- 2. resolution
def test_each_operation_resolves_to_exactly_one_skill_with_the_expected_role(registry: SkillRegistry) -> None:
    runtime = SkillRuntime(registry)
    assert runtime.validate() == []
    seen: dict[str, str] = {}
    for operation, role in EXPECTED_BINDINGS.items():
        definition = runtime.resolve(operation)
        assert definition.role == role
        assert definition.is_runtime
        seen[operation.value] = definition.name
    assert seen == {
        "creative_conversation": "director",
        "story_generation": "director",
        "story_revision": "director",
        "shot_decomposition": "short-drama",
        "shot_revision": "short-drama",
        "cinematography_design": "cinematography",
        "continuity_review": "continuity",
        "prompt_compilation": "prompt-compiler",
    }
    # Loading is by operation only: no operation loads every Skill, and no
    # creative task falls through to the Director.
    assert runtime.resolve(SkillOperation.SHOT_DECOMPOSITION).name != "director"
    assert runtime.resolve(SkillOperation.CINEMATOGRAPHY_DESIGN).name != "director"


def test_a_duplicate_binding_is_refused(tmp_path: Path) -> None:
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(
            "---\n"
            f"name: {name}\n"
            "description: claims the same operation\n"
            "metadata:\n"
            "  role: cinematography\n"
            "  runtime: model\n"
            "  model_role: CINEMATOGRAPHY_REASONING\n"
            "  operations: [cinematography_design]\n"
            "---\n\n# Body\n",
            encoding="utf-8",
        )
    registry = SkillRegistry(tmp_path)
    assert registry.validate_bindings()
    with pytest.raises(SkillRegistryError, match="more than one Skill"):
        registry.resolve_operation("cinematography_design")


def test_a_reference_skill_cannot_claim_a_runtime_operation(tmp_path: Path) -> None:
    (tmp_path / "ref").mkdir()
    (tmp_path / "ref" / "SKILL.md").write_text(
        "---\nname: ref\ndescription: reference\nmetadata:\n  runtime: reference\n"
        "  operations: [continuity_review]\n---\n\n# Body\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillRegistryError, match="reference Skill cannot claim"):
        SkillRegistry(tmp_path).list_skills()


# ------------------------------------------------------------ 3. injection
@pytest.mark.asyncio
async def test_an_invocation_injects_only_the_bound_skill_body_under_its_role(
    registry: SkillRegistry, bodies: dict[str, str]
) -> None:
    model = StageModel(lambda role, request: _cinematography_plan())
    runtime = SkillRuntime(registry, model)
    invocation = await runtime.invoke(
        SkillOperation.CINEMATOGRAPHY_DESIGN,
        project_id="p1",
        protocol="## protocol\nanswer JSON",
        messages=[{"role": "user", "content": json.dumps({"task": "DESIGN_SHOT"})}],
        validator=lambda raw: raw,
    )
    call = model.calls[0]
    assert call["role"] == "CINEMATOGRAPHY_REASONING"
    system = call["messages"][0]
    assert system["role"] == "system"
    assert _bodies_in(system["content"], bodies) == {"cinematography"}
    assert system["content"].startswith(registry.resolve("cinematography").system_prompt)
    assert system["content"].endswith("answer JSON")
    definition = registry.resolve("cinematography")
    assert (invocation.name, invocation.version, invocation.content_hash, invocation.role) == (
        "cinematography",
        definition.version,
        definition.content_hash,
        "cinematography",
    )
    assert invocation.resolved and invocation.loaded and invocation.model_invoked
    assert invocation.execution_mode == "MODEL" and invocation.skill_driven
    assert invocation.fallback_reason is None
    assert invocation.reason_codes[0] == "SKILL_LOADED" and invocation.reason_codes[-1] == "MODEL_REPLY"
    assert invocation.output == _cinematography_plan()
    assert runtime.ledger["cinematography_design"] == {
        "total": 1,
        "model": 1,
        "model_without_skill": 0,
        "deterministic": 0,
        "body_injected": 1,
    }


@pytest.mark.asyncio
async def test_injecting_a_second_skill_body_is_refused(registry: SkillRegistry) -> None:
    runtime = SkillRuntime(registry, StageModel(lambda role, request: {}))
    smuggled = registry.resolve("director").system_prompt
    with pytest.raises(SkillRuntimeError, match="one operation loads one Skill"):
        await runtime.invoke(
            SkillOperation.CONTINUITY_REVIEW,
            project_id="p1",
            protocol="p",
            messages=[{"role": "user", "content": smuggled}],
            validator=lambda raw: raw,
        )


# ------------------------------------------------------------- 4. fallback
@pytest.mark.asyncio
async def test_every_fallback_is_tracked_and_never_skill_driven(registry: SkillRegistry) -> None:
    unconfigured = SkillRuntime(registry, None)
    request = [{"role": "user", "content": "{}"}]
    not_configured = await unconfigured.invoke(
        SkillOperation.CONTINUITY_REVIEW,
        project_id="p",
        protocol="p",
        messages=request,
        validator=lambda raw: raw,
    )
    assert not_configured.resolved and not not_configured.loaded and not not_configured.model_invoked
    assert not_configured.execution_mode == "DETERMINISTIC"
    assert not_configured.fallback_reason == "MODEL_RUNTIME_NOT_CONFIGURED"
    assert not_configured.version == registry.resolve("continuity").version
    assert not not_configured.skill_driven and not not_configured.retryable

    from production_domain.models import RetryCategory

    outage = SkillRuntime(
        registry,
        StageModel(lambda role, request: ProviderError("503", RetryCategory.PROVIDER_BUSY, code="UP")),
    )
    unavailable = await outage.invoke(
        SkillOperation.CONTINUITY_REVIEW,
        project_id="p",
        protocol="p",
        messages=request,
        validator=lambda raw: raw,
    )
    assert unavailable.loaded and not unavailable.model_invoked
    assert unavailable.fallback_reason == "MODEL_UNAVAILABLE" and unavailable.retryable
    assert unavailable.execution_mode == "DETERMINISTIC" and not unavailable.skill_driven
    assert "ProviderError" in unavailable.reason_codes

    garbage = SkillRuntime(registry, StageModel(lambda role, request: {"verdict": "MAYBE"}))
    invalid = await garbage.invoke(
        SkillOperation.CONTINUITY_REVIEW,
        project_id="p",
        protocol="p",
        messages=request,
        validator=lambda raw: (_ for _ in ()).throw(ValueError("verdict MAYBE is not a verdict")),
    )
    assert invalid.model_invoked and invalid.raw == {"verdict": "MAYBE"}
    assert invalid.fallback_reason == "MODEL_OUTPUT_INVALID" and not invalid.skill_driven
    assert invalid.validation_errors == ["verdict MAYBE is not a verdict"]
    assert invalid.execution_record_id == "exec-stage-1"

    def refuse(raw: dict[str, Any]) -> Any:
        raise AuthorityViolation(["camera", "dialogue"])

    breach = await garbage.invoke(
        SkillOperation.CONTINUITY_REVIEW, project_id="p", protocol="p", messages=request, validator=refuse
    )
    assert breach.fallback_reason == "AUTHORITY_VIOLATION"
    assert breach.authority_violations == ["camera", "dialogue"]
    assert "AUTHORITY_VIOLATION:camera" in breach.reason_codes and not breach.skill_driven

    disabled = SkillRuntime(registry, StageModel(lambda role, request: {}), enabled=False)
    off = await disabled.invoke(
        SkillOperation.CONTINUITY_REVIEW,
        project_id="p",
        protocol="p",
        messages=request,
        validator=lambda raw: raw,
    )
    assert off.fallback_reason == "SKILL_STAGE_DISABLED" and not off.loaded

    for invocation in (not_configured, unavailable, invalid, breach, off):
        assert invocation.as_json()["skill_driven"] is False
        assert invocation.as_json()["execution_mode"] == "DETERMINISTIC"


@pytest.mark.asyncio
async def test_a_missing_skill_runs_the_generic_prompt_without_being_skill_driven(tmp_path: Path) -> None:
    model = StageModel(lambda role, request: {"assistant_message": "hi", "brief_operations": []})
    runtime = SkillRuntime(SkillRegistry(tmp_path / "nothing-installed"), model)
    invocation = await runtime.invoke(
        SkillOperation.CREATIVE_CONVERSATION,
        project_id="p",
        protocol="## turn protocol",
        messages=[{"role": "user", "content": "{}"}],
        validator=lambda raw: raw,
        fallback_system_prompt="You are a director.",
    )
    assert invocation.reason_codes[0] == "SKILL_UNAVAILABLE"
    assert not invocation.resolved and not invocation.loaded and invocation.model_invoked
    assert invocation.execution_mode == "MODEL_WITHOUT_SKILL" and not invocation.skill_driven
    assert invocation.name is None and invocation.version is None
    assert model.calls[0]["role"] == "DIRECTOR"
    assert model.calls[0]["messages"][0]["content"] == "You are a director.\n\n## turn protocol"
    refused = await runtime.invoke(
        SkillOperation.CINEMATOGRAPHY_DESIGN,
        project_id="p",
        protocol="p",
        messages=[{"role": "user", "content": "{}"}],
        validator=lambda raw: raw,
    )
    assert refused.fallback_reason == "SKILL_UNAVAILABLE" and not refused.model_invoked


# ----------------------------------------------- 5. authority: director
def test_the_director_cannot_decide_shots_lens_light_model_or_provider() -> None:
    story_payload = story_from_screenplay(SCREENPLAY)
    story_payload["beats"][0]["shots"] = SCREENPLAY["beats"][0]["shots"]
    story_payload["beats"][0]["lens"] = "85mm"
    story_payload["lighting"] = {"key": "hard"}
    story_payload["provider"] = "openrouter"
    story_payload["model"] = "veo"
    story, stripped = validate_story(story_payload)
    assert isinstance(story, StoryDraft)
    assert sorted(stripped) == ["beats[0].lens", "beats[0].shots", "lighting", "model", "provider"]
    dumped = story.model_dump(by_alias=True)
    assert "lighting" not in dumped and "provider" not in dumped and "shots" not in dumped["beats"][0]
    # The dialogue the Director wrote survives, in order, as required lines.
    assert [line["text"] for line in dumped["beats"][1]["dialogue"]] == [
        "This isn't mine. Who put my name on it?",
        "You did, Mira. Three days from now.",
    ]


# -------------------------------------------- 5. authority: shot planner
def test_the_shot_planner_cannot_change_a_line_invent_a_character_or_frame_a_shot() -> None:
    story, _ = validate_story(story_from_screenplay(SCREENPLAY))
    plan_payload = json.loads(json.dumps(SCREENPLAY))
    plan_payload = {
        "beats": [{"sequence": b["sequence"], "shots": b["shots"]} for b in plan_payload["beats"]]
    }
    plan, stripped = validate_shot_plan({**plan_payload, "ending": "she jumps", "camera": {"lens": "35mm"}})
    assert "ending" in stripped and "camera" in stripped
    # The fixture's WIDE/CLOSE are shot sizes: Cinematography's, stripped and named.
    assert any(path.endswith(".shot_type") for path in stripped)
    assert shot_plan_violations(story, plan) == []
    merged = merge_story_and_shot_plan(story, plan)
    assert [shot.shot_type for shot in merged.beats[0].shots] == ["MEDIUM", "MEDIUM"]
    assert [shot.shot_type for shot in merged.beats[1].shots] == ["DIALOGUE", "DIALOGUE"]

    reworded = json.loads(json.dumps(plan_payload))
    reworded["beats"][1]["shots"][0]["dialogue"]["text"] = "This isn't mine."
    plan2, _ = validate_shot_plan(reworded)
    assert shot_plan_violations(story, plan2) == ["dialogue_changed:beat 2"]

    dropped = json.loads(json.dumps(plan_payload))
    dropped["beats"][1]["shots"] = dropped["beats"][1]["shots"][:1]
    plan3, _ = validate_shot_plan(dropped)
    assert shot_plan_violations(story, plan3) == ["dialogue_dropped:beat 2"]

    invented = json.loads(json.dumps(plan_payload))
    invented["beats"][2]["shots"][0]["action"]["actor"] = "Kai"
    plan4, _ = validate_shot_plan(invented)
    assert "unknown_character:Kai" in shot_plan_violations(story, plan4)

    unplanned = json.loads(json.dumps(plan_payload))
    unplanned["beats"] = unplanned["beats"][:2]
    plan5, _ = validate_shot_plan(unplanned)
    assert shot_plan_violations(story, plan5) == ["beat_unplanned:3"]

    fallback = deterministic_shot_plan(story, reason="MODEL_UNAVAILABLE")
    screenplay = merge_story_and_shot_plan(story, fallback)
    assert [len(beat.shots) for beat in screenplay.beats] == [1, 2, 1]
    assert screenplay.beats[1].shots[0].dialogue.text == "This isn't mine. Who put my name on it?"
    assert any("DETERMINISTIC SHOT PLAN" in item for item in screenplay.unresolved)


def test_a_rewording_shot_planner_is_rejected_whole_and_the_screenplay_says_so(container, project):  # type: ignore[no-untyped-def]
    def rewording(request: dict[str, Any]) -> dict[str, Any]:
        plan = json.loads(json.dumps(SCREENPLAY))
        beats = [{"sequence": b["sequence"], "shots": b["shots"]} for b in plan["beats"]]
        beats[1]["shots"][1]["dialogue"]["text"] = "You did. Three days from now."
        return {"beats": beats, "required_copy": [], "mobile_hook_check": "", "unresolved": []}

    container.creative_director.model_roles = ScriptedDirector(_rich_turn, shot_plan=rewording)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        approved = _approve_brief(client, started["session_id"], started["brief_revision"])
    screenplay = approved["screenplay"]
    assert screenplay["reasoner"] == "MODEL:DIRECTOR+DETERMINISTIC:SHOT_PLANNER"
    assert screenplay["deterministic"] is True and screenplay["skill_driven"] is False
    assert "SHOT_PLANNER:AUTHORITY_VIOLATION" in screenplay["reason_codes"]
    assert "SHOT_PLANNER:AUTHORITY_VIOLATION:dialogue_changed:beat 2" in screenplay["reason_codes"]
    shots = screenplay["skill_invocations"]["shots"]
    assert shots["name"] == "short-drama" and shots["role"] == "shot_planner"
    assert shots["model_invoked"] and shots["execution_mode"] == "DETERMINISTIC"
    assert shots["fallback_reason"] == "AUTHORITY_VIOLATION"
    story = screenplay["skill_invocations"]["story"]
    assert story["name"] == "director" and story["skill_driven"] is True
    # The Director's line stands, unreworded, in the fallback plan.
    lines = [
        shot["dialogue"]["text"]
        for beat in screenplay["content"]["beats"]
        for shot in beat["shots"]
        if shot.get("dialogue")
    ]
    assert "You did, Mira. Three days from now." in lines
    with _client(container) as client:
        refused = client.post(
            f"/v1/creative/sessions/{started['session_id']}/screenplay/approve",
            json={"revision": screenplay["revision"]},
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["reason_code"] == "DETERMINISTIC_SCREENPLAY_UNCONFIRMED"


def test_director_shot_fields_are_stripped_on_record_and_never_reach_the_screenplay(container, project):  # type: ignore[no-untyped-def]
    def overreaching(request: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(json.dumps(SCREENPLAY))
        payload["camera"] = {"lens": "50mm"}
        payload["provider"] = "openrouter"
        return payload

    director = ScriptedDirector(_rich_turn, overreaching)
    container.creative_director.model_roles = director
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        approved = _approve_brief(client, started["session_id"], started["brief_revision"])
    screenplay = approved["screenplay"]
    assert screenplay["reasoner"] == "MODEL:DIRECTOR+MODEL:SHOT_PLANNER" and screenplay["skill_driven"]
    assert {"AUTHORITY_STRIPPED:camera", "AUTHORITY_STRIPPED:provider"} <= set(screenplay["reason_codes"])
    assert "camera" not in screenplay["content"] and "provider" not in screenplay["content"]
    story = screenplay["skill_invocations"]["story"]
    assert story["authority_violations"] == ["stripped:camera", "stripped:provider"]


# ------------------------------------------ 5. authority: cinematography
def test_cinematography_cannot_add_an_action_a_line_or_a_provider() -> None:
    from director_production import validate_plan

    with pytest.raises(AuthorityViolation) as excinfo:
        validate_plan({**_cinematography_plan(), "dominant_action": "she jumps"})
    assert excinfo.value.violations == ["dominant_action"]
    with pytest.raises(AuthorityViolation):
        validate_plan({**_cinematography_plan(), "lighting": {"direction": "left", "provider": "kling"}})
    with pytest.raises(ValueError, match="one dominant camera movement"):
        validate_plan({**_cinematography_plan(), "camera": {"dominant_movement": "dolly in and orbit"}})
    plan = validate_plan(_cinematography_plan())
    assert plan.camera_values()["framing"] == "wide establishing"
    assert plan.lighting_values()["practicals"] == ["phone screen"]


@pytest.mark.asyncio
async def test_the_cinematography_stage_designs_a_shot_and_the_compiler_renders_it(  # type: ignore[no-untyped-def]
    container, project, bodies
):
    _episode_id, shot_ids = _compiled_episode(container, project)
    calls = StageModel(lambda role, request: _cinematography_plan())
    container.skill_runtime.model_roles = calls
    designed = await container.cinematography_designer.design_shot(shot_ids[0])
    call = calls.calls[0]
    assert call["role"] == "CINEMATOGRAPHY_REASONING"
    assert _bodies_in(call["messages"][0]["content"], bodies) == {"cinematography"}
    request = json.loads(call["messages"][-1]["content"])
    assert request["task"] == "DESIGN_SHOT" and request["shot"]["action_line"].startswith("Mira")
    # Canonical assets travel as ids, never as description.
    assert "canonical_bindings" in request and "look" not in json.dumps(request["shot"])
    skill = container.skills.resolve("cinematography")
    assert designed["skill_driven"] and designed["execution_mode"] == "MODEL"
    assert designed["skill_invocation"]["version"] == skill.version
    assert designed["skill_invocation"]["content_hash"] == skill.content_hash
    assert designed["shot_type"] == "WIDE"
    with container.database.session() as session:
        shot = session.get(Shot, shot_ids[0])
        assert shot.cinematography_json["plan"]["camera"]["framing"] == "wide establishing"
        assert shot.shot_type == "WIDE"
        decision = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.shot_id == shot_ids[0], DecisionRecord.decision_type == "CINEMATOGRAPHY_DESIGN"
            )
        )
        assert decision.selected_action == "MODEL" and decision.model_version == skill.version
    compiled = container.prompts.compile(shot_ids[0])
    assert compiled.spec.camera.framing == "wide establishing"
    assert compiled.spec.camera.dominant_movement == "slow dolly in"
    assert compiled.spec.lighting.direction == "city glow from below, camera left"
    assert compiled.spec.lighting.practicals == ["phone screen"]
    with container.database.session() as session:
        record = session.get(PromptCompilation, compiled.record_id)
        assert record.diff_json["cinematography"]["execution_mode"] == "MODEL"
        assert record.diff_json["cinematography"]["skill_version"] == skill.version


@pytest.mark.asyncio
async def test_a_cinematography_plan_that_rewrites_the_action_falls_back_on_record(container, project):  # type: ignore[no-untyped-def]
    _episode_id, shot_ids = _compiled_episode(container, project)
    container.skill_runtime.model_roles = StageModel(
        lambda role, request: {**_cinematography_plan(), "action": "Mira throws the phone"}
    )
    designed = await container.cinematography_designer.design_shot(shot_ids[0])
    assert designed["skill_driven"] is False and designed["execution_mode"] == "DETERMINISTIC"
    assert designed["skill_invocation"]["fallback_reason"] == "AUTHORITY_VIOLATION"
    assert designed["skill_invocation"]["authority_violations"] == ["action"]
    # The narrative compiler's own shot kind is untouched by a fallback.
    assert designed["shot_type"] == "ACTION"
    assert any("DETERMINISTIC DEFAULTS" in item for item in designed["plan"]["unresolved"])
    compiled = container.prompts.compile(shot_ids[0])
    assert compiled.spec.camera.dominant_movement == "locked-off"
    with container.database.session() as session:
        shot = session.get(Shot, shot_ids[0])
        assert shot.prompt.startswith("Mira picks up the phone")  # the action is untouched


# ---------------------------------------------- 5. authority: continuity
def test_continuity_cannot_redesign_a_frame_or_rewrite_an_action() -> None:
    from continuity_core import deterministic_review, validate_review

    with pytest.raises(AuthorityViolation) as excinfo:
        validate_review({**_continuity_pass(), "camera": {"framing": "close-up"}})
    assert excinfo.value.violations == ["camera"]
    with pytest.raises(AuthorityViolation):
        validate_review({**_continuity_pass(), "minimal_repair": "x", "model": "veo"})
    assert validate_review(_continuity_pass()).verdict == "PASS"

    same = {
        "scene": {"location": "Rooftop", "time": "NIGHT"},
        "characters": {"m": {"costume": "canonical", "position": "left"}},
    }
    assert deterministic_review(same, same, transition="CONTINUOUS").verdict == "PASS"
    moved = {
        "scene": {"location": "Rooftop", "time": "NIGHT"},
        "characters": {"m": {"costume": "canonical", "position": "right"}},
    }
    repairable = deterministic_review(same, moved, transition="CONTINUOUS")
    assert repairable.verdict == "REPAIRABLE" and "restore characters.m.position to left" in (
        repairable.minimal_repair or ""
    )
    elsewhere = {
        "scene": {"location": "Subway", "time": "NIGHT"},
        "characters": {"m": {"costume": "torn", "position": "left"}},
    }
    escalated = deterministic_review(same, elsewhere, transition="CONTINUOUS")
    assert escalated.verdict == "ESCALATE" and escalated.approval_required
    assert {item.field for item in escalated.mismatches} == {"scene.location", "characters.m.costume"}
    # A declared location change resets spatial state; wardrobe still carries.
    cut = deterministic_review(same, elsewhere, transition="LOCATION_CHANGE")
    assert {item.field for item in cut.mismatches} == {"characters.m.costume"}
    assert deterministic_review(same, elsewhere, transition="FLASHBACK").verdict == "PASS"


@pytest.mark.asyncio
async def test_the_continuity_stage_reviews_each_handoff_under_its_skill(container, project, bodies):  # type: ignore[no-untyped-def]
    _episode_id, shot_ids = _compiled_episode(container, project)
    calls = StageModel(lambda role, request: _continuity_pass())
    container.skill_runtime.model_roles = calls
    first = await container.continuity_reviewer.review_pair(shot_ids[0])
    assert first["from_shot_id"] is None and first["review"]["verdict"] == "PASS"
    assert first["skill_invocation"]["fallback_reason"] == "NO_PREVIOUS_SHOT" and not first["skill_driven"]
    assert calls.calls == []
    second = await container.continuity_reviewer.review_pair(shot_ids[1])
    call = calls.calls[0]
    assert call["role"] == "CONTINUITY_REASONER"
    assert _bodies_in(call["messages"][0]["content"], bodies) == {"continuity"}
    request = json.loads(call["messages"][-1]["content"])
    assert request["task"] == "REVIEW_HANDOFF"
    assert request["from_shot"]["id"] == shot_ids[0] and request["to_shot"]["id"] == shot_ids[1]
    assert request["transition"]["type"] == "CONTINUOUS"
    skill = container.skills.resolve("continuity")
    assert second["skill_driven"] and second["review"]["verdict"] == "PASS"
    assert second["skill_invocation"]["version"] == skill.version
    with container.database.session() as session:
        decision = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.shot_id == shot_ids[1], DecisionRecord.decision_type == "CONTINUITY_REVIEW"
            )
        )
        assert decision.selected_action == "PASS" and decision.model_version == skill.version
        assert decision.input_features["skill_invocation"]["content_hash"] == skill.content_hash
    container.skill_runtime.model_roles = StageModel(
        lambda role, request: {**_continuity_pass(), "lighting": {"direction": "left"}}
    )
    breached = await container.continuity_reviewer.review_pair(shot_ids[1])
    assert not breached["skill_driven"] and breached["execution_mode"] == "DETERMINISTIC"
    assert breached["skill_invocation"]["fallback_reason"] == "AUTHORITY_VIOLATION"
    assert breached["review"]["verdict"] in {"PASS", "REPAIRABLE", "ESCALATE"}
    assert any("DETERMINISTIC REVIEW" in item for item in breached["review"]["unresolved"])


# ------------------------------------------------ 5. authority: compiler
def test_the_compiler_refuses_to_resolve_an_unresolved_field(container):  # type: ignore[no-untyped-def]
    def envelope(**spec: Any) -> PromptCompilerInput:
        base = {
            "intent": "Mira picks up the phone",
            "dominant_action": "Mira picks up the phone",
            "subjects": [{"name": "Mira", "eyeline_target": "the phone"}],
            "start_state": {"a": 1},
            "end_state": {"a": 2},
        }
        return PromptCompilerInput(
            shot_spec={**base, **spec}, asset_bindings=[], continuity_context=PromptContinuityContext()
        )

    assert container.prompts.compile_input(envelope()).status == "COMPILED"
    unresolved = container.prompts.compile_input(
        envelope(subjects=[{"name": "Mira", "eyeline_target": "UNRESOLVED"}])
    )
    assert unresolved.status == "NOT_COMPILABLE"
    assert unresolved.missing_fields == ["subjects[0].eyeline_target"]
    assert unresolved.positive_prompt is None
    sequenced = container.prompts.compile_input(
        envelope(dominant_action="Mira picks up the phone and then answers it")
    )
    assert sequenced.status == "NOT_COMPILABLE" and sequenced.missing_fields == ["dominant_action"]
    two_moves = container.prompts.compile_input(envelope(camera={"dominant_movement": "dolly in and pan"}))
    assert two_moves.missing_fields == ["camera.dominant_movement"]
    marked = container.prompts.compile_input(envelope(constraints=["unresolved: which hand holds the phone"]))
    assert marked.status == "NOT_COMPILABLE" and marked.missing_fields == ["constraints[0]"]
    assert "unresolved" in (marked.review_reason or "")


@pytest.mark.asyncio
async def test_the_compiler_skill_package_is_re_verified_and_reused_by_the_generation_path(  # type: ignore[no-untyped-def]
    container, project, bodies
):
    _episode_id, shot_ids = _compiled_episode(container, project)
    shot_id = shot_ids[0]
    skill = container.skills.resolve("prompt-compiler")

    naming = StageModel(
        lambda role, request: _compiled_package(request["envelope"], extra=" Render on Kling.")
    )
    container.skill_runtime.model_roles = naming
    result = await container.prompts.compile_shot_with_skill(shot_id)
    assert naming.calls[0]["role"] == "PROMPT_COMPILER"
    assert _bodies_in(naming.calls[0]["messages"][0]["content"], bodies) == {"prompt-compiler"}
    assert result.execution_mode == "DETERMINISTIC" and not result.skill_driven
    assert result.skill_invocation["fallback_reason"] == "AUTHORITY_VIOLATION"
    assert result.skill_invocation["authority_violations"] == ["provider_or_model_named:Kling"]
    assert result.output.status == "COMPILED"  # the deterministic package stood in

    dropping = StageModel(lambda role, request: _compiled_package(request["envelope"], drop_action=True))
    container.skill_runtime.model_roles = dropping
    result = await container.prompts.compile_shot_with_skill(shot_id)
    assert result.skill_invocation["fallback_reason"] == "MODEL_OUTPUT_INVALID"
    assert "dominant_action_missing" in result.skill_invocation["validation_errors"][0]

    faithful = StageModel(lambda role, request: _compiled_package(request["envelope"]))
    container.skill_runtime.model_roles = faithful
    result = await container.prompts.compile_shot_with_skill(shot_id)
    assert result.execution_mode == "MODEL" and result.skill_driven
    assert result.skill_version == skill.version
    assert result.skill_invocation["content_hash"] == skill.content_hash
    assert result.output.positive_prompt.startswith("Mira")
    with container.database.session() as session:
        record = session.get(PromptCompilation, result.record_id)
        assert record.skill_versions == {"prompt-compiler": skill.version}
        assert record.diff_json["execution_mode"] == "MODEL" and record.diff_json["skill_driven"]
        assert record.diff_json["skill_invocation"]["model_invoked"] is True
        assert session.get(Shot, shot_id).compiled_prompt == result.output.positive_prompt

    # The synchronous generation path reuses the Skill's fresh package as the
    # Skill's work, and records the reuse.
    reused = container.prompts.compile(shot_id)
    assert reused.execution_mode == "MODEL" and reused.skill_driven
    assert reused.output.positive_prompt == result.output.positive_prompt
    with container.database.session() as session:
        record = session.get(PromptCompilation, reused.record_id)
        assert record.diff_json["reused_from"] == result.record_id
    assert len(faithful.calls) == 1

    # Change the shot and the package is stale: deterministic, on record.
    with container.database.session() as session:
        session.get(Shot, shot_id).user_prompt = "Mira puts down the phone."
        session.flush()
    stale = container.prompts.compile(shot_id)
    assert stale.execution_mode == "DETERMINISTIC" and not stale.skill_driven
    assert stale.skill_invocation["fallback_reason"] == "NO_FRESH_SKILL_COMPILATION"
    assert stale.skill_invocation["version"] == skill.version and not stale.skill_invocation["loaded"]

    # The Skill's NOT_COMPILABLE verdict is honoured, not overridden.
    refusing = StageModel(
        lambda role, request: {
            "status": "NOT_COMPILABLE",
            "missing_fields": ["end_state"],
            "review_reason": "the end state is not reachable by the single action",
        }
    )
    container.skill_runtime.model_roles = refusing
    verdict = await container.prompts.compile_shot_with_skill(shot_id)
    assert verdict.output.status == "NOT_COMPILABLE" and verdict.skill_driven
    with pytest.raises(ValueError, match="prompt compiler Skill judged this shot not compilable"):
        container.prompts.compile(shot_id)


# ------------------------------------------------- 6. call sites and matrix
def test_the_creative_stages_run_after_beats_approval_and_the_matrix_says_what_ran(  # type: ignore[no-untyped-def]
    container, project, bodies
):
    from test_creative_director import _ready_anchors_without_generation, _user

    def stage_handler(role: str, request: dict[str, Any]) -> dict[str, Any]:
        if role == "CINEMATOGRAPHY_REASONING":
            return _cinematography_plan()
        if role == "CONTINUITY_REASONER":
            return _continuity_pass()
        if role == "PROMPT_COMPILER":
            return _compiled_package(request["envelope"])
        raise AssertionError(role)

    stages = StageModel(stage_handler)
    container.skill_runtime.model_roles = stages
    director = ScriptedDirector(_rich_turn)
    container.creative_director.model_roles = director
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        view = _state(client, session_id)
        client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/approve",
            json={"revision": view["screenplay"]["revision"]},
        ).raise_for_status()
    _ready_anchors_without_generation(container, session_id, project.id)
    with _client(container) as client:
        bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose").json()
    container.creative_director.approve_bible(
        session_id,
        version=bible["version"],
        actor="locker",
        actor_user_id=_user(container, "locker@example.com"),
    )
    with _client(container) as client:
        client.post(f"/v1/creative/sessions/{session_id}/beats/propose").raise_for_status()
        compiled = client.post(f"/v1/creative/sessions/{session_id}/beats/approve", json={"plan_revision": 1})
        assert compiled.status_code == 200, compiled.text
        report = compiled.json()["skill_stages"]
        matrix = client.get("/v1/skills/runtime").json()
        listing = client.get("/v1/skills").json()
    shots = len(compiled.json()["shot_ids"])
    assert report["summary"] == {
        "cinematography": {"total": shots, "skill_driven": shots},
        "continuity": {"total": shots, "skill_driven": shots - 1},
        "prompt_compilation": {"total": shots, "skill_driven": shots},
    }
    assert report["errors"] == []
    # Every model call - director turns, story, shot plan, the three stages -
    # carried exactly one Skill body: the one its operation binds to.
    roles = {"DIRECTOR": "director", "SHOT_PLANNER": "short-drama"}
    for call in director.calls:
        assert _bodies_in(call["messages"][0]["content"], bodies) == {roles[str(call["role"])]}
    stage_roles = {
        "CINEMATOGRAPHY_REASONING": "cinematography",
        "CONTINUITY_REASONER": "continuity",
        "PROMPT_COMPILER": "prompt-compiler",
    }
    assert {call["role"] for call in stages.calls} == set(stage_roles)
    for call in stages.calls:
        assert _bodies_in(call["messages"][0]["content"], bodies) == {stage_roles[call["role"]]}
    assert matrix["problems"] == []
    rows = {row["skill"]: row for row in matrix["matrix"]}
    integrated = {name for name, row in rows.items() if row["integrated"]}
    assert integrated == set(RUNTIME_SKILLS)
    for name in RUNTIME_SKILLS:
        row = rows[name]
        assert row["registered"] and row["runtime_bound"] and row["body_injected"] and row["fallback_tracked"]
        assert row["structured_validated"]
        assert all(entry["body_injected"] >= 1 for entry in row["observed"].values())
    for name in REFERENCE_SKILLS:
        row = rows[name]
        assert row["registered"] and not row["runtime_bound"] and not row["body_injected"]
        assert row["structured_validated"] == [] and not row["fallback_tracked"] and not row["integrated"]
    assert rows["cinematography"]["structured_validated"] == ["CinematographyPlan"]
    assert rows["director"]["structured_validated"] == ["DirectorTurnResult", "StoryDraft"]
    by_name = {item["name"]: item for item in listing}
    assert by_name["short-drama"]["role"] == "shot_planner"
    assert by_name["short-drama"]["operations"] == ["shot_decomposition", "shot_revision"]
    assert by_name["lighting"]["bound_to"] == "cinematography" and not by_name["lighting"]["runtime_bound"]
    assert set(CALL_SITES) == set(EXPECTED_BINDINGS)


def test_the_creative_rows_carry_the_versions_of_what_was_actually_injected(container, project):  # type: ignore[no-untyped-def]
    director = ScriptedDirector(_rich_turn)
    container.creative_director.model_roles = director
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        approved = _approve_brief(client, started["session_id"], started["brief_revision"])
        view = _state(client, started["session_id"])
    director_skill = container.skills.resolve("director")
    planner_skill = container.skills.resolve("short-drama")
    turn = next(item for item in view["turns"] if item["speaker"] == "DIRECTOR")
    assert turn["skill_version"] == director_skill.version and turn["skill_driven"] is True
    assert turn["skill_invocation"]["operation"] == "creative_conversation"
    assert turn["skill_invocation"]["content_hash"] == director_skill.content_hash
    assert turn["context"]["skill_invocation"]["loaded"] is True
    screenplay = approved["screenplay"]
    assert screenplay["skill_version"] == director_skill.version
    story, shots = screenplay["skill_invocations"]["story"], screenplay["skill_invocations"]["shots"]
    assert story["operation"] == "story_generation" and story["content_hash"] == director_skill.content_hash
    assert shots["operation"] == "shot_decomposition" and shots["content_hash"] == planner_skill.content_hash
    assert shots["model_role"] == "SHOT_PLANNER" and shots["skill_driven"] is True
    assert story["execution_record_id"] == screenplay["model_execution_record_id"]
    assert shots["execution_record_id"].startswith("exec-shotplan")
    # The hash on the row is the hash of the body that was in the call.
    story_call = next(
        call for call in director.calls if _latest_state_block(call["messages"]).get("task") == "WRITE_STORY"
    )
    assert director_skill.system_prompt in story_call["messages"][0]["content"]


@pytest.mark.asyncio
async def test_the_episode_continuation_plans_under_the_director_skill(container, project, bodies):  # type: ignore[no-untyped-def]
    episode_id, _shot_ids = _compiled_episode(container, project)
    model = StageModel(
        lambda role, request: {
            "premise": "Mira answers the phone that knows her name.",
            "beats": [{"intent": "PICKUP", "summary": "She answers."}],
            "shots": [{"framing": "wide"}],
        }
    )
    container.episode_continuations.model_roles = model
    prepared = await container.episode_continuations.prepare(
        project.id, previous_episode_id=episode_id, continuation_mode="CONTINUOUS"
    )
    assert model.calls[0]["role"] == "DIRECTOR"
    assert _bodies_in(model.calls[0]["messages"][0]["content"], bodies) == {"director"}
    assert prepared["reasoner"] == "MODEL:DIRECTOR"
    provenance = prepared["brief"]["provenance"]
    assert provenance["skill_invocation"]["name"] == "director"
    assert provenance["skill_invocation"]["version"] == container.skills.resolve("director").version
    assert provenance["skill_invocation"]["authority_violations"] == ["stripped:shots"]
    assert prepared["brief"]["premise"] == "Mira answers the phone that knows her name."
    container.episode_continuations.model_roles = None
    fallback = await container.episode_continuations.prepare(
        project.id, previous_episode_id=episode_id, continuation_mode="CONTINUOUS", regenerate=True
    )
    assert fallback["reasoner"] == "DETERMINISTIC"
    assert (
        fallback["brief"]["provenance"]["skill_invocation"]["fallback_reason"]
        == "MODEL_RUNTIME_NOT_CONFIGURED"
    )
    assert fallback["brief"]["provenance"]["skill_invocation"]["skill_driven"] is False


def test_a_disabled_stage_is_recorded_as_disabled_not_as_the_skills_work(container, project):  # type: ignore[no-untyped-def]
    import asyncio

    _episode_id, shot_ids = _compiled_episode(container, project)
    container.skill_runtime.model_roles = StageModel(lambda role, request: _cinematography_plan())
    container.cinematography_designer.enabled = False
    designed = asyncio.run(container.cinematography_designer.design_shot(shot_ids[0]))
    assert designed["skill_invocation"]["fallback_reason"] == "SKILL_STAGE_DISABLED"
    assert not designed["skill_driven"] and designed["shot_type"] == "ACTION"
    container.cinematography_designer.enabled = True


# ------------------------------------------------ 7. invariant versioning
def test_invariant_classes_are_versioned_and_a_change_records_what_it_supersedes() -> None:
    first = invariant_record(None, SCREENPLAY)
    assert first["version"].startswith("inv:") and first["supersedes_version"] is None
    same = invariant_record(SCREENPLAY, SCREENPLAY)
    assert same["changed"] == [] and same["supersedes_version"] is None
    assert same["unchanged_from_version"] == first["version"]
    moved = json.loads(json.dumps(SCREENPLAY))
    moved["treatment"]["ending"] = "The phone goes dark."
    moved["beats"][1]["shots"][1]["dialogue"]["text"] = "You did. Three days from now."
    changed = invariant_record(SCREENPLAY, moved)
    assert changed["changed"] == ["required_dialogue", "ending"]
    assert changed["supersedes_version"] == first["version"] and changed["version"] != first["version"]
    variable = json.loads(json.dumps(SCREENPLAY))
    variable["variables"] = ["pacing"]
    variable["treatment"]["tone_direction"] = "warmer"
    assert invariant_record(SCREENPLAY, variable)["changed"] == []


def test_a_revision_that_moves_an_invariant_supersedes_the_previous_version_without_rewriting_it(  # type: ignore[no-untyped-def]
    container, project
):
    def screenplay(request: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(json.dumps(SCREENPLAY))
        if request.get("task") == "REVISE_STORY":
            payload["treatment"]["ending"] = "The phone goes dark and the city lights go out."
        return payload

    container.creative_director.model_roles = ScriptedDirector(_rich_turn, screenplay)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        first = _approve_brief(client, session_id, started["brief_revision"])["screenplay"]
        second = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/propose", json={"notes": "darker ending"}
        ).json()
    assert first["invariant_version"] and first["supersedes_version"] is None
    assert second["revision"] == 2 and second["parent_revision"] == 1
    assert second["invariant_changes"] == ["ending"]
    assert second["supersedes_version"] == first["invariant_version"]
    assert second["invariant_version"] != first["invariant_version"]
    with container.database.session() as session:
        rows = {
            row.revision: row
            for row in session.scalars(
                select(CreativeScreenplayRevision).where(CreativeScreenplayRevision.session_id == session_id)
            )
        }
        assert rows[1].status == "SUPERSEDED" and rows[2].status == "PROPOSED"
        assert rows[1].content_json["treatment"]["ending"] == SCREENPLAY["treatment"]["ending"]
        assert rows[1].content_hash == first["content_hash"]


# ------------------------------------------------------------ the API list
def test_the_skills_endpoint_reports_binding_not_only_registration(container):  # type: ignore[no-untyped-def]
    with TestClient(create_app(container)) as client:
        listing = {item["name"]: item for item in client.get("/v1/skills").json()}
    assert len(listing) == 12
    assert listing["director"]["runtime_bound"] and listing["director"]["body_injected"]
    assert listing["director"]["model_role"] == "DIRECTOR" and listing["director"]["stage"] == "STORY"
    assert "shot_count" in listing["director"]["forbidden_authority"]
    assert listing["prompt-compiler"]["output_contract"] == "PromptCompilerOutput"
    for name in REFERENCE_SKILLS:
        assert listing[name]["runtime"] == "reference" and not listing[name]["runtime_bound"]


# ------------------------------------------------ the generation path
def test_generation_compiles_through_the_skill_first_and_reuses_the_package(container, bodies):  # type: ignore[no-untyped-def]
    compiler_calls = StageModel(
        lambda role, request: _compiled_package(request["envelope"])
        if role == "PROMPT_COMPILER"
        else AssertionError(role)
    )
    container.skill_runtime.model_roles = compiler_calls
    with TestClient(create_app(container)) as client:
        project = client.post(
            "/v1/projects", json={"title": "Rainy Night", "default_provider": "google_flow"}
        )
        project_id = project.json()["id"]
        episode = client.post(
            f"/v1/projects/{project_id}/episodes",
            json={
                "project_id": project_id,
                "title": "Pilot",
                "episode_number": 1,
                "script_source": "EXT. HOTEL - NIGHT\nLinJin turns toward the door.",
            },
        )
        shot_id = client.post(f"/v1/episodes/{episode.json()['id']}/compile").json()["shot_ids"][0]
        first = client.post(
            f"/v1/shots/{shot_id}/generate", json={"idempotency_key": "skill-gen-1", "estimated_cost": 0.8}
        )
        assert first.status_code == 202, first.text
        second = client.post(
            f"/v1/shots/{shot_id}/generate", json={"idempotency_key": "skill-gen-1", "estimated_cost": 0.8}
        )
        assert second.status_code == 202, second.text
        assert second.json()["replayed"] is True
    # One paid Skill compile for the envelope; the replayed request reused it.
    assert [call["role"] for call in compiler_calls.calls] == ["PROMPT_COMPILER"]
    assert _bodies_in(compiler_calls.calls[0]["messages"][0]["content"], bodies) == {"prompt-compiler"}
    skill = container.skills.resolve("prompt-compiler")
    with container.database.session() as session:
        records = list(
            session.scalars(
                select(PromptCompilation)
                .where(PromptCompilation.shot_id == shot_id)
                .order_by(PromptCompilation.created_at, PromptCompilation.id)
            )
        )
    modes = [record.diff_json["execution_mode"] for record in records]
    assert modes and set(modes) == {"MODEL"}, modes
    assert records[0].diff_json["reused_from"] is None
    assert all(record.diff_json["reused_from"] == records[0].id for record in records[1:])
    assert all(record.skill_versions == {"prompt-compiler": skill.version} for record in records)
    assert all(record.diff_json["skill_driven"] for record in records)
    assert records[0].compiled_prompt.startswith("LinJin")
    with container.database.session() as session:
        assert session.get(Shot, shot_id).compiled_prompt == records[0].compiled_prompt
