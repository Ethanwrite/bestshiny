"""Content-quality acceptance of the Skill runtime: what a paid Skill decided is what the provider gets.

Five gaps the 2026-09-09 acceptance review reproduced on the tree carrying
#64 and #65, each pinned here so it cannot come back:

1. delivery - the prompt-compiler Skill's package was recorded and never
   delivered: the adapter regenerated the prompt from the spec. Now the
   package is the body of the provider request (prompt, negative prompt,
   payload), the request says which source it carries, a same-key replay
   still replays, and a retry onto another model keeps the Skill's wording;
2. the continuity gate - a Skill-driven ESCALATE with approval_required was a
   record only. Now it blocks the Skill compile (recorded, no call), the
   generation-path compile (raised) and POST /generate (409) until a real
   user acknowledges the decision or a Skill-driven re-review clears it; a
   deterministic review stays advisory, and a re-review that fell back
   cannot release the Skill's escalation;
3. the preflight - TBD / hedged photographic fields compiled, and a Skill
   plan's own unresolved entries never reached the envelope. Now both refuse
   on every path, prose is never hedge-scanned, and a deterministic plan's
   marker does not block;
4. the mapping - height, lens intent, depth of field, motivation, exposure
   intent, subject placement, atmosphere and the compositions were designed
   and dropped. Now they reach the spec, the neutral prompt and every
   adapter's lines; an un-designed shot renders exactly as before;
5. freshness - a package from an earlier Skill was reused under the new
   version's name. Now freshness is the envelope hash *and* the producing
   Skill's content hash, the record names the producing Skill, a Skill
   refusal survives a Skill edit, and a cinematography fallback keeps an
   older Skill's plan while saying so.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from continuity_core import ContinuityAcknowledgementConflict
from evaluation_core import EvaluationDecision, RetryPlan
from fastapi.testclient import TestClient
from platform_contracts import (
    FORBIDDEN_PREFIX,
    PROHIBITION_PREFIX,
    CanonicalShotSpec,
    PromptCompilerInput,
    PromptCompilerOutput,
    PromptContinuityContext,
)
from production_domain.models import (
    DecisionRecord,
    Episode,
    GenerationJob,
    PromptCompilation,
    Scene,
    Shot,
    TimelineState,
)
from provider_sdk import ProviderError
from skill_core import ContinuityApprovalRequired
from skill_core.runtime import Validated
from sqlalchemy import select
from test_creative_director import _registered_pro
from test_provider_payload_contracts import (
    CANONICAL_SPEC,
)
from test_provider_payload_contracts import (
    _shot as _plain_shot,
)
from test_provider_payload_contracts import (
    retry_container as retry_container,  # re-exported so the fixture resolves here
)
from test_skill_runtime import (
    StageModel,
    _cinematography_plan,
    _compiled_episode,
    _compiled_package,
    _continuity_pass,
)
from video_adapter_core import AdapterInput, KlingAdapter, VideoAdapterRegistry
from video_adapter_core.base import canonical_lines, negative_prompt, prompt_lines
from video_platform_api.main import create_app
from video_prompt_core.compiler import _validate_compiler_output, action_preserved

SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
ADAPTER_MODELS = {
    "kling": "kling-3.0",
    "veo": "veo-3.1-quality",
    "seedance": "doubao-seedance-2-5-260628",
    "grok": "grok-video",
    "wan": "wan-2.7",
}


def _continuity_escalate() -> dict[str, Any]:
    return {
        "verdict": "ESCALATE",
        "matched_state": ["characters.mira.costume"],
        "mismatches": [
            {"field": "held_props.phone", "previous": "in hand", "next": "absent", "severity": "MAJOR"}
        ],
        "evidence": ["no registered END_FRAME evidence for the source shot"],
        "minimal_repair": None,
        "approval_required": True,
        "unresolved": ["end frame evidence missing"],
    }


def _stage_handler(continuity: dict[str, Any] | Exception, plan: dict[str, Any] | None = None):  # type: ignore[no-untyped-def]
    def handler(role: str, request: dict[str, Any]) -> Any:
        if role == "CINEMATOGRAPHY_REASONING":
            return plan or _cinematography_plan()
        if role == "CONTINUITY_REASONER":
            return continuity
        if role == "PROMPT_COMPILER":
            return _compiled_package(request["envelope"])
        raise AssertionError(role)

    return handler


def _envelope(**spec: Any) -> PromptCompilerInput:
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


def _modified_skills(tmp_path: Path, name: str) -> Path:
    """A copy of the installed Skills with one body changed: a new version of that Skill."""

    root = tmp_path / "skills"
    shutil.copytree(SKILLS_ROOT, root)
    body = root / name / "SKILL.md"
    body.write_text(
        body.read_text("utf-8") + "\n\nA reviewed note on wording, added by the acceptance test.\n"
    )
    return root


def _job_request(container, shot_id: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        job = session.scalar(
            select(GenerationJob)
            .where(GenerationJob.shot_id == shot_id)
            .order_by(GenerationJob.created_at.desc())
        )
        assert job is not None
        return dict(job.request_json)


def _skill_records(container, shot_id: str) -> list[PromptCompilation]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        rows = list(
            session.scalars(
                select(PromptCompilation)
                .where(PromptCompilation.shot_id == shot_id)
                .order_by(PromptCompilation.created_at, PromptCompilation.id)
            )
        )
        for row in rows:
            session.expunge(row)
        return rows


def _api_shot(client: TestClient, script: str, *, title: str = "Rainy Night") -> tuple[str, list[str]]:
    project = client.post("/v1/projects", json={"title": title, "default_provider": "google_flow"}).json()
    episode = client.post(
        f"/v1/projects/{project['id']}/episodes",
        json={"project_id": project["id"], "title": "Pilot", "episode_number": 1, "script_source": script},
    ).json()
    shot_ids = client.post(f"/v1/episodes/{episode['id']}/compile").json()["shot_ids"]
    return project["id"], list(shot_ids)


# ------------------------------------------------------------------ 1. delivery
def test_the_skill_package_is_the_body_of_the_provider_request_and_replays_still_replay(container):  # type: ignore[no-untyped-def]
    compiler_calls = StageModel(
        lambda role, request: (
            _compiled_package(request["envelope"]) if role == "PROMPT_COMPILER" else AssertionError(role)
        )
    )
    container.skill_runtime.model_roles = compiler_calls
    with TestClient(create_app(container)) as client:
        _project_id, shot_ids = _api_shot(client, "EXT. HOTEL - NIGHT\nLinJin turns toward the door.")
        shot_id = shot_ids[0]
        first = client.post(f"/v1/shots/{shot_id}/generate", json={"idempotency_key": "delivery-1"})
        assert first.status_code == 202, first.text
        second = client.post(f"/v1/shots/{shot_id}/generate", json={"idempotency_key": "delivery-1"})
        assert second.status_code == 202, second.text
        assert second.json()["replayed"] is True
    assert [call["role"] for call in compiler_calls.calls] == ["PROMPT_COMPILER"]
    records = _skill_records(container, shot_id)
    package = records[0].diff_json["prompt_compiler_output"]
    request = _job_request(container, shot_id)

    # The paid wording is the body; the adapter adds only its model-specific line.
    assert request["prompt"].startswith(package["positive_prompt"])
    assert "Shot intent:" not in request["prompt"]
    assert "one continuous physically plausible trajectory" in request["prompt"]  # the Veo line
    assert request["provider_payload"]["prompt"] == request["prompt"]
    # The Skill's negative prompt, with the platform's guards it did not name.
    assert request["negative_prompt"].startswith(package["negative_prompt"])
    assert "visual style drift" in request["negative_prompt"]
    delivery = request["metadata"]["prompt_delivery"]
    skill = container.skills.resolve("prompt-compiler")
    assert delivery["prompt_source"] == "SKILL_PACKAGE" and delivery["skill_driven"] is True
    assert delivery["skill_version"] == skill.version and delivery["skill_content_hash"] == skill.content_hash
    assert delivery["input_hash"] == records[0].diff_json["input_hash"]
    assert request["metadata"]["prompt_package"]["positive_prompt"] == package["positive_prompt"]
    assert "record_id" not in delivery  # replay-stable facts only


def test_a_deterministic_compilation_keeps_the_canonical_rendering_and_says_so(container):  # type: ignore[no-untyped-def]
    container.skill_runtime.model_roles = None
    with TestClient(create_app(container)) as client:
        _project_id, shot_ids = _api_shot(client, "EXT. HOTEL - NIGHT\nLinJin turns toward the door.")
        shot_id = shot_ids[0]
        response = client.post(f"/v1/shots/{shot_id}/generate", json={"idempotency_key": "canonical-1"})
        assert response.status_code == 202, response.text
    request = _job_request(container, shot_id)
    assert request["prompt"].startswith("Shot intent: LinJin turns toward the door.")
    assert "Locked visual style" in request["prompt"]
    assert request["negative_prompt"].startswith("identity drift, visual style drift")
    delivery = request["metadata"]["prompt_delivery"]
    assert delivery["prompt_source"] == "ADAPTER_CANONICAL" and delivery["skill_driven"] is False
    assert delivery["execution_mode"] == "DETERMINISTIC"
    assert "prompt_package" not in request["metadata"]


def test_prompt_lines_deliver_the_package_with_the_guards_the_package_does_not_carry() -> None:
    spec = CanonicalShotSpec(
        intent="Mira picks up the phone",
        dominant_action="Mira picks up the phone",
        subjects=[{"name": "Mira", "eyeline_target": "the phone"}],
        style_lock={"name": "Noir"},
    )
    package = PromptCompilerOutput(
        status="COMPILED",
        positive_prompt="Mira picks up the phone, eyes on the phone.",
        negative_prompt="a second action, an unintended cut",
        continuity_assertions=[
            "Mira picks up the phone, eyes on the phone.",
            "the phone stays in her left hand",
        ],
    )
    lines = prompt_lines(spec, {"assembled_text": "CURRENT_TEMPORAL_STATE: wardrobe remains blue"}, package)
    assert lines[0] == "Mira picks up the phone, eyes on the phone."
    assert "Locked visual style: {'name': 'Noir'}" in lines
    # An assertion the Skill already restated in its prose is not repeated.
    assert "Continuity assertions: the phone stays in her left hand" in lines
    assert "Bounded production context:\nCURRENT_TEMPORAL_STATE: wardrobe remains blue" in lines
    assert "Shot intent:" not in "\n".join(lines)
    assert negative_prompt(package) == (
        "a second action, an unintended cut, identity drift, visual style drift, palette drift, "
        "altered canonical product, extra subjects, duplicate limbs, extra cuts"
    )
    # No package, or a refusal: the canonical rendering and the baseline guards.
    assert prompt_lines(spec, {}, None) == canonical_lines(spec, {})
    refusal = PromptCompilerOutput(status="NOT_COMPILABLE", review_reason="no")
    assert prompt_lines(spec, {}, refusal) == canonical_lines(spec, {})
    assert negative_prompt(None) == negative_prompt(refusal)
    kling = KlingAdapter().compile("kling-3.0", AdapterInput(shot=spec, context={}, package=package))
    assert kling.prompt.startswith("Mira picks up the phone, eyes on the phone.")
    assert kling.prompt.endswith("Execute continuous physical motion with precise first/last-frame control.")
    assert kling.negative_prompt == negative_prompt(package)
    assert kling.continuity_assertions == list(package.continuity_assertions)


def test_every_adapter_delivers_the_package_and_never_loses_a_prohibition() -> None:
    """All five, not just the three the API and retry tests happen to exercise."""

    spec = CanonicalShotSpec(
        intent="Mira picks up the phone",
        dominant_action="Mira picks up the phone",
        subjects=[{"name": "Mira", "eyeline_target": "the phone"}],
        constraints=[
            "stage the approved action as: she crosses to the ledge first",
            f"{PROHIBITION_PREFIX}no alcohol anywhere in frame",
            f"{FORBIDDEN_PREFIX}alcohol",
        ],
    )
    package = PromptCompilerOutput(
        status="COMPILED",
        positive_prompt="Mira picks up the phone, eyes on the phone.",
        negative_prompt="a second action",
    )
    registry = VideoAdapterRegistry()
    tails = {
        "kling": "precise first/last-frame control.",
        "veo": "physically plausible trajectory.",
        "seedance": "never merge another story action.",
        "grok": "preserve the approved body orientation.",
        "wan": "preserve all multimodal references.",
    }
    for name, model in ADAPTER_MODELS.items():
        request = registry.get(name).compile(model, AdapterInput(shot=spec, context={}, package=package))
        assert request.prompt.startswith("Mira picks up the phone, eyes on the phone."), name
        assert "Shot intent:" not in request.prompt, name
        assert request.prompt.rstrip().endswith(tails[name]), name
        # The client's own words and the staging the deterministic path
        # printed are still delivered, and the forbidden thing is negative.
        assert "no alcohol anywhere in frame" in request.prompt, name
        assert "she crosses to the ledge first" in request.prompt, name
        assert "alcohol" in request.negative_prompt, name
        assert "visual style drift" in request.negative_prompt, name
        assert request.negative_prompt.startswith("a second action"), name
    # Without a package the canonical rendering and the baseline stand.
    plain = registry.get("kling").compile("kling-3.0", AdapterInput(shot=spec, context={}))
    assert plain.prompt.startswith("Shot intent:")
    assert plain.negative_prompt.startswith("identity drift, visual style drift")
    assert "alcohol" in plain.negative_prompt


def test_a_retry_onto_another_model_keeps_the_skills_wording(retry_container):  # type: ignore[no-untyped-def]
    container, project = retry_container
    shot_id = _plain_shot(container, project)
    package = {
        "status": "COMPILED",
        "positive_prompt": "Lin turns once, eyes on the door, in the doorway light.",
        "negative_prompt": "a second turn",
        "asset_bindings": [],
        "continuity_assertions": ["the door stays half open"],
        "qc_checklist": ["one turn"],
        "missing_fields": [],
        "review_reason": None,
    }
    request = {
        "project_id": project.id,
        "shot_id": shot_id,
        "candidate_id": None,
        "type": "video",
        "provider": "google_flow",
        "model": "flow-veo-3.1",
        "prompt": package["positive_prompt"] + "\nUse concise spatial language.",
        "negative_prompt": "a second turn, identity drift",
        "duration": 8,
        "aspect_ratio": "9:16",
        "reference_asset_ids": [],
        "idempotency_key": "skill-retry-origin",
        "provider_payload": {"prompt": "stale"},
        "metadata": {"prompt_package": package, "prompt_delivery": {"prompt_source": "SKILL_PACKAGE"}},
    }
    plan = RetryPlan(
        action=EvaluationDecision.SWITCH_MODEL,
        attempt_number=1,
        terminal=False,
        next_provider="seedance",
        next_model="doubao-seedance-2-5-260628",
        reasons=["switch"],
    )
    job = container.visual_runtime._execute_retry(
        "origin-skill-retry", request, request["metadata"], CANONICAL_SPEC, plan
    )
    with container.database.session() as session:
        stored = dict(session.get(GenerationJob, job.id).request_json)
    assert stored["model"] == "doubao-seedance-2-5-260628"
    assert stored["prompt"].startswith(package["positive_prompt"])
    assert "Preserve complex blocking as ordered temporal beats" in stored["prompt"]  # the Seedance line
    assert "Shot intent:" not in stored["prompt"]
    assert stored["provider_payload"]["prompt"] == stored["prompt"]
    assert stored["negative_prompt"].startswith("a second turn")
    # A stored package that no longer validates is treated as absent, never as
    # a failure - and the retry's record says the canonical prompt was sent.
    broken = {
        **request,
        "idempotency_key": "skill-retry-broken",
        "metadata": {
            "prompt_package": {"status": "COMPILED"},
            "prompt_delivery": {"prompt_source": "SKILL_PACKAGE", "skill_driven": True},
        },
    }
    fallback = container.visual_runtime._execute_retry(
        "origin-skill-retry-broken", broken, broken["metadata"], CANONICAL_SPEC, plan
    )
    with container.database.session() as session:
        stored = dict(session.get(GenerationJob, fallback.id).request_json)
    assert stored["prompt"].startswith("Shot intent:")
    delivery = stored["metadata"]["prompt_delivery"]
    assert delivery["prompt_source"] == "ADAPTER_CANONICAL" and delivery["skill_driven"] is False
    assert delivery["prompt_package_invalid"] is True
    assert "prompt_package" not in stored["metadata"]


def test_the_action_check_holds_meaning_not_punctuation() -> None:
    """The verbatim rule that discarded the first live production package.

    A model rendered the approved action `雨桐进入城市天台上发现了一部不属于她的手机`
    as `雨桐进入城市天台，发现了一部不属于她的手机。` - one particle fewer, one comma
    more - and the package was refused with `dominant_action_missing`, so the
    deterministic JSON dump went to the provider instead of the paid wording.
    The check exists so the compiler cannot change what happens in the shot;
    it must not also be a test of punctuation.
    """

    action = "雨桐进入城市天台上发现了一部不属于她的手机"
    live = "夜间城市天台场景，人物雨桐。雨桐进入城市天台，发现了一部不属于她的手机。相机缓慢向前推轨。"

    # Carried, and recorded as a paraphrase rather than a quotation.
    assert action_preserved(action, live) == (True, False)
    assert action_preserved(action, f"前缀。{action}。后缀。") == (True, True)
    assert action_preserved("Mira picks up the phone", "Mira picks up the phone, slowly.") == (True, True)
    assert action_preserved(
        "Mira picks up the phone", "In the rain, Mira picks up the phone from the ledge."
    ) == (True, True)
    assert action_preserved("", "anything at all") == (True, True)

    # What the check is actually for: a different action, or none.
    for prose in (
        "夜间城市天台，空无一人的画面，远处霓虹灯。",
        "雨桐进入城市天台，扔掉了那一部不属于她的手机。",
    ):
        assert action_preserved(action, prose)[0] is False, prose
    for prose in (
        "A quiet moment on the rooftop; the skyline glows.",
        "Mira throws the phone off the ledge.",
        "Theo picks up the phone.",
    ):
        assert action_preserved("Mira picks up the phone", prose)[0] is False, prose


def test_a_package_that_renders_the_action_without_quoting_it_is_recorded_as_a_paraphrase() -> None:
    """The advisory the runtime carries when the action survives but is not quoted."""

    action = "雨桐进入城市天台上发现了一部不属于她的手机"
    spec = CanonicalShotSpec(
        intent=action,
        dominant_action=action,
        subjects=[{"name": "雨桐", "eyeline_target": "the phone"}],
    )
    envelope = PromptCompilerInput(
        shot_spec=spec.model_dump(mode="json"),
        asset_bindings=[],
        continuity_context=PromptContinuityContext(),
    )
    package = {
        "status": "COMPILED",
        "positive_prompt": "夜间城市天台。雨桐进入城市天台，发现了一部不属于她的手机。相机缓慢推轨。",
        "negative_prompt": "禁止第二个动作",
        "asset_bindings": [],
        "continuity_assertions": [],
        "qc_checklist": ["one action"],
        "missing_fields": [],
        "review_reason": None,
    }
    validated = _validate_compiler_output(spec, envelope, package)
    assert isinstance(validated, Validated)
    assert validated.reason_codes == ["ACTION_PARAPHRASED"]
    assert validated.output.status == "COMPILED"

    # Quoted exactly: no advisory, the output itself comes back.
    quoted = {**package, "positive_prompt": f"夜间城市天台。{action}。相机缓慢推轨。"}
    assert isinstance(_validate_compiler_output(spec, envelope, quoted), PromptCompilerOutput)

    # A different action is still refused.
    swapped = {**package, "positive_prompt": "雨桐进入城市天台，扔掉了那一部不属于她的手机。"}
    with pytest.raises(ValueError, match="dominant_action_missing"):
        _validate_compiler_output(spec, envelope, swapped)


@pytest.mark.asyncio
async def test_a_package_that_does_not_quote_the_action_still_ships(container, project):  # type: ignore[no-untyped-def]
    """The whole point of the fix: the paid wording reaches the provider."""

    _episode_id, (first, _second) = _compiled_episode(container, project)
    action = container.prompts._envelope(first).spec.dominant_action

    def rendering(role: str, request: dict[str, Any]) -> dict[str, Any]:
        package = _compiled_package(request["envelope"])
        # The approved action, punctuated as prose rather than quoted.
        package["positive_prompt"] = package["positive_prompt"].replace(
            action, action.rstrip(".") + ", slowly."
        )
        return package

    container.skill_runtime.model_roles = StageModel(rendering)
    result = await container.prompts.compile_shot_with_skill(first)
    assert result.output.status == "COMPILED"
    assert result.skill_driven and result.execution_mode == "MODEL"
    assert action.rstrip(".") in (result.output.positive_prompt or "")
    with container.database.session() as session:
        assert session.get(PromptCompilation, result.record_id).diff_json["skill_driven"] is True

    # And it is what the provider receives.
    request = (
        VideoAdapterRegistry()
        .get("kling")
        .compile("kling-3.0", AdapterInput(shot=result.spec, context={}, package=result.output))
    )
    assert request.prompt.startswith(result.output.positive_prompt.strip().splitlines()[0])
    assert "Shot intent:" not in request.prompt

    # A model that drops the action still loses its package. `reuse_fresh=False`,
    # or the fresh package from the call above would be returned untouched.
    container.skill_runtime.model_roles = StageModel(
        lambda role, req: _compiled_package(req["envelope"], drop_action=True)
    )
    refused = await container.prompts.compile_shot_with_skill(first, reuse_fresh=False)
    assert refused.execution_mode == "DETERMINISTIC" and not refused.skill_driven
    assert "dominant_action_missing" in refused.skill_invocation["validation_errors"][0]


# ------------------------------------------------------------ 2. the continuity gate
@pytest.mark.asyncio
async def test_a_skill_escalation_blocks_compilation_until_a_human_approves_it(container, project):  # type: ignore[no-untyped-def]
    episode_id, (first, second) = _compiled_episode(container, project)
    stages = StageModel(_stage_handler(_continuity_escalate()))
    container.skill_runtime.model_roles = stages
    reviewed = await container.continuity_reviewer.review_pair(second)
    assert reviewed["review"]["verdict"] == "ESCALATE" and reviewed["skill_driven"]
    assert reviewed["approval_required"] is True and reviewed["input_hash"]
    pending = container.continuity_reviewer.pending_escalation(second)
    assert pending is not None
    assert pending["decision_id"] == reviewed["decision_record_id"] and pending["stale"] is False
    assert pending["skill_driven"] is True and pending["from_shot_id"] == first
    assert container.continuity_reviewer.pending_escalation(first) is None

    # The Skill compile records the block and pays for nothing.
    calls_before = len(stages.calls)
    blocked = await container.prompts.compile_shot_with_skill(second)
    assert blocked.output.status == "NOT_COMPILABLE" and blocked.execution_mode == "DETERMINISTIC"
    assert blocked.output.missing_fields == ["continuity.approval"]
    assert blocked.skill_invocation["fallback_reason"] == "CONTINUITY_APPROVAL_REQUIRED"
    assert f"CONTINUITY_DECISION:{pending['decision_id']}" in blocked.skill_invocation["reason_codes"]
    assert len(stages.calls) == calls_before
    with container.database.session() as session:
        record = session.get(PromptCompilation, blocked.record_id)
        assert record.diff_json["continuity_gate"]["decision_id"] == pending["decision_id"]
    # The generation-path compile raises, with the decision to acknowledge.
    with pytest.raises(ContinuityApprovalRequired) as excinfo:
        container.prompts.compile(second)
    detail = excinfo.value.as_detail()
    assert detail["reason_code"] == "CONTINUITY_APPROVAL_REQUIRED"
    assert detail["decision_id"] == pending["decision_id"] and detail["shot_id"] == second
    assert detail["acknowledge"] == f"POST /v1/shots/{second}/continuity/review/acknowledge"
    assert "end frame evidence missing" in detail["message"]
    # The shot with no handoff is untouched.
    assert container.prompts.compile(first).output.status == "COMPILED"

    # The stage runner names the handoff and reports the shot as blocked, not compiled.
    report = await container.visual_stages.run_episode(episode_id)
    assert [item["shot_id"] for item in report["approval_required"]] == [second]
    assert report["approval_required"][0]["from_shot_id"] == first
    blocked_entry = next(item for item in report["prompt_compilation"] if item["shot_id"] == second)
    assert blocked_entry["status"] == "NOT_COMPILABLE"
    assert blocked_entry["fallback_reason"] == "CONTINUITY_APPROVAL_REQUIRED"
    assert report["summary"]["prompt_compilation"] == {"total": 2, "skill_driven": 1}
    assert report["errors"] == []
    standing = container.continuity_reviewer.pending_escalation(second)
    assert standing is not None and standing["decision_id"] == report["approval_required"][0]["decision_id"]

    # A deterministic review is advisory: it neither clears nor replaces the Skill's escalation.
    container.skill_runtime.model_roles = None
    advisory = await container.continuity_reviewer.review_pair(second)
    assert advisory["execution_mode"] == "DETERMINISTIC"
    assert container.continuity_reviewer.pending_escalation(second)["decision_id"] == standing["decision_id"]

    # A human acknowledges the standing decision, on record; the shot compiles.
    older = pending["decision_id"]
    with pytest.raises(ContinuityAcknowledgementConflict) as conflict:
        container.continuity_reviewer.acknowledge(second, decision_id=older, actor="t", actor_user_id=None)
    assert conflict.value.as_detail()["pending_decision_id"] == standing["decision_id"]
    acknowledged = container.continuity_reviewer.acknowledge(
        second,
        decision_id=standing["decision_id"],
        actor="tester",
        actor_user_id=None,
        note="shot one will be shot and its end frame registered first",
    )
    assert acknowledged["already_acknowledged"] is False and acknowledged["pending_escalation"] is None
    again = container.continuity_reviewer.acknowledge(
        second, decision_id=standing["decision_id"], actor="tester", actor_user_id=None
    )
    assert again["already_acknowledged"] is True
    assert again["acknowledgement_id"] == acknowledged["acknowledgement_id"]
    assert container.prompts.compile(second).output.status == "COMPILED"
    view = container.continuity_reviewer.gate_view(second)
    assert view["blocked"] is False and view["latest_review"]["execution_mode"] == "DETERMINISTIC"
    assert view["acknowledgements"][0]["review_decision_id"] == standing["decision_id"]
    with container.database.session() as session:
        ack = session.get(DecisionRecord, acknowledged["acknowledgement_id"])
        assert ack.decision_type == "CONTINUITY_ESCALATION_ACKNOWLEDGED"
        assert (
            ack.selected_action == "APPROVED"
            and ack.input_features["review_decision_id"] == standing["decision_id"]
        )

    # A later Skill escalation needs its own approval; a Skill PASS clears it.
    container.skill_runtime.model_roles = StageModel(_stage_handler(_continuity_escalate()))
    later = await container.continuity_reviewer.review_pair(second)
    assert (
        container.continuity_reviewer.pending_escalation(second)["decision_id"] == later["decision_record_id"]
    )
    with pytest.raises(ContinuityApprovalRequired):
        container.prompts.compile(second)
    container.skill_runtime.model_roles = StageModel(_stage_handler(_continuity_pass()))
    await container.continuity_reviewer.review_pair(second)
    assert container.continuity_reviewer.pending_escalation(second) is None
    assert container.prompts.compile(second).output.status == "COMPILED"


@pytest.mark.asyncio
async def test_a_deterministic_escalation_never_blocks_a_shot(container, project):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(project_id=project.id, title="Costume", episode_number=1)
        session.add(episode)
        session.flush()
        episode_id = episode.id
        scene = Scene(episode_id=episode.id, sequence=1, description="Rooftop")
        session.add(scene)
        session.flush()
        states = []
        for kind, costume in (
            ("SHOT_INPUT", "red"),
            ("SHOT_OUTPUT", "red"),
            ("SHOT_INPUT", "torn"),
            ("SHOT_OUTPUT", "torn"),
        ):
            state = TimelineState(
                project_id=project.id,
                episode_id=episode.id,
                scene_id=scene.id,
                state_kind=kind,
                state_json={
                    "characters": {"mira": {"name": "Mira", "costume": costume, "gaze_target": "the door"}}
                },
            )
            session.add(state)
            states.append(state)
        session.flush()
        first = Shot(
            scene_id=scene.id,
            sequence=1,
            prompt="Mira waits.",
            input_state_id=states[0].id,
            output_state_id=states[1].id,
        )
        session.add(first)
        session.flush()
        second = Shot(
            scene_id=scene.id,
            sequence=2,
            prompt="Mira turns.",
            previous_shot_id=first.id,
            input_state_id=states[2].id,
            output_state_id=states[3].id,
        )
        session.add(second)
        session.flush()
        second_id = second.id
    container.skill_runtime.model_roles = None
    reviewed = await container.continuity_reviewer.review_pair(second_id)
    assert reviewed["review"]["verdict"] == "ESCALATE" and reviewed["approval_required"] is True
    assert reviewed["execution_mode"] == "DETERMINISTIC" and not reviewed["skill_driven"]
    assert container.continuity_reviewer.pending_escalation(second_id) is None
    assert container.prompts.compile(second_id).output.status == "COMPILED"
    assert container.continuity_reviewer.gate_view(second_id)["blocked"] is False
    # The stage report records the verdict and does not ask anyone to approve it.
    report = await container.visual_stages.run_episode(episode_id)
    entry = next(item for item in report["continuity"] if item["to_shot_id"] == second_id)
    assert entry["approval_required"] is True and entry["skill_driven"] is False
    assert report["approval_required"] == []
    assert report["summary"]["prompt_compilation"]["total"] == 2


@pytest.mark.asyncio
async def test_a_stale_escalation_is_reviewed_again_and_a_fallback_re_review_keeps_it_pending(  # type: ignore[no-untyped-def]
    container, project
):
    _episode_id, (first, second) = _compiled_episode(container, project)
    container.skill_runtime.model_roles = StageModel(_stage_handler(_continuity_escalate()))
    await container.continuity_reviewer.review_pair(second)
    assert container.continuity_reviewer.pending_escalation(second)["stale"] is False
    # Nothing pending or a fresh escalation: no call.
    passing = StageModel(_stage_handler(_continuity_pass()))
    container.skill_runtime.model_roles = passing
    assert (await container.continuity_reviewer.ensure_reviewed(second))["stale"] is False
    assert (await container.continuity_reviewer.ensure_reviewed(first)) is None
    assert passing.calls == []

    # The handoff's inputs move (the previous shot is designed): the escalation is stale.
    await container.cinematography_designer.design_shot(first)
    assert passing.calls[-1]["role"] == "CINEMATOGRAPHY_REASONING"
    assert container.continuity_reviewer.pending_escalation(second)["stale"] is True
    # A Skill-driven re-review answers PASS and releases the shot: one paid call.
    assert (await container.continuity_reviewer.ensure_reviewed(second)) is None
    assert [call["role"] for call in passing.calls[-1:]] == ["CONTINUITY_REASONER"]
    assert container.prompts.compile(second).output.status == "COMPILED"

    # Escalate again, move the inputs again, and let the model be unavailable for
    # the re-review: the deterministic fallback cannot release the Skill's verdict.
    container.skill_runtime.model_roles = StageModel(_stage_handler(_continuity_escalate()))
    escalated = await container.continuity_reviewer.review_pair(second)
    container.skill_runtime.model_roles = StageModel(
        _stage_handler(_continuity_pass(), plan=_cinematography_plan("close-up"))
    )
    await container.cinematography_designer.design_shot(first)
    assert container.continuity_reviewer.pending_escalation(second)["stale"] is True
    from production_domain.models import RetryCategory

    outage = StageModel(
        lambda role, request: (
            ProviderError("503", RetryCategory.PROVIDER_BUSY, code="UP")
            if role == "CONTINUITY_REASONER"
            else AssertionError(role)
        )
    )
    container.skill_runtime.model_roles = outage
    still = await container.continuity_reviewer.ensure_reviewed(second)
    assert still is not None and still["decision_id"] == escalated["decision_record_id"]
    assert still["rereview_fallback_reason"] == "MODEL_UNAVAILABLE" and still["rereview_decision_id"]
    assert len(outage.calls) == 1
    with pytest.raises(ContinuityApprovalRequired) as excinfo:
        container.prompts.compile(second)
    detail = excinfo.value.as_detail()
    assert detail["latest_review_fallback_reason"] == "MODEL_UNAVAILABLE"
    assert detail["latest_review_decision_id"] == still["rereview_decision_id"]


def test_generate_refuses_an_escalated_handoff_until_a_real_user_acknowledges_it(container):  # type: ignore[no-untyped-def]
    stages = StageModel(_stage_handler(_continuity_escalate()))
    container.skill_runtime.model_roles = stages
    with TestClient(create_app(container)) as client:
        headers, project_id, user_id = _registered_pro(client, container, "continuity-gate@example.com")
        episode = client.post(
            f"/v1/projects/{project_id}/episodes",
            headers=headers,
            json={
                "project_id": project_id,
                "title": "Pilot",
                "episode_number": 1,
                "script_source": "INT. Rooftop - NIGHT\nMira picks up the phone.\nMira: You finally came.",
            },
        ).json()
        first, second = client.post(f"/v1/episodes/{episode['id']}/compile", headers=headers).json()[
            "shot_ids"
        ]
        reviewed = client.post(
            f"/v1/shots/{second}/continuity/review", headers=headers, json={"project_id": project_id}
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["review"]["verdict"] == "ESCALATE"
        decision_id = reviewed.json()["decision_record_id"]
        gate = client.get(f"/v1/shots/{second}/continuity/review", headers=headers).json()
        assert gate["blocked"] is True and gate["pending_escalation"]["decision_id"] == decision_id

        refused = client.post(
            f"/v1/shots/{second}/generate", headers=headers, json={"idempotency_key": "gate-1"}
        )
        assert refused.status_code == 409, refused.text
        detail = refused.json()["detail"]
        assert detail["reason_code"] == "CONTINUITY_APPROVAL_REQUIRED"
        assert detail["decision_id"] == decision_id and detail["stale"] is False
        assert detail["acknowledge"] == f"POST /v1/shots/{second}/continuity/review/acknowledge"
        # Refused before anything was compiled or planned: no compiler call.
        assert [call["role"] for call in stages.calls] == ["CONTINUITY_REASONER"]
        compiled = client.post(
            f"/v1/shots/{second}/prompt/compile", headers=headers, json={"project_id": project_id}
        )
        assert compiled.status_code == 200, compiled.text
        assert compiled.json()["status"] == "NOT_COMPILABLE"
        assert compiled.json()["skill_invocation"]["fallback_reason"] == "CONTINUITY_APPROVAL_REQUIRED"
        # The first shot has no handoff and is not refused for continuity.
        first_gate = client.get(f"/v1/shots/{first}/continuity/review", headers=headers).json()
        assert first_gate["blocked"] is False

        # The development bypass cannot release a Skill's escalation.
        bypass = client.post(
            f"/v1/shots/{second}/continuity/review/acknowledge",
            json={"project_id": project_id, "decision_id": decision_id},
        )
        assert bypass.status_code == 403, bypass.text
        wrong = client.post(
            f"/v1/shots/{second}/continuity/review/acknowledge",
            headers=headers,
            json={"project_id": project_id, "decision_id": first},
        )
        assert wrong.status_code == 404, wrong.text
        acknowledged = client.post(
            f"/v1/shots/{second}/continuity/review/acknowledge",
            headers=headers,
            json={"project_id": project_id, "decision_id": decision_id, "note": "approved by the director"},
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["pending_escalation"] is None
        gate = client.get(f"/v1/shots/{second}/continuity/review", headers=headers).json()
        assert gate["blocked"] is False
        assert gate["acknowledgements"][0]["actor_user_id"] == user_id
        assert gate["acknowledgements"][0]["note"] == "approved by the director"
        # A later escalation needs its own approval. Acknowledging the decision
        # already approved stays idempotent and says what now stands...
        second_escalation = client.post(
            f"/v1/shots/{second}/continuity/review", headers=headers, json={"project_id": project_id}
        )
        assert second_escalation.status_code == 200, second_escalation.text
        second_id = second_escalation.json()["decision_record_id"]
        assert second_id != decision_id
        repeated = client.post(
            f"/v1/shots/{second}/continuity/review/acknowledge",
            headers=headers,
            json={"project_id": project_id, "decision_id": decision_id},
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["already_acknowledged"] is True
        assert repeated.json()["pending_escalation"]["decision_id"] == second_id
        # ...and approving a decision that was never approved and no longer
        # stands is a 409 naming the one that does, never a 500.
        third = client.post(
            f"/v1/shots/{second}/continuity/review", headers=headers, json={"project_id": project_id}
        )
        third_id = third.json()["decision_record_id"]
        superseded = client.post(
            f"/v1/shots/{second}/continuity/review/acknowledge",
            headers=headers,
            json={"project_id": project_id, "decision_id": second_id},
        )
        assert superseded.status_code == 409, superseded.text
        assert superseded.json()["detail"]["reason_code"] == "CONTINUITY_ESCALATION_NOT_PENDING"
        assert superseded.json()["detail"]["pending_decision_id"] == third_id
        client.post(
            f"/v1/shots/{second}/continuity/review/acknowledge",
            headers=headers,
            json={"project_id": project_id, "decision_id": third_id},
        ).raise_for_status()
        released = client.post(
            f"/v1/shots/{second}/generate", headers=headers, json={"idempotency_key": "gate-2"}
        )
        # Released from the continuity gate; whatever the generation-policy
        # gate says about a chained second shot is its own answer.
        assert released.status_code != 409 or "CONTINUITY_APPROVAL_REQUIRED" not in released.text, (
            released.text
        )


# ------------------------------------------------------------- 3. the preflight
def test_unresolved_or_hedged_photographic_fields_are_not_compilable(container):  # type: ignore[no-untyped-def]
    compile_input = container.prompts.compile_input
    assert compile_input(_envelope()).status == "COMPILED"
    tbd = compile_input(_envelope(camera={"framing": "TBD", "focus": "tbd", "angle": "待定"}))
    assert tbd.status == "NOT_COMPILABLE"
    assert tbd.missing_fields == ["camera.angle", "camera.framing", "camera.focus"]
    assert tbd.positive_prompt is None and "camera.framing is unresolved" in (tbd.review_reason or "")
    hedged = compile_input(_envelope(camera={"framing": "provisional medium shot, to be confirmed"}))
    assert hedged.status == "NOT_COMPILABLE" and hedged.missing_fields == ["camera.framing"]
    assert compile_input(_envelope(camera={"height": "暂定", "lens_intent": "natural"})).missing_fields == [
        "camera.height"
    ]
    lighting = compile_input(_envelope(lighting={"direction": "TBD", "quality": "soft", "practicals": ["?"]}))
    assert lighting.missing_fields == ["lighting.direction", "lighting.practicals[0]"]
    named = compile_input(_envelope(subjects=[{"name": "TBD", "eyeline_target": "the phone"}]))
    assert named.missing_fields == ["subjects[0].name"]
    placed = compile_input(
        _envelope(
            subjects=[{"name": "Mira", "eyeline_target": "the phone", "screen_position": "placeholder"}]
        )
    )
    assert placed.missing_fields == ["subjects[0].screen_position"]
    prop = compile_input(_envelope(props=[{"name": "phone", "state": "to be determined"}]))
    assert prop.missing_fields == ["props[0].state"]
    # Prose is never hedge-scanned: a tentative step is a performance, a line is a line,
    # and the deterministic scaffold's staging keeps compiling.
    assert (
        compile_input(_envelope(dominant_action="Mira takes a tentative step toward the ledge")).status
        == "COMPILED"
    )
    assert compile_input(_envelope(dialogue="To be confirmed, she says.")).status == "COMPILED"
    assert (
        compile_input(
            _envelope(subjects=[{"name": "Mira", "eyeline_target": "the phone", "pose": "tentative"}])
        ).status
        == "COMPILED"
    )
    scaffold = compile_input(
        _envelope(
            constraints=[
                "stage the approved action as: placeholder staging; the director model was unavailable"
            ]
        )
    )
    assert scaffold.status == "COMPILED"
    # "tentatively" is not "tentative"; the marker values still refuse.
    assert compile_input(_envelope(camera={"path": "tentatively along the ledge"})).status == "COMPILED"
    assert compile_input(_envelope(camera={"path": "?"})).missing_fields == ["camera.path"]


@pytest.mark.asyncio
async def test_a_skill_plans_unresolved_entries_refuse_compilation_on_every_path(container, project):  # type: ignore[no-untyped-def]
    _episode_id, (first, second) = _compiled_episode(container, project)
    hedging = StageModel(
        _stage_handler(
            _continuity_pass(),
            plan={**_cinematography_plan(), "unresolved": ["gaze target missing for the cast", "None.", " "]},
        )
    )
    container.skill_runtime.model_roles = hedging
    designed = await container.cinematography_designer.design_shot(first)
    assert designed["skill_driven"]
    envelope = container.prompts._envelope(first)
    assert [item for item in envelope.spec.constraints if item.startswith("unresolved:")] == [
        "unresolved: cinematography[0]: gaze target missing for the cast"
    ]
    assert envelope.cinematography_notes["unresolved"] == ["gaze target missing for the cast"]
    with pytest.raises(ValueError, match="cinematography stage left a decision unresolved"):
        container.prompts.compile(first)
    calls_before = len(hedging.calls)
    refused = await container.prompts.compile_shot_with_skill(first)
    assert refused.output.status == "NOT_COMPILABLE" and not refused.skill_driven
    assert refused.output.missing_fields == ["cinematography.unresolved[0]"]
    assert refused.skill_invocation["fallback_reason"] == "PREFLIGHT_NOT_COMPILABLE"
    assert "CINEMATOGRAPHY_UNRESOLVED" in refused.skill_invocation["reason_codes"]
    assert "PREFLIGHT:cinematography.unresolved[0]" in refused.skill_invocation["reason_codes"]
    assert len(hedging.calls) == calls_before  # no compiler model call
    with container.database.session() as session:
        record = session.get(PromptCompilation, refused.record_id)
        assert record.diff_json["cinematography"]["unresolved"] == ["gaze target missing for the cast"]
    # The stage's own fallback marker is a record, not an unresolved field.
    container.skill_runtime.model_roles = StageModel(
        lambda role, request: {**_cinematography_plan(), "action": "Mira throws the phone"}
    )
    fallen = await container.cinematography_designer.design_shot(second)
    assert not fallen["skill_driven"] and any(
        "DETERMINISTIC DEFAULTS" in item for item in fallen["plan"]["unresolved"]
    )
    assert container.prompts.compile(second).output.status == "COMPILED"
    # A plan that says "none" leaves nothing behind.
    container.skill_runtime.model_roles = StageModel(
        _stage_handler(
            _continuity_pass(), plan={**_cinematography_plan(), "unresolved": ["N/A - all fields decided"]}
        )
    )
    await container.cinematography_designer.design_shot(second)
    assert container.prompts.compile(second).output.status == "COMPILED"


# ---------------------------------------------------------------- 4. the mapping
@pytest.mark.asyncio
async def test_the_whole_cinematography_design_reaches_the_spec_the_neutral_prompt_and_every_adapter(  # type: ignore[no-untyped-def]
    container, project
):
    _episode_id, (first, second) = _compiled_episode(container, project)
    plan = {**_cinematography_plan(), "subject_positions": {"mira": "screen right, midground"}}
    container.skill_runtime.model_roles = StageModel(_stage_handler(_continuity_pass(), plan=plan))
    assert (await container.cinematography_designer.design_shot(first))["skill_driven"]
    compiled = container.prompts.compile(first)
    spec = compiled.spec
    assert (spec.camera.height, spec.camera.lens_intent, spec.camera.depth_of_field) == (
        "knee height",
        "natural perspective",
        "deep",
    )
    assert (spec.lighting.motivation, spec.lighting.exposure_intent) == (
        "the city below",
        "protect the phone screen",
    )
    assert spec.atmosphere == "rain"
    assert spec.composition == {
        "start": "Mira small against the skyline",
        "end": "Mira at the ledge, phone glowing",
    }
    # The plan's placement, matched case- and space-blind, fills the subject's screen position.
    assert spec.subjects[0].screen_position == "screen right, midground"
    neutral = json.loads(compiled.neutral_prompt)
    assert (
        neutral["camera"]["height"] == "knee height"
        and neutral["camera"]["lens_intent"] == "natural perspective"
    )
    assert neutral["lighting"]["motivation"] == "the city below"
    assert (
        neutral["atmosphere"] == "rain"
        and neutral["composition"]["end"] == "Mira at the ledge, phone glowing"
    )
    assert any(item == "atmosphere=rain" for item in compiled.output.qc_checklist)
    assert any(item.startswith("composition_end=") for item in compiled.output.qc_checklist)
    with container.database.session() as session:
        record = session.get(PromptCompilation, compiled.record_id)
        notes = record.diff_json["cinematography"]
        assert notes["subject_position_overrides"] == {
            "Mira": {"from": "midground_center", "to": "screen right, midground"}
        }
    registry = VideoAdapterRegistry()
    for name, model in ADAPTER_MODELS.items():
        prompt = registry.get(name).compile(model, AdapterInput(shot=spec, context={})).prompt
        for expected in (
            "height=knee height",
            "lens=natural perspective",
            "depth of field=deep",
            "angle=low angle",
            "'motivation': 'the city below'",
            "'exposure_intent': 'protect the phone screen'",
            "Atmosphere: rain",
            "Composition: start=Mira small against the skyline; end=Mira at the ledge, phone glowing",
            "screen right, midground",
        ):
            assert expected in prompt, (name, expected)

    # An explicit approved screen position is kept; the plan's placement is recorded beside it.
    with container.database.session() as session:
        shot = session.get(Shot, second)
        state = session.get(TimelineState, shot.input_state_id)
        characters = dict(state.state_json["characters"])
        key = next(iter(characters))
        characters[key] = {**characters[key], "screen_position": "screen left, foreground"}
        state.state_json = {**state.state_json, "characters": characters}
    await container.cinematography_designer.design_shot(second)
    kept = container.prompts.compile(second)
    assert kept.spec.subjects[0].screen_position == "screen left, foreground"
    with container.database.session() as session:
        record = session.get(PromptCompilation, kept.record_id)
        assert record.diff_json["cinematography"]["state_positions_kept"] == {
            "Mira": {"state": "screen left, foreground", "plan": "screen right, midground"}
        }
    # A name the plan invented is recorded, never guessed.
    container.skill_runtime.model_roles = StageModel(
        _stage_handler(_continuity_pass(), plan={**plan, "subject_positions": {"Nobody": "centre"}})
    )
    await container.cinematography_designer.design_shot(first)
    with container.database.session() as session:
        record = session.get(PromptCompilation, container.prompts.compile(first).record_id)
        assert record.diff_json["cinematography"]["unmatched_subject_positions"] == ["Nobody"]


@pytest.mark.asyncio
async def test_a_deterministic_plan_is_not_a_design(container, project):  # type: ignore[no-untyped-def]
    """A fallback plan validates the contract, so it carries the contract's defaults.

    ``deterministic_plan`` builds a ``CinematographyPlan`` from a framing
    alone, and the contract fills ``height``, ``lens_intent`` and
    ``depth_of_field`` with its own defaults. Rendering those would tell the
    provider that a stage decided a camera height nobody decided.
    """

    _episode_id, (first, _second) = _compiled_episode(container, project)
    container.skill_runtime.model_roles = StageModel(
        lambda role, request: {**_cinematography_plan(), "action": "Mira throws the phone"}
    )
    fallen = await container.cinematography_designer.design_shot(first)
    assert not fallen["skill_driven"]
    assert fallen["plan"]["camera"]["height"] == "subject eye height"  # the contract's default
    spec = container.prompts.compile(first).spec
    assert (spec.camera.height, spec.camera.lens_intent, spec.camera.depth_of_field) == ("", "", "")
    assert spec.atmosphere == "" and spec.composition == {}
    assert spec.lighting.motivation == "" and spec.lighting.exposure_intent == ""
    assert "height=" not in "\n".join(canonical_lines(spec, {}))


def test_an_undesigned_shot_renders_exactly_as_before(container, project):  # type: ignore[no-untyped-def]
    _episode_id, (first, _second) = _compiled_episode(container, project)
    compiled = container.prompts.compile(first)
    neutral = json.loads(compiled.neutral_prompt)
    assert "height" not in neutral["camera"] and "lens_intent" not in neutral["camera"]
    assert (
        "motivation" not in neutral["lighting"]
        and "atmosphere" not in neutral
        and "composition" not in neutral
    )
    assert compiled.output.positive_prompt == compiled.neutral_prompt
    assert not any(item.startswith(("atmosphere=", "composition_")) for item in compiled.output.qc_checklist)
    prompt = "\n".join(canonical_lines(compiled.spec, {}))
    assert "height=" not in prompt and "lens=" not in prompt and "depth of field=" not in prompt
    assert "'motivation'" not in prompt and "Atmosphere:" not in prompt and "Composition:" not in prompt
    assert "; angle=" in prompt


# ---------------------------------------------------------------- 5. freshness
@pytest.mark.asyncio
async def test_a_package_from_an_earlier_skill_is_not_reused_and_the_record_names_the_producing_skill(  # type: ignore[no-untyped-def]
    container, project, tmp_path
):
    _episode_id, (first, _second) = _compiled_episode(container, project)
    faithful = StageModel(lambda role, request: _compiled_package(request["envelope"]))
    container.skill_runtime.model_roles = faithful
    produced = await container.prompts.compile_shot_with_skill(first)
    old_version = produced.skill_version
    old_hash = produced.skill_invocation["content_hash"]
    assert produced.skill_driven and len(faithful.calls) == 1
    reused = container.prompts.compile(first)
    assert reused.skill_driven and reused.skill_invocation["reason_codes"][-1] == "MODEL_REPLY"

    # The Skill changes: the registry re-parses on every resolve.
    container.skills.root = _modified_skills(tmp_path, "prompt-compiler")
    new = container.skills.resolve("prompt-compiler")
    assert new.version != old_version and new.content_hash != old_hash

    stale = container.prompts.compile(first)
    assert stale.execution_mode == "DETERMINISTIC" and not stale.skill_driven
    assert stale.skill_invocation["fallback_reason"] == "NO_FRESH_SKILL_COMPILATION"
    assert f"SKILL_VERSION_CHANGED:{old_version}" in stale.skill_invocation["reason_codes"]
    assert stale.skill_version == new.version and stale.skill_invocation["version"] == new.version
    assert len(faithful.calls) == 1

    repaid = await container.prompts.compile_shot_with_skill(first)
    assert repaid.skill_driven and len(faithful.calls) == 2
    assert repaid.skill_version == new.version and repaid.skill_invocation["content_hash"] == new.content_hash
    assert f"SKILL_VERSION_CHANGED:{old_version}" in repaid.skill_invocation["reason_codes"]
    fresh_again = container.prompts.compile(first)
    assert fresh_again.skill_driven and len(faithful.calls) == 2
    with container.database.session() as session:
        for record_id, expected_version in (
            (reused.record_id, old_version),
            (stale.record_id, new.version),
            (repaid.record_id, new.version),
            (fresh_again.record_id, new.version),
        ):
            record = session.get(PromptCompilation, record_id)
            assert record.skill_versions == {"prompt-compiler": expected_version}, record_id
            assert record.diff_json["skill_invocation"]["version"] == expected_version
            assert (
                record.diff_json["skill_content_hash"] == record.diff_json["skill_invocation"]["content_hash"]
            )
        assert (
            session.get(PromptCompilation, fresh_again.record_id).diff_json["reused_from"] == repaid.record_id
        )
        assert (
            session.get(PromptCompilation, stale.record_id).diff_json["installed_skill_version"]
            == new.version
        )


@pytest.mark.asyncio
async def test_a_skill_refusal_survives_a_skill_edit_until_the_new_skill_answers(
    container, project, tmp_path
):  # type: ignore[no-untyped-def]
    _episode_id, (first, _second) = _compiled_episode(container, project)
    refusing = StageModel(
        lambda role, request: {
            "status": "NOT_COMPILABLE",
            "missing_fields": ["end_state"],
            "review_reason": "the end state is not reachable by the single action",
        }
    )
    container.skill_runtime.model_roles = refusing
    verdict = await container.prompts.compile_shot_with_skill(first)
    assert verdict.output.status == "NOT_COMPILABLE" and verdict.skill_driven
    container.skills.root = _modified_skills(tmp_path, "prompt-compiler")
    with pytest.raises(ValueError, match="judged this shot not compilable"):
        container.prompts.compile(first)
    faithful = StageModel(lambda role, request: _compiled_package(request["envelope"]))
    container.skill_runtime.model_roles = faithful
    answered = await container.prompts.compile_shot_with_skill(first)
    assert answered.output.status == "COMPILED" and answered.skill_driven and len(faithful.calls) == 1
    assert container.prompts.compile(first).skill_driven


@pytest.mark.asyncio
async def test_one_design_then_a_fallback_keeps_the_paid_plan(container, project):  # type: ignore[no-untyped-def]
    """The production sequence: design once, then re-run when the model is down.

    The stage writes its framing back onto ``shots.shot_type``, which is part
    of the shot's own context. While that write-back was inside the reuse key,
    a single Skill design could never be recognised again: the very next
    fallback overwrote a paid, authority-checked plan with locked-off defaults,
    and the compiler rendered those.
    """

    _episode_id, (first, _second) = _compiled_episode(container, project)
    container.skill_runtime.model_roles = StageModel(_stage_handler(_continuity_pass()))
    designed = await container.cinematography_designer.design_shot(first)
    assert designed["skill_driven"] and designed["shot_type"] == "WIDE"
    container.skill_runtime.model_roles = StageModel(
        lambda role, request: {**_cinematography_plan(), "action": "Mira throws the phone"}
    )
    fallen = await container.cinematography_designer.design_shot(first)
    assert fallen["reused_existing_plan"] is True
    assert fallen["plan"]["camera"]["framing"] == "wide establishing"
    assert fallen["skill_driven"] is True  # the plan on the shot is still the Skill's
    assert container.prompts.compile(first).spec.camera.dominant_movement == "slow dolly in"


@pytest.mark.asyncio
async def test_a_cinematography_fallback_keeps_an_older_skills_plan_and_says_so(container, project, tmp_path):  # type: ignore[no-untyped-def]
    _episode_id, (first, _second) = _compiled_episode(container, project)
    container.skill_runtime.model_roles = StageModel(_stage_handler(_continuity_pass()))
    designed = await container.cinematography_designer.design_shot(first)
    assert designed["skill_driven"]
    old = container.skills.resolve("cinematography")
    container.skills.root = _modified_skills(tmp_path, "cinematography")
    new = container.skills.resolve("cinematography")
    assert new.content_hash != old.content_hash
    container.skill_runtime.model_roles = StageModel(
        lambda role, request: {**_cinematography_plan(), "action": "Mira throws the phone"}
    )
    fallen = await container.cinematography_designer.design_shot(first)
    assert fallen["reused_existing_plan"] is True and fallen["reused_plan_skill_version"] == old.version
    assert fallen["skill_invocation"]["version"] == old.version  # the plan on the shot keeps its provenance
    assert fallen["plan"]["camera"]["framing"] == "wide establishing"
    with container.database.session() as session:
        decision = session.scalar(
            select(DecisionRecord)
            .where(DecisionRecord.shot_id == first, DecisionRecord.decision_type == "CINEMATOGRAPHY_DESIGN")
            .order_by(DecisionRecord.created_at.desc(), DecisionRecord.id.desc())
        )
        assert decision.selected_action == "REUSED_SKILL_PLAN"
        assert f"SKILL_VERSION_CHANGED:{old.version}" in decision.reason_codes
        assert decision.input_features["reused_plan_skill_version"] == old.version
        assert decision.input_features["reused_plan_content_hash"] == old.content_hash
        assert decision.input_features["skill_invocation"]["version"] == new.version
