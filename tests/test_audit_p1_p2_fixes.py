"""The P1/P2 findings of the 2026-09-04 creative-director audit, each pinned by a test.

Ten of them let something the user approved fail to reach production - the
frame, a bound anchor, the copy, a selling point, the call to action, a
prohibition - or let the model move a fact on a genuine quote about something
else, or paid for a director call twice. Five were quieter: a constraint lost
to a misspelt scope, a scene plate that never found its Location, a shot
memory retrieval could never return, a degraded vector that was never
re-embedded, and a shadow job marked reported while a character was still
waiting.
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from datetime import timedelta
from typing import Any

import pytest
from creative_director_core import CreativeDirectorService, CreativeSessionConflict
from creative_director_core.beats import director_intent
from creative_director_core.brief import (
    OperationActor,
    apply_operations,
    value_supported_by_evidence,
)
from creative_director_core.evidence import VALUE_NOT_IN_EVIDENCE, UserTextIndex, UserUtterance
from creative_director_core.schemas import BriefOperation, BriefOperationKind
from creative_director_core.screenplay import (
    ScreenplayInvalid,
    merge_shot_anchors,
    preserved_product_claims,
    shot_constraints,
    validate_screenplay,
)
from creative_director_core.screenplay_brief import enforceable_prohibition, prohibited_terms
from creative_director_core.service import _TURN_CLAIM_LEASE, _location_keys, _styles_match
from director_production.pipeline import CandidatePipeline
from memory_core.embedding import LocalTestEmbeddingProvider, MemoryEmbeddingUnavailable
from memory_core.engine import MultimodalMemoryEngine
from memory_core.outbox import MemoryIndexOutboxWorker, MemoryIndexOutboxWriter
from platform_contracts import (
    CanonicalShotSpec,
    PromptCompilerInput,
    PromptContinuityContext,
    approved_aspect_ratio,
)
from production_domain.models import (
    Asset,
    CharacterEvidenceCoverage,
    CharacterEvidenceSubmission,
    CreativeSession,
    CreativeTurnClaim,
    CreativeVisualAnchor,
    Episode,
    GenerationCandidate,
    Location,
    MemoryIndexOutbox,
    Project,
    ProjectStyleLock,
    Scene,
    Shot,
    ShotMemory,
    VisualBibleVersion,
    utcnow,
)
from production_domain.models import Character as CharacterRow
from sqlalchemy import select
from test_character_evidence_lifecycle import _AcceptingProducer, _tracker
from test_character_evidence_multicharacter import (
    _report_payloads,
    _seed_two_hander,
    _signed_callback,
    _webhook_container,
)
from test_creative_director import (
    RICH_IDEA,
    SCREENPLAY,
    ScriptedDirector,
    _approve_brief,
    _approve_screenplay,
    _client,
    _complete_visuals,
    _ready_anchors_without_generation,
    _registered_pro,
    _rich_turn,
    _start,
    _state,
    _user,
    _wire_openrouter_images,
)
from test_creative_director import (
    openrouter_container as openrouter_container,  # re-exported so the fixture resolves here
)
from test_creative_director_evidence import _last_director_turn, _turn
from test_screenplay_brief_conformance import BRIEF, USER_FACTS, VALIDATOR
from video_prompt_core.compiler import FORBIDDEN_PREFIX, PROHIBITION_PREFIX


def _claim_for(session, session_id: str):  # type: ignore[no-untyped-def]
    return session.scalar(select(CreativeTurnClaim).where(CreativeTurnClaim.session_id == session_id))


def _submission_for(session, candidate_id: str):  # type: ignore[no-untyped-def]
    return session.scalar(
        select(CharacterEvidenceSubmission).where(
            CharacterEvidenceSubmission.candidate_id == candidate_id
        )
    )


def _codes(conformance) -> set[str]:  # type: ignore[no-untyped-def]
    return {item.code for item in conformance.violations}


def _blocking(conformance) -> set[str]:  # type: ignore[no-untyped-def]
    return {item.code for item in conformance.blocking}


# --------------------------------------------------------------------------
# P1 · the approved frame reaches the compiler and the request
# --------------------------------------------------------------------------


def test_the_approved_aspect_ratio_rides_the_intent_and_beats_the_project_default(
    container, project
) -> None:  # type: ignore[no-untyped-def]
    intent = director_intent({"action": "Mira enters", "aspect_ratio": "16:9"})
    assert intent["aspect_ratio"] == "16:9"
    assert approved_aspect_ratio(intent) == "16:9"
    # Only a real ratio counts; garbage falls back to the project default.
    assert approved_aspect_ratio({"aspect_ratio": "wide"}) is None
    assert approved_aspect_ratio(None) is None
    # A shot made outside a session carries no frame of its own.
    assert "aspect_ratio" not in director_intent({"action": "Mira enters"})

    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="E1",
            episode_number=1,
            script_source="INT. ROOM - NIGHT\nMira raises the phone.\n",
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    shot_id = container.narrative.compile_episode(episode_id).shot_ids[0]
    with container.database.session() as session:
        assert session.get(Project, project.id).default_aspect_ratio == "9:16"
        shot = session.get(Shot, shot_id)
        shot.director_intent_json = {**dict(shot.director_intent_json or {}), "aspect_ratio": "16:9"}
    compiled = container.prompts.compile(shot_id)
    assert compiled.spec.aspect_ratio == "16:9"
    assert "aspect_ratio=16:9" in compiled.output.qc_checklist


# --------------------------------------------------------------------------
# P1 · a declared shot anchor survives beat materialization
# --------------------------------------------------------------------------


def test_a_declared_shot_anchor_is_kept_ahead_of_the_derived_ones() -> None:
    assert merge_shot_anchors(
        ["character:Ren", " prop:Phone ", "character:ren"],
        ["character:mira", "scene:rooftop", "style:master"],
    ) == ["character:ren", "prop:phone", "character:mira", "scene:rooftop", "style:master"]

    content = copy.deepcopy(SCREENPLAY)
    content["beats"][0]["shots"][0]["anchors"] = ["character:Ren"]
    planned = CreativeDirectorService._materialize_beats(validate_screenplay(content), {})
    anchors = planned[0]["shots"][0]["anchors"]
    assert anchors[0] == "character:ren", anchors
    assert {"character:mira", "scene:rooftop", "style:master"} <= set(anchors)
    # A shot that declared nothing is exactly what it was.
    assert planned[0]["shots"][1]["anchors"][0] == "character:mira"


# --------------------------------------------------------------------------
# P1 · copy placed in a shot that does not exist is refused
# --------------------------------------------------------------------------


def test_copy_placed_in_a_shot_that_does_not_exist_is_refused() -> None:
    content = copy.deepcopy(SCREENPLAY)
    content["required_copy"] = [{"text": "Only at bestshiny.com", "beat": 3, "shot": 2}]
    with pytest.raises(ScreenplayInvalid) as refused:
        validate_screenplay(content)
    assert "beat 3 shot 2" in " ".join(refused.value.details)
    content["required_copy"] = [{"text": "Only at bestshiny.com", "beat": 9, "shot": 1}]
    with pytest.raises(ScreenplayInvalid):
        validate_screenplay(content)
    content["required_copy"] = [{"text": "Only at bestshiny.com", "beat": 3, "shot": 1}]
    assert validate_screenplay(content).required_copy[0].placed
    # Unplaced copy is still the approval gate's business, not the schema's.
    content["required_copy"] = ["Only at bestshiny.com"]
    assert validate_screenplay(content).unplaced_copy


# --------------------------------------------------------------------------
# P1 · a genuine quote about something else cannot move a user fact
# --------------------------------------------------------------------------


def test_the_value_has_to_be_in_the_quote_for_the_paths_that_are_facts() -> None:
    replace, set_ = BriefOperationKind.REPLACE, BriefOperationKind.SET
    assert not value_supported_by_evidence(
        "setting.location", replace, "subway platform", "the rooftop is perfect"
    )
    assert value_supported_by_evidence("setting.location", replace, "地铁站", "换到地铁站")
    assert not value_supported_by_evidence("duration_seconds", replace, 60, "make it a minute")
    assert value_supported_by_evidence("duration_seconds", set_, 30, "三十秒")
    assert value_supported_by_evidence("duration_seconds", set_, 120, "两分钟")
    # Enum-like values may be proved through the cues the extractor knows.
    assert value_supported_by_evidence("visual_style.medium", set_, "anime", "改成动画")
    assert value_supported_by_evidence("aspect_ratio", set_, "9:16", "vertical")
    assert not value_supported_by_evidence("aspect_ratio", set_, "16:9", "竖屏")
    assert value_supported_by_evidence("format", set_, "SHORT_DRAMA", "短剧")
    # A cast member has to be named in the quote, whatever else it says.
    assert value_supported_by_evidence(
        "characters", BriefOperationKind.UPSERT, {"name": "Mira", "role": "lead"}, "protagonist is Mira"
    )
    assert not value_supported_by_evidence(
        "characters", BriefOperationKind.UPSERT, {"name": "Ren"}, "the rooftop is perfect"
    )
    # Prose the director phrases is not held to this; the quote check is.
    assert value_supported_by_evidence("logline", set_, "anything the director wrote", "whatever")
    assert value_supported_by_evidence("setting.location", BriefOperationKind.KEEP, None, "yes")


def test_a_real_quote_offered_as_proof_of_another_value_is_demoted_on_record() -> None:
    index = UserTextIndex([UserUtterance(turn_id="t1", turn_sequence=1, text=RICH_IDEA)])
    fields = {"setting": {"location": "rooftop", "time": "NIGHT"}, "duration_seconds": 30}
    provenance = {
        "setting.location": {"source": "USER_STATED"},
        "duration_seconds": {"source": "USER_STATED"},
    }
    operations = [
        BriefOperation(
            op=BriefOperationKind.REPLACE,
            path="setting.location",
            value="subway platform",
            evidence="set in rooftop at night",  # the user's words, about the rooftop
            confidence="USER_STATED",
        ),
        BriefOperation(
            op=BriefOperationKind.REPLACE,
            path="duration_seconds",
            value=90,
            evidence="30 second",
            confidence="USER_STATED",
        ),
    ]
    actor = OperationActor(
        reasoner="MODEL:DIRECTOR",
        turn_id="t3",
        turn_sequence=3,
        revision=2,
        at="2026-09-04T00:00:00+00:00",
        evidence_index=index,
    )
    result, records, outcome = apply_operations(fields, provenance, operations, actor)
    assert result["setting"]["location"] == "rooftop"
    assert result["duration_seconds"] == 30
    assert records["setting.location"]["source"] == "USER_STATED"
    reasons = {(item["path"], item["reason"]) for item in outcome.rejected}
    assert ("setting.location", VALUE_NOT_IN_EVIDENCE) in reasons
    assert ("duration_seconds", VALUE_NOT_IN_EVIDENCE) in reasons
    # Demoted to the director's inference, the operation then meets the rule
    # that an inference never overrides a user fact.
    assert ("setting.location", "INFERRED_CANNOT_OVERRIDE_USER_FACT") in reasons
    demotion = next(
        item
        for item in outcome.rejected
        if item["path"] == "setting.location" and item["reason"] == VALUE_NOT_IN_EVIDENCE
    )
    assert demotion["claimed"] == "USER_STATED" and demotion["recorded"] == "MODEL_INFERRED"


def test_a_genuine_quote_about_the_rooftop_cannot_move_the_story_to_the_subway(container, project):  # type: ignore[no-untyped-def]
    def handler(latest: str, state: dict) -> dict:
        if not state.get("brief"):
            return _rich_turn(latest, state)
        return {
            "assistant_message": "Moved to the subway.",
            "brief_operations": [
                {
                    "op": "REPLACE",
                    "path": "setting.location",
                    "value": "subway platform",
                    # Verbatim from the user's first message - and not about a subway.
                    "evidence": "set in rooftop at night",
                    "confidence": "USER_STATED",
                }
            ],
        }

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        reply = _turn(client, session_id, "what would you change?")
        view = _state(client, session_id)

    assert "EVIDENCE_UNVERIFIED" in reply["reason_codes"]
    assert view["brief"]["fields"]["setting"]["location"] == "rooftop"
    assert view["brief"]["provenance"]["setting.location"]["source"] == "USER_STATED"
    rejected = _last_director_turn(view)["result"]["rejected_operations"]
    demoted = next(item for item in rejected if item.get("reason") == VALUE_NOT_IN_EVIDENCE)
    assert demoted["path"] == "setting.location"
    assert demoted["claimed"] == "USER_STATED" and demoted["recorded"] == "MODEL_INFERRED"


# --------------------------------------------------------------------------
# P1 · a bible whose brief asks for another look cannot silently inherit
# --------------------------------------------------------------------------


def test_style_descriptors_compare_the_look_not_the_wording() -> None:
    assert _styles_match(
        {"medium": "Cinematic Live-Action", "palette": "", "tone": ["suspenseful", "warm"]},
        {"medium": "cinematic  live-action", "palette": "", "tone": ["warm", "suspenseful"]},
    )
    assert not _styles_match(
        {"medium": "anime", "palette": "", "tone": []},
        {"medium": "cinematic live-action", "palette": "", "tone": []},
    )
    # A lock whose look nobody recorded is never assumed to be this one.
    assert not _styles_match({"unknown": True}, {"medium": "anime", "palette": "", "tone": []})
    assert not _styles_match(None, {"medium": "anime", "palette": "", "tone": []})


ANIME_IDEA = RICH_IDEA.replace("cinematic live-action", "anime")


def _anime_turn(latest: str, state: dict) -> dict:
    """The same reading as `_rich_turn`, for an idea that asks for anime."""

    payload = _rich_turn(latest, state)
    for operation in payload.get("brief_operations", []):
        if operation["path"] == "visual_style":
            operation["value"] = {"medium": "anime"}
            operation["evidence"] = "anime"
    return payload


def test_a_second_session_with_another_look_must_accept_the_inherited_lock_on_record(container, project):  # type: ignore[no-untyped-def]
    service = container.creative_director
    user_id = _user(container, "style-conflict@example.com")

    # Session one locks the project's style: cinematic live-action.
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        first = _start(client, project.id, RICH_IDEA)
        _approve_brief(client, first["session_id"], first["brief_revision"])
        _approve_screenplay(client, first["session_id"])
    _ready_anchors_without_generation(container, first["session_id"], project.id)
    with _client(container) as client:
        bible = client.post(f"/v1/creative/sessions/{first['session_id']}/bible/propose").json()
    assert bible["content"]["style_inheritance"] is None
    locked = service.approve_bible(
        first["session_id"], version=bible["version"], actor="locker", actor_user_id=user_id
    )
    assert locked["status"] == "LOCKED" and locked["lineage"]["style_inherited"] is False

    # Session two asks for anime in the same project.
    container.creative_director.model_roles = ScriptedDirector(_anime_turn)
    with _client(container) as client:
        second = _start(client, project.id, ANIME_IDEA)
        session_id = second["session_id"]
        assert _state(client, session_id)["brief"]["fields"]["visual_style"]["medium"] == "anime"
        _approve_brief(client, session_id, second["brief_revision"])
        _approve_screenplay(client, session_id)
    with container.database.session() as session:
        style_anchor = session.scalar(
            select(CreativeVisualAnchor).where(
                CreativeVisualAnchor.session_id == session_id, CreativeVisualAnchor.kind == "STYLE"
            )
        )
        # The plate is not generated (the lock would discard it), and the
        # record says why: the locked look differs from this brief's.
        assert style_anchor.status == "SKIPPED"
        assert style_anchor.skip_reason == "PROJECT_STYLE_LOCK_DIFFERS"
    _ready_anchors_without_generation(container, session_id, project.id)
    with _client(container) as client:
        draft = client.post(f"/v1/creative/sessions/{session_id}/bible/propose").json()
        inheritance = draft["content"]["style_inheritance"]
        assert inheritance["inherited"] is True and inheritance["matches_brief"] is False
        assert inheritance["locked_style"]["medium"] == "cinematic live-action"
        assert inheritance["brief_style"]["medium"] == "anime"
        assert inheritance["locked_style"]["locked_from_session_id"] == first["session_id"]

        refused = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve", json={"version": draft["version"]}
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["reason_code"] == "STYLE_LOCK_CONFLICT"
        with container.database.session() as session:
            assert len(list(session.scalars(select(ProjectStyleLock)))) == 1
            assert session.scalar(
                select(VisualBibleVersion.status).where(VisualBibleVersion.session_id == session_id)
            ) == "DRAFT"

    # The user decides, on record, that the new cast renders in the old look.
    # (Through the service: the API's development bypass carries no user, and
    # a style lock requires one - the route passes the flag through as above.)
    accepted = service.approve_bible(
        session_id,
        version=draft["version"],
        actor="locker",
        actor_user_id=user_id,
        accept_inherited_style=True,
    )
    lineage = accepted["lineage"]
    assert accepted["status"] == "LOCKED"
    assert lineage["lock_status"] == "LOCKED"
    assert lineage["style_inherited"] is True
    assert lineage["style_matches_this_bible"] is False
    assert lineage["style_conflict_accepted"] is True
    assert lineage["style_inheritance"]["accepted"] is True
    assert lineage["style_inheritance"]["accepted_by"]
    with container.database.session() as session:
        assert len(list(session.scalars(select(ProjectStyleLock)))) == 1


def test_a_second_session_with_the_same_look_inherits_without_a_decision(container, project):  # type: ignore[no-untyped-def]
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    service = container.creative_director
    user_id = _user(container, "same-look@example.com")
    session_ids = []
    with _client(container) as client:
        for _ in range(2):
            started = _start(client, project.id, RICH_IDEA)
            _approve_brief(client, started["session_id"], started["brief_revision"])
            _approve_screenplay(client, started["session_id"])
            session_ids.append(started["session_id"])
    for session_id in session_ids:
        _ready_anchors_without_generation(container, session_id, project.id)
        with _client(container) as client:
            bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose").json()
        locked = service.approve_bible(
            session_id, version=bible["version"], actor="locker", actor_user_id=user_id
        )
        assert locked["status"] == "LOCKED"
    with container.database.session() as session:
        second = session.scalar(
            select(VisualBibleVersion).where(VisualBibleVersion.session_id == session_ids[1])
        )
    assert second.lineage_json["style_inherited"] is True
    assert second.lineage_json["style_matches_this_bible"] is True
    assert second.lineage_json["style_conflict_accepted"] is False
    assert second.lineage_json["style_inheritance"]["matches_brief"] is True
    assert second.lineage_json["style_inheritance"]["accepted"] is True


# --------------------------------------------------------------------------
# P1 · a prohibition is enforced on what is done, and travels to the prompt
# --------------------------------------------------------------------------


def test_prohibition_sentences_become_the_things_they_forbid() -> None:
    assert prohibited_terms("不要暴力") == ["暴力"]
    assert prohibited_terms("请不要出现任何暴力镜头") == ["暴力"]
    assert prohibited_terms("不要暴力，也别有血腥镜头") == ["暴力", "血腥"]
    assert prohibited_terms("no talking heads or product close-ups") == [
        "talking heads",
        "product close-ups",
    ]
    assert prohibited_terms("Don't show the product price") == ["product price"]
    assert prohibited_terms("avoid direct gaze into the camera") == ["direct gaze into the camera"]
    # Sentences that merely contain a negative are not instructions: a villa
    # (别墅) and a fact about an umbrella forbid nothing.
    assert not enforceable_prohibition("主角别墅里有一把枪")
    assert not enforceable_prohibition("She has no umbrella")
    assert not enforceable_prohibition("我特别喜欢悬疑")
    assert prohibited_terms("She has no umbrella") == []
    assert enforceable_prohibition("千万别用动画") and enforceable_prohibition("Please, no blood.")


def test_a_forbidden_action_blocks_even_when_nobody_says_the_word() -> None:
    content = copy.deepcopy(SCREENPLAY)
    content["beats"][2]["shots"][0]["action"]["description"] = "她挥拳暴力地砸向门"
    found = VALIDATOR.validate(
        validate_screenplay(content),
        BRIEF,
        format_value="SHORT_DRAMA",
        provenance=USER_FACTS,
        prohibitions=["不要暴力"],
    )
    breach = [item for item in found.blocking if item.code == "PROHIBITION_BREACHED"]
    assert breach and breach[0].location == "beat 3 shot 1"
    assert "暴力" in breach[0].reason

    # An English instruction, breached in a beat summary rather than a line.
    spoken = copy.deepcopy(SCREENPLAY)
    spoken["beats"][1]["summary"] = "The phone rings; a talking heads exchange follows."
    found = VALIDATOR.validate(
        validate_screenplay(spoken),
        BRIEF,
        format_value="SHORT_DRAMA",
        provenance=USER_FACTS,
        prohibitions=["no talking heads"],
    )
    assert any(item.location == "beat 2" for item in found.blocking if item.code == "PROHIBITION_BREACHED")

    # The director agreeing with the user is not a breach.
    agreeing = copy.deepcopy(SCREENPLAY)
    agreeing["treatment"]["tone_direction"] = "cold rain, no violence, no blood"
    found = VALIDATOR.validate(
        validate_screenplay(agreeing),
        BRIEF,
        format_value="SHORT_DRAMA",
        provenance=USER_FACTS,
        prohibitions=["no violence"],
    )
    assert "PROHIBITION_BREACHED" not in _codes(found)

    # And a sentence that is not an instruction blocks nothing at all.
    found = VALIDATOR.validate(
        validate_screenplay(copy.deepcopy(SCREENPLAY)),
        BRIEF,
        format_value="SHORT_DRAMA",
        provenance=USER_FACTS,
        prohibitions=["主角别墅里有一把枪"],
    )
    assert "PROHIBITION_BREACHED" not in _codes(found)


def test_prohibitions_reach_the_shot_intent_the_negative_prompt_and_the_qc_checklist(container):  # type: ignore[no-untyped-def]
    constraints = shot_constraints(
        validate_screenplay(SCREENPLAY),
        prohibitions=["不要暴力"],
        prohibited_terms=["暴力"],
    )
    assert all(item.prohibitions == ("不要暴力",) for item in constraints)
    intent = director_intent({"action": "x", **constraints[0].as_json()})
    assert intent["prohibitions"] == ["不要暴力"] and intent["prohibited_terms"] == ["暴力"]

    spec = CanonicalShotSpec(
        intent="Mira enters the rooftop",
        dominant_action="Mira enters the rooftop",
        constraints=[f"{PROHIBITION_PREFIX}不要暴力", f"{FORBIDDEN_PREFIX}暴力"],
    )
    output = container.prompts.compile_input(
        PromptCompilerInput(
            shot_spec=spec.model_dump(mode="json"),
            asset_bindings=[],
            continuity_context=PromptContinuityContext(),
        )
    )
    assert output.status == "COMPILED", output.review_reason
    assert output.negative_prompt is not None and output.negative_prompt.endswith("暴力")
    assert "prohibition=不要暴力" in output.qc_checklist


async def test_a_prohibition_and_the_approved_frame_reach_every_compiled_shot(openrouter_container):  # type: ignore[no-untyped-def]
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, _user_id = _registered_pro(client, container, "prohibit@example.com")
        started = client.post(
            "/v1/creative/sessions", headers=headers, json={"project_id": project_id, "idea": RICH_IDEA}
        ).json()
        session_id = started["session_id"]
        # The user forbids something, and changes the frame in the brief editor.
        forbade = client.post(
            f"/v1/creative/sessions/{session_id}/messages",
            headers=headers,
            json={"content": "不要暴力"},
        )
        assert forbade.status_code == 200, forbade.text
        edited = client.post(
            f"/v1/creative/sessions/{session_id}/brief/edit",
            headers=headers,
            json={
                "operations": [
                    {"op": "REPLACE", "path": "aspect_ratio", "value": "16:9", "evidence": "brief editor"}
                ]
            },
        )
        assert edited.status_code == 200, edited.text
        _approve_brief(client, session_id, edited.json()["revision"], headers)
        _approve_screenplay(client, session_id, headers)
        await _complete_visuals(container, client, session_id, headers)
        bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose", headers=headers).json()
        assert bible["content"]["aspect_ratio"] == "16:9"
        locked = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": bible["version"]},
        )
        assert locked.status_code == 200, locked.text
        client.post(f"/v1/creative/sessions/{session_id}/beats/propose", headers=headers)
        compiled = client.post(
            f"/v1/creative/sessions/{session_id}/beats/approve",
            headers=headers,
            json={"plan_revision": 1},
        )
        assert compiled.status_code == 200, compiled.text
        shot_ids = compiled.json()["shot_ids"]

    with container.database.session() as session:
        assert session.get(Project, project_id).default_aspect_ratio == "9:16"
        intents = [dict(session.get(Shot, shot_id).director_intent_json) for shot_id in shot_ids]
    assert intents and all(intent["aspect_ratio"] == "16:9" for intent in intents)
    assert all(intent["prohibitions"] == ["不要暴力"] for intent in intents)
    assert all(intent["prohibited_terms"] == ["暴力"] for intent in intents)

    result = container.video_prompt_compiler.compile(shot_ids[0])
    assert result.spec.aspect_ratio == "16:9"
    assert f"{PROHIBITION_PREFIX}不要暴力" in result.spec.constraints
    assert f"{FORBIDDEN_PREFIX}暴力" in result.spec.constraints
    assert result.output.negative_prompt and result.output.negative_prompt.endswith("暴力")
    assert "prohibition=不要暴力" in result.output.qc_checklist
    assert "aspect_ratio=16:9" in result.output.qc_checklist


# --------------------------------------------------------------------------
# P1 · a call to action only in the treatment never reaches the screen
# --------------------------------------------------------------------------


def test_a_call_to_action_has_to_live_in_something_that_compiles() -> None:
    brief = {**BRIEF, "call_to_action": "download the app tonight"}
    prose_only = copy.deepcopy(SCREENPLAY)
    prose_only["treatment"]["ending"] = "Title card: download the app tonight."
    found = VALIDATOR.validate(
        validate_screenplay(prose_only), brief, format_value="SHORT_DRAMA", provenance=USER_FACTS
    )
    assert "CALL_TO_ACTION_MISSING" in _blocking(found)

    spoken = copy.deepcopy(SCREENPLAY)
    spoken["beats"][1]["shots"][1]["dialogue"]["text"] = "Download the app tonight, Mira."
    assert "CALL_TO_ACTION_MISSING" not in _codes(
        VALIDATOR.validate(
            validate_screenplay(spoken), brief, format_value="SHORT_DRAMA", provenance=USER_FACTS
        )
    )

    staged = copy.deepcopy(SCREENPLAY)
    staged["beats"][2]["shots"][0]["action"]["description"] = "on-screen title: download the app tonight"
    assert "CALL_TO_ACTION_MISSING" not in _codes(
        VALIDATOR.validate(
            validate_screenplay(staged), brief, format_value="SHORT_DRAMA", provenance=USER_FACTS
        )
    )


# --------------------------------------------------------------------------
# P1 · a user-stated selling point is preserved on the user's authority
# --------------------------------------------------------------------------


def _product_screenplay(*, must_preserve: bool | None = None) -> dict[str, Any]:
    content = copy.deepcopy(SCREENPLAY)
    content["beats"][0]["shots"][1]["action"]["object"] = "Aurora Serum"
    if must_preserve is not None:
        content["product_claims"] = [
            {"claim": "It absorbs in ten seconds", "must_preserve": must_preserve}
        ]
    return content


def test_a_user_stated_selling_point_missing_from_a_commerce_piece_blocks() -> None:
    brief = {
        **BRIEF,
        "format": "ADVERTISEMENT",
        "product": {"name": "Aurora Serum", "selling_points": ["absorbs in ten seconds"]},
    }
    stated = {**USER_FACTS, "product.selling_points": {"source": "USER_STATED"}}
    found = VALIDATOR.validate(
        validate_screenplay(_product_screenplay()), brief, format_value="ADVERTISEMENT", provenance=stated
    )
    assert "SELLING_POINT_MISSING" in _blocking(found)
    # The director's own inference is advice; so is a drama's product placement.
    inferred = {**USER_FACTS, "product.selling_points": {"source": "MODEL_INFERRED"}}
    found = VALIDATOR.validate(
        validate_screenplay(_product_screenplay()), brief, format_value="ADVERTISEMENT", provenance=inferred
    )
    assert "SELLING_POINT_MISSING" in _codes(found) and "SELLING_POINT_MISSING" not in _blocking(found)
    found = VALIDATOR.validate(
        validate_screenplay(_product_screenplay()), brief, format_value="SHORT_DRAMA", provenance=stated
    )
    assert "SELLING_POINT_MISSING" not in _blocking(found)


def test_a_selling_point_reaches_the_product_shot_whatever_the_director_flagged() -> None:
    screenplay = validate_screenplay(_product_screenplay(must_preserve=False))
    preserved = preserved_product_claims(screenplay, ["absorbs in ten seconds"])
    # The user's wording first, the director's restatement kept with it.
    assert preserved == ["absorbs in ten seconds", "It absorbs in ten seconds"]
    assert preserved_product_claims(screenplay) == []  # nothing the director chose to preserve
    constraints = shot_constraints(
        screenplay, product="Aurora Serum", selling_points=["absorbs in ten seconds"]
    )
    product_shots = [item for item in constraints if item.product_claims]
    assert len(product_shots) == 1
    assert product_shots[0].product_claims == ("absorbs in ten seconds", "It absorbs in ten seconds")


# --------------------------------------------------------------------------
# P1 · the same client_turn_id never pays for a second director call
# --------------------------------------------------------------------------


async def test_a_duplicate_message_during_the_director_call_is_refused_not_paid_for(container, project):  # type: ignore[no-untyped-def]
    director = ScriptedDirector(_rich_turn)
    service = container.creative_director
    service.model_roles = director
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
    calls = len(director.calls)
    content = "再悬疑一点"

    # The state a concurrent request leaves while the director is thinking.
    with container.database.session() as session:
        session.add(
            CreativeTurnClaim(
                session_id=session_id,
                client_turn_id="dup-1",
                claim_token=str(uuid.uuid4()),
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                claimed_at=utcnow(),
            )
        )
    with pytest.raises(CreativeSessionConflict) as refused:
        await service.post_message(session_id, content, client_turn_id="dup-1")
    assert refused.value.reason_code == "TURN_IN_PROGRESS" and refused.value.retryable
    assert len(director.calls) == calls, "the duplicate must not reach the model"

    # The same key with other words is a client bug, refused outright.
    with pytest.raises(CreativeSessionConflict) as mismatch:
        await service.post_message(session_id, "换个说法", client_turn_id="dup-1")
    assert mismatch.value.reason_code == "CLIENT_TURN_ID_CONTENT_MISMATCH"
    assert len(director.calls) == calls

    # A claim past its lease belongs to a process that died: taken over,
    # answered once, and released with the turn.
    with container.database.session() as session:
        claim = session.scalar(select(CreativeTurnClaim).where(CreativeTurnClaim.session_id == session_id))
        claim.claimed_at = utcnow() - _TURN_CLAIM_LEASE - timedelta(seconds=1)
    reply = await service.post_message(session_id, content, client_turn_id="dup-1")
    assert reply.replayed is False and len(director.calls) == calls + 1
    with container.database.session() as session:
        assert _claim_for(session, session_id) is None
    again = await service.post_message(session_id, content, client_turn_id="dup-1")
    assert again.replayed is True and len(director.calls) == calls + 1

    # An ordinary round leaves no claim behind either.
    landed = await service.post_message(session_id, "再长一点", client_turn_id="dup-2")
    assert landed.replayed is False
    with container.database.session() as session:
        assert _claim_for(session, session_id) is None


def test_a_retried_session_create_during_the_opening_call_is_refused_not_paid_for(container, project):  # type: ignore[no-untyped-def]
    director = ScriptedDirector(_rich_turn)
    container.creative_director.model_roles = director
    # The window: the session row exists (its create key is unique per
    # project) and its opening turn is still being answered.
    with container.database.session() as session:
        row = CreativeSession(project_id=project.id, title="opening", create_client_turn_id="open-1")
        session.add(row)
        session.flush()
        session.add(
            CreativeTurnClaim(
                session_id=row.id,
                client_turn_id="open-1",
                claim_token=str(uuid.uuid4()),
                content_hash=hashlib.sha256(RICH_IDEA.encode("utf-8")).hexdigest(),
                claimed_at=utcnow(),
            )
        )
        session_id = row.id
    with _client(container) as client:
        retried = client.post(
            "/v1/creative/sessions",
            json={"project_id": project.id, "idea": RICH_IDEA, "client_turn_id": "open-1"},
        )
    assert retried.status_code == 409, retried.text
    assert retried.json()["detail"]["reason_code"] == "TURN_IN_PROGRESS"
    assert retried.json()["detail"]["retryable"] is True
    assert director.calls == []
    with container.database.session() as session:
        sessions = list(
            session.scalars(select(CreativeSession).where(CreativeSession.project_id == project.id))
        )
        assert [item.id for item in sessions] == [session_id]


# --------------------------------------------------------------------------
# P1 · two names that collapse to one script token are refused
# --------------------------------------------------------------------------


def test_two_names_that_collapse_to_one_script_token_are_refused() -> None:
    content = copy.deepcopy(SCREENPLAY)
    content["characters"][1]["name"] = "Mary Jane"
    content["characters"].append({"name": "Mary-Jane", "role": "her double"})
    content["beats"][1]["characters"] = ["Mira", "Mary Jane"]
    content["beats"][1]["shots"][1]["dialogue"]["speaker"] = "Mary Jane"
    with pytest.raises(ScreenplayInvalid) as refused:
        validate_screenplay(content)
    assert "collapse to the same script name" in " ".join(refused.value.details)
    assert "Mary-Jane" in " ".join(refused.value.details)
    # Case alone collides too: the Character row is matched case-blind.
    content["characters"][2]["name"] = "MARY JANE"
    with pytest.raises(ScreenplayInvalid):
        validate_screenplay(content)


# --------------------------------------------------------------------------
# P2 · an invariant scoped to nobody is refused instead of lost
# --------------------------------------------------------------------------


def test_an_invariant_scoped_to_an_unknown_character_or_scene_is_refused() -> None:
    content = copy.deepcopy(SCREENPLAY)
    content["invariants"] = [{"text": "Mira never smiles", "characters": ["Mirra"]}]
    with pytest.raises(ScreenplayInvalid) as refused:
        validate_screenplay(content)
    assert "unknown character 'Mirra'" in " ".join(refused.value.details)
    content["invariants"] = [{"text": "the phone stays on the ledge", "scenes": ["nowhere"]}]
    with pytest.raises(ScreenplayInvalid) as refused:
        validate_screenplay(content)
    assert "unknown scene 'nowhere'" in " ".join(refused.value.details)
    content["invariants"] = [{"text": "Mira never smiles", "characters": ["mira"], "scenes": ["roof"]}]
    assert validate_screenplay(content).invariants[0].characters == ["mira"]


# --------------------------------------------------------------------------
# P2 · a scene plate binds to the Location the compiler named
# --------------------------------------------------------------------------


def test_a_scene_plate_finds_its_location_through_the_compilers_own_spelling(container, project):  # type: ignore[no-untyped-def]
    assert _location_keys("Tokyo, Shibuya crossing") == [
        "tokyo, shibuya crossing",
        "tokyo shibuya crossing",
    ]
    assert _location_keys("rooftop") == ["rooftop"]

    with container.database.session() as session:
        # The compiler names the Location after the cleaned scene heading.
        location = Location(project_id=project.id, name="Tokyo Shibuya crossing")
        session.add(location)
        session.flush()
        episode = Episode(project_id=project.id, title="E1", episode_number=1)
        session.add(episode)
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1, location_id=location.id)
        session.add(scene)
        session.flush()
        episode_id, location_id = episode.id, location.id
    plate = container.asset_registry.create(
        project.id, "SCENE", "Tokyo plate", canonical_metadata={"creative_anchor_key": "scene:x"}
    )
    lineage = {
        "assets": {
            "scene:tokyo, shibuya crossing": {
                "kind": "SCENE",
                "asset_id": plate.id,
                # The anchor names the screenplay's raw, punctuated location.
                "subject": "Tokyo, Shibuya crossing",
            }
        }
    }
    container.creative_director._bind_scene_locations(project.id, episode_id, lineage)
    bound = lineage["assets"]["scene:tokyo, shibuya crossing"]
    assert bound["location_bound"] is True and bound["location_id"] == location_id
    with container.database.session() as session:
        assert session.get(Asset, plate.id).canonical_metadata["location_id"] == location_id


# --------------------------------------------------------------------------
# P2 · a committed shot's memory names what retrieval filters on
# --------------------------------------------------------------------------


def test_a_committed_shot_memory_names_the_entities_retrieval_filters_on(container, project, register_bytes):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        episode = Episode(
            project_id=project.id,
            title="E1",
            episode_number=1,
            script_source="INT. ROOM - NIGHT\nMira raises the phone.\n",
        )
        session.add(episode)
        session.flush()
        episode_id = episode.id
    shot_id = container.narrative.compile_episode(episode_id).shot_ids[0]
    with container.database.session() as session:
        character = CharacterRow(project_id=project.id, name="Mira")
        session.add(character)
        session.flush()
        character_id = character.id
    registry = container.asset_registry
    face = register_bytes(container, project.id, "CHARACTER_MASTER", b"mira-face")
    identity = registry.create(
        project.id, "CHARACTER", "Mira", canonical_metadata={"character_id": character_id}
    )
    version = registry.add_version(
        identity.id, primary_media_asset_id=face.id, label="v1", source="CREATIVE_KEY_VISUAL"
    )
    registry.promote(identity.id, version.id, reason="test")
    plate_media = register_bytes(container, project.id, "REFERENCE", b"rooftop-plate")
    plate = registry.create(project.id, "SCENE", "Rooftop")
    plate_version = registry.add_version(
        plate.id, primary_media_asset_id=plate_media.id, label="v1", source="CREATIVE_KEY_VISUAL"
    )
    registry.promote(plate.id, plate_version.id, reason="test")
    unrelated = registry.create(project.id, "PROP", "Umbrella")
    unrelated_media = register_bytes(container, project.id, "REFERENCE", b"umbrella")
    unrelated_version = registry.add_version(
        unrelated.id, primary_media_asset_id=unrelated_media.id, label="v1", source="USER_UPLOAD"
    )
    registry.promote(unrelated.id, unrelated_version.id, reason="test")

    with container.database.session() as session:
        shot = session.get(Shot, shot_id)
        shot.director_intent_json = {"reference_asset_ids": [plate_media.id]}
        candidate = GenerationCandidate(
            shot_id=shot_id,
            attempt_number=1,
            status="CREATED",
            metadata_json={"character_state_context": [{"character_id": character_id}]},
        )
        session.add(candidate)
        session.flush()
        entity_ids = CandidatePipeline._shot_memory_entity_ids(session, shot, candidate)
    assert set(entity_ids) == {character_id, identity.id, plate.id}
    # And the wiring: the commit path passes them through the outbox.
    assert "entity_ids=self._shot_memory_entity_ids(session, shot, candidate)" in _commit_source()


def _commit_source() -> str:
    import inspect

    return inspect.getsource(CandidatePipeline.commit)


# --------------------------------------------------------------------------
# P2 · a degraded memory is re-embedded in place, never duplicated
# --------------------------------------------------------------------------


class _FlakyEmbeddings:
    """An embedding provider that is down until told otherwise."""

    def __init__(self) -> None:
        self.down = True
        self.inner = LocalTestEmbeddingProvider()

    def embed_with_provenance(self, content, *, input_type, project_id):  # type: ignore[no-untyped-def]
        if self.down:
            raise MemoryEmbeddingUnavailable("voyage is unreachable")
        return self.inner.embed_with_provenance(content, input_type=input_type, project_id=project_id)


def test_a_degraded_memory_is_re_embedded_in_place_not_duplicated(container, project) -> None:  # type: ignore[no-untyped-def]
    embeddings = _FlakyEmbeddings()
    engine = MultimodalMemoryEngine(container.database, embeddings, enabled=True)
    writer = MemoryIndexOutboxWriter(container.database)
    worker = MemoryIndexOutboxWorker(container.database, engine, flags=None)
    assert writer.enqueue(
        project.id,
        idempotency_key="reindex:style",
        source="VISUAL_BIBLE_LOCK",
        memory_type="STYLE",
        text="teal night, single amber light source",
    )

    first = worker.drain()
    assert first.retried == 1 and first.indexed == 0
    with container.database.session() as session:
        row = session.scalar(select(MemoryIndexOutbox).where(MemoryIndexOutbox.project_id == project.id))
        memories = list(session.scalars(select(ShotMemory).where(ShotMemory.project_id == project.id)))
        assert row.status == "PENDING" and row.last_error == "VECTOR_DEGRADED"
        assert len(memories) == 1 and memories[0].metadata_json["vector_degraded"] is True
        assert row.shot_memory_id == memories[0].id
        memory_id = memories[0].id
        row.next_attempt_at = utcnow() - timedelta(seconds=1)

    embeddings.down = False
    second = worker.drain()
    assert second.indexed == 1 and second.retried == 0
    with container.database.session() as session:
        row = session.scalar(select(MemoryIndexOutbox).where(MemoryIndexOutbox.project_id == project.id))
        memories = list(session.scalars(select(ShotMemory).where(ShotMemory.project_id == project.id)))
    assert row.status == "DONE" and row.last_error is None
    assert len(memories) == 1, "the retry must not append a second memory"
    assert memories[0].id == memory_id
    assert memories[0].embedding_provider == "local_test" and memories[0].embedding
    assert "vector_degraded" not in memories[0].metadata_json
    assert memories[0].metadata_json["reembedded"] is True

    # A retry while the provider is still down leaves the same single row.
    embeddings.down = True
    assert engine.reindex(memory_id, engine_input(project.id)).metadata_json.get("reembedded") is True
    with container.database.session() as session:
        assert len(list(session.scalars(select(ShotMemory).where(ShotMemory.project_id == project.id)))) == 1


def engine_input(project_id: str):  # type: ignore[no-untyped-def]
    from memory_core import MemoryLayer
    from memory_core.schemas import MultimodalContent, ShotMemoryInput

    return ShotMemoryInput(
        project_id=project_id,
        layer=MemoryLayer.EPISODIC,
        memory_type="STYLE",
        content=MultimodalContent(text="teal night"),
    )


# --------------------------------------------------------------------------
# P2 · a partial callback keeps the job under its deadline
# --------------------------------------------------------------------------


def test_a_partial_callback_leaves_the_job_accepted_until_the_rest_arrive(container, project):  # type: ignore[no-untyped-def]
    candidate_id, (first, second) = _seed_two_hander(container, project)
    tracker = _tracker(container, _AcceptingProducer())
    tracker.enqueue_ready_candidates()
    tracker.dispatch_pending()
    tracker.record_character_report(
        candidate_id, character_id=first, producer_run_id="run-1", decision="ABSTAIN", qa_result_id=None
    )
    tracker.record_callback(candidate_id, status="SUCCEEDED", character_ids=[first])
    with container.database.session() as session:
        submission = _submission_for(session, candidate_id)
        assert submission.status == "ACCEPTED" and submission.reported_at is None
        assert submission.metadata_json["reported_character_ids"] == [first]
        assert submission.metadata_json["missing_character_ids"] == [second]
        assert submission.metadata_json["partial_callbacks"] == 1
    coverage = {item["character_id"]: item["status"] for item in tracker.coverage(candidate_id)}
    assert coverage == {first: "REPORTED", second: "REQUESTED"}

    # The rest arrives: now it is reported.
    tracker.record_character_report(
        candidate_id, character_id=second, producer_run_id="run-2", decision="ABSTAIN", qa_result_id=None
    )
    tracker.record_callback(candidate_id, status="SUCCEEDED", character_ids=[second])
    with container.database.session() as session:
        submission = _submission_for(session, candidate_id)
        assert submission.status == "REPORTED" and submission.reported_at is not None
        assert submission.metadata_json["reported_character_ids"] == [first, second]
        assert submission.metadata_json["missing_character_ids"] == []


def test_a_character_that_never_reports_is_settled_by_the_deadline(container, project):  # type: ignore[no-untyped-def]
    candidate_id, (first, second) = _seed_two_hander(container, project)
    tracker = _tracker(container, _AcceptingProducer())
    tracker.enqueue_ready_candidates()
    tracker.dispatch_pending()
    tracker.record_character_report(
        candidate_id, character_id=first, producer_run_id="run-1", decision="ABSTAIN", qa_result_id=None
    )
    tracker.record_callback(candidate_id, status="SUCCEEDED", character_ids=[first])
    assert tracker.scan_accepted_timeouts() == 0
    with container.database.session() as session:
        submission = _submission_for(session, candidate_id)
        submission.accepted_at = utcnow() - timedelta(seconds=tracker.callback_timeout_seconds + 1)
    assert tracker.scan_accepted_timeouts() == 1
    with container.database.session() as session:
        submission = _submission_for(session, candidate_id)
        rows = {
            row.character_id: row
            for row in session.scalars(
                select(CharacterEvidenceCoverage).where(
                    CharacterEvidenceCoverage.candidate_id == candidate_id
                )
            )
        }
        assert submission.status == "RECONCILIATION_REQUIRED"
        assert "only 1 of the requested characters reported" in submission.reconciliation_note
        assert second in submission.reconciliation_note
        assert rows[first].status == "REPORTED"
        assert rows[second].status == "FAILED"
        assert rows[second].failure_reason == "NO_REPORT_BEFORE_DEADLINE"


def test_the_webhook_says_which_characters_are_still_owed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient
    from video_platform_api.main import create_app

    container = _webhook_container(tmp_path)
    try:
        with container.database.session() as session:
            project = Project(title="Evidence project")
            session.add(project)
            session.flush()
            project_row = project
        candidate_id, character_ids = _seed_two_hander(container, project_row)
        tracker = _tracker(container, _AcceptingProducer())
        tracker.enqueue_ready_candidates()
        tracker.dispatch_pending()
        with container.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            shot_id = candidate.shot_id
            project_id = project_row.id
        first, second = character_ids
        payload = {
            "job_id": candidate_id,
            "project_id": project_id,
            "shot_id": shot_id,
            "status": "SUCCEEDED",
            "reports": _report_payloads(candidate_id, [first]),
        }
        with TestClient(create_app(container)) as client:
            response = _signed_callback(client, container, payload)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["characters"] == [first]
            assert body["complete"] is False
            assert body["missing_characters"] == [second]
        with container.database.session() as session:
            submission = _submission_for(session, candidate_id)
            assert submission.status == "ACCEPTED"
            assert submission.metadata_json["missing_character_ids"] == [second]
    finally:
        container.database.engine.dispose()
