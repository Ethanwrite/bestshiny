"""The creative director: dialogue, brief, screenplay, key visuals, bible lock, compile.

What these tests pin, one per defect class:

1. the DIRECTOR model is called through the Director Skill with the whole
   conversation, its own earlier questions, the structured brief with
   provenance and question states, and the user's latest message;
2. the user can correct facts (location, duration, style), extend a
   character's look, add / change / remove a second character - and a model
   inference can never overwrite a user fact;
3. "asked" is not "answered": approval is refused while a CRITICAL field is
   open, and allowed once the user answers or explicitly accepts the
   director's assumption; the backend enforces it, not a hidden button;
4. malformed nested model output degrades on record, never a 500;
5. the model writes an original treatment, beats, dialogue and screenplay
   (a real DIRECTOR reply is simulated); the fixed scaffold appears only when
   the model is unavailable and is labelled DETERMINISTIC;
6. only the exact approved screenplay revision - including the user's edits -
   reaches the narrative compiler, idempotently;
7. key visuals sync through the ordinary gateway path; the visual bible is
   refused until every required anchor is READY; locking it produces real
   CharacterIdentityVersion rows and a ProjectStyleLock through the platform's
   own services; compiled shots trace back to brief, screenplay, bible and
   anchors;
8. one dialogue round is one transaction: a crash leaves no orphan turn and
   spends no FREE round; retries never duplicate episodes, shots, actions or
   paid jobs.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from production_domain.models import (
    Character,
    CharacterIdentityVersion,
    CreativeAction,
    CreativeScreenplayRevision,
    CreativeSession,
    CreativeShotLineage,
    CreativeTurn,
    CreativeVisualAnchor,
    Episode,
    GenerationJob,
    NarrativeObligation,
    Project,
    ProjectStyleLock,
    Scene,
    Shot,
    User,
    VisualBibleVersion,
    Workspace,
)
from provider_sdk.transport import MockProviderTransport, ProviderHttpResponse
from skill_core import SkillRegistry
from sqlalchemy import select
from video_platform_api.main import create_app

RICH_IDEA = (
    "I want a 30 second suspenseful short drama on TikTok, vertical 9:16, "
    "cinematic live-action, protagonist is Mira, set in rooftop at night. "
    "She finds a phone that is not hers."
)
VAGUE_IDEA = "帮我做一个短剧"
SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"


def _client(container) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(create_app(container))


def _start(client: TestClient, project_id: str, idea: str, **extra: Any) -> dict:
    response = client.post("/v1/creative/sessions", json={"project_id": project_id, "idea": idea, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def _state(client: TestClient, session_id: str, headers: dict | None = None) -> dict:
    response = client.get(f"/v1/creative/sessions/{session_id}", headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


def _execution(payload: dict[str, Any], *, record_id: str = "exec-1"):  # type: ignore[no-untyped-def]
    content = json.dumps(payload, ensure_ascii=False)
    return type(
        "Execution",
        (),
        {"response": {"choices": [{"message": {"content": content}}]}, "execution_record_id": record_id},
    )()


def _latest_state_block(messages: list[dict]) -> dict:
    """The structured state block the service appends as the final user message."""

    return json.loads(messages[-1]["content"])


SCREENPLAY = {
    "treatment": {
        "title": "The Wrong Phone",
        "premise": "Mira finds a stranger's phone on a rooftop at night and realizes it is ringing for her.",
        "hook": {
            "opening_question": "Whose phone is this, and why does it know her name?",
            "promise": "Every answer costs Mira something she cannot get back.",
            "audience_feeling": "held breath",
        },
        "audience_expectation": "A suspense hook that pays off in thirty seconds.",
        "tone_direction": "cold rain, warm screen glow",
        "visual_direction": "cinematic live-action, teal night, single amber light source",
        "ending": "The phone rings again; the caller ID shows Mira's own number.",
    },
    "invariants": ["protagonist is Mira", "rooftop at night", "the phone is not hers"],
    "variables": ["hook framing", "pacing", "coverage"],
    "characters": [
        {
            "name": "Mira",
            "role": "protagonist",
            "look": "black coat, wet hair, no umbrella",
            "wants": "to leave",
        },
        {
            "name": "Ren",
            "role": "the caller",
            "look": "unseen, voice only",
            "relationships": [{"with": "Mira", "relation": "stranger"}],
        },
    ],
    "scenes": [
        {
            "key": "roof",
            "location": "rooftop",
            "time": "NIGHT",
            "interior": False,
            "description": "rain, city lights below",
        }
    ],
    "beats": [
        {
            "sequence": 1,
            "intent": "COLD_OPEN",
            "summary": "Mira steps onto the rooftop and sees a phone glowing on the ledge.",
            "scene_key": "roof",
            "characters": ["Mira"],
            "emotional_beat": "unease",
            "shots": [
                {
                    "sequence": 1,
                    "shot_type": "WIDE",
                    "duration": 5,
                    "action": {
                        "actor": "Mira",
                        "verb": "enter",
                        "description": "she walks into the rain, phone glowing ahead",
                    },
                    "start_state": "rooftop empty, phone glowing on the ledge",
                    "end_state": "Mira at the ledge",
                    "gaze_target": "the phone",
                    "continuity_obligations": ["the phone stays on the ledge until picked up"],
                },
                {
                    "sequence": 2,
                    "shot_type": "CLOSE",
                    "duration": 4,
                    "action": {
                        "actor": "Mira",
                        "verb": "pick_up",
                        "object": "phone",
                        "description": "hesitant",
                    },
                    "start_state": "phone on the ledge",
                    "end_state": "phone in Mira's right hand",
                    "gaze_target": "the phone screen",
                },
            ],
        },
        {
            "sequence": 2,
            "intent": "TURN",
            "summary": "The phone rings. The screen shows her own name.",
            "scene_key": "roof",
            "characters": ["Mira", "Ren"],
            "emotional_beat": "dread",
            "shots": [
                {
                    "sequence": 1,
                    "shot_type": "DIALOGUE",
                    "duration": 6,
                    "dialogue": {"speaker": "Mira", "text": "This isn't mine. Who put my name on it?"},
                    "start_state": "phone ringing in hand",
                    "end_state": "call answered",
                    "gaze_target": "the phone",
                },
                {
                    "sequence": 2,
                    "shot_type": "DIALOGUE",
                    "duration": 6,
                    "dialogue": {"speaker": "Ren", "text": "You did, Mira. Three days from now."},
                    "start_state": "call open",
                    "end_state": "Mira frozen",
                    "gaze_target": "off, into the rain",
                },
            ],
        },
        {
            "sequence": 3,
            "intent": "CLIFFHANGER",
            "summary": "Mira drops the call. The phone rings again from her own number.",
            "scene_key": "roof",
            "characters": ["Mira"],
            "emotional_beat": "vertigo",
            "shots": [
                {
                    "sequence": 1,
                    "shot_type": "CLOSE",
                    "duration": 5,
                    "action": {"actor": "Mira", "verb": "stop", "description": "she freezes at the ledge"},
                    "start_state": "Mira turning away",
                    "end_state": "Mira still, phone ringing",
                    "gaze_target": "the phone",
                },
            ],
        },
    ],
    "product_claims": [],
    "required_copy": [],
    "obligations": [
        {
            "key": "who_is_ren",
            "promise": "reveal who Ren is and how the phone knows Mira",
            "category": "MYSTERY",
        }
    ],
    "unresolved": ["whether Ren is ever seen"],
}


class ScriptedDirector:
    """A DIRECTOR model that answers turns and writes screenplays under the new contract."""

    def __init__(self, turn_handler=None, screenplay=None, *, raise_with: Exception | None = None):  # type: ignore[no-untyped-def]
        self.turn_handler = turn_handler
        self.screenplay = screenplay if screenplay is not None else SCREENPLAY
        self.raise_with = raise_with
        self.calls: list[dict[str, Any]] = []

    async def execute_chat(self, project_id, role, *, messages, parameters=None):  # type: ignore[no-untyped-def]
        self.calls.append(
            {"project_id": project_id, "role": role, "messages": list(messages), "parameters": parameters}
        )
        if self.raise_with is not None:
            raise self.raise_with
        request = _latest_state_block(messages)
        if request.get("task") in {"WRITE_SCREENPLAY", "REVISE_SCREENPLAY"}:
            payload = self.screenplay(request) if callable(self.screenplay) else self.screenplay
            return _execution(payload, record_id=f"exec-screenplay-{len(self.calls)}")
        latest = request.get("latest_client_message", "")
        if self.turn_handler is None:
            payload = {"assistant_message": f"回合{len(self.calls)}：明白。", "brief_operations": []}
        else:
            payload = self.turn_handler(latest, request)
        return _execution(payload, record_id=f"exec-turn-{len(self.calls)}")


def _rich_turn(latest: str, state: dict) -> dict:
    """A model reading of RICH_IDEA: everything the user said, as USER_STATED operations."""

    if not state.get("brief"):
        return {
            "assistant_message": (
                "Got it: a thirty-second rooftop suspense piece for TikTok, with Mira. "
                "I have what I need - approve when you're ready."
            ),
            "brief_operations": [
                {
                    "op": "SET",
                    "path": "format",
                    "value": "SHORT_DRAMA",
                    "evidence": "short drama",
                    "confidence": "USER_STATED",
                },
                {
                    "op": "SET",
                    "path": "logline",
                    "value": "Mira finds a phone that is not hers on a rooftop at night.",
                    "evidence": "She finds a phone that is not hers",
                    "confidence": "USER_STATED",
                },
                {
                    "op": "SET",
                    "path": "duration_seconds",
                    "value": 30,
                    "evidence": "30 second",
                    "confidence": "USER_STATED",
                },
                {
                    "op": "SET",
                    "path": "platform",
                    "value": "tiktok",
                    "evidence": "on TikTok",
                    "confidence": "USER_STATED",
                },
                {
                    "op": "SET",
                    "path": "aspect_ratio",
                    "value": "9:16",
                    "evidence": "vertical 9:16",
                    "confidence": "USER_STATED",
                },
                {
                    "op": "SET",
                    "path": "visual_style",
                    "value": {"medium": "cinematic live-action"},
                    "evidence": "cinematic live-action",
                    "confidence": "USER_STATED",
                },
                {
                    "op": "SET",
                    "path": "tone",
                    "value": ["suspenseful"],
                    "evidence": "suspenseful",
                    "confidence": "USER_STATED",
                },
                {
                    "op": "UPSERT",
                    "path": "characters",
                    "value": {"name": "Mira", "role": "protagonist"},
                    "evidence": "protagonist is Mira",
                    "confidence": "USER_STATED",
                },
                {
                    "op": "SET",
                    "path": "setting",
                    "value": {"location": "rooftop", "time": "NIGHT"},
                    "evidence": "set in rooftop at night",
                    "confidence": "USER_STATED",
                },
            ],
            "answered_question_codes": [
                "FORMAT",
                "LOGLINE",
                "DURATION",
                "PLATFORM",
                "VISUAL_STYLE",
                "TONE",
                "PROTAGONIST",
                "SETTING",
            ],
            "creative_notes": ["one light source, teal night"],
        }
    return {"assistant_message": "Noted.", "brief_operations": []}


def _png_response() -> ProviderHttpResponse:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (30, 90, 200)).save(buffer, format="PNG")
    return ProviderHttpResponse(
        200,
        {
            "created": 1_782_264_714,
            "data": [
                {
                    "b64_json": base64.b64encode(buffer.getvalue()).decode(),
                    "media_type": "image/png",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 196, "total_tokens": 208},
        },
    )


@pytest.fixture
def openrouter_container(tmp_path):  # type: ignore[no-untyped-def]
    """A container whose IMAGE_GENERATION role resolves to OpenRouter.

    The credential is an offline placeholder: the transport is swapped for a
    fixture before any call, so nothing can leave the process.
    """

    from platform_shared import Settings
    from video_platform_api.container import build_container

    built = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'platform.db'}",
            storage_root=tmp_path / "media",
            public_base_url="http://testserver",
            openrouter_api_key="offline-placeholder-never-sent",
            auth_required=False,
            deployment_environment="test",
        )
    )
    try:
        yield built
    finally:
        built.database.engine.dispose()


def _wire_openrouter_images(container) -> None:  # type: ignore[no-untyped-def]
    from model_registry_core import ModelRole
    from openrouter_provider import OpenRouterProvider
    from production_domain.models import BrowserWorker, ProviderAccount

    resolved = container.model_infrastructure.resolve_role(ModelRole.IMAGE_GENERATION)
    provider = container.providers.get(resolved.provider)
    assert isinstance(provider, OpenRouterProvider)
    provider.client.transport = MockProviderTransport({("POST", "/images"): _png_response()})
    with container.database.session() as session:
        account = ProviderAccount(
            provider=resolved.provider,
            account_identifier="openrouter@example.com",
            tier="PRO",
            credits=100,
            image_capacity=8,
            video_capacity=2,
            supported_models=[resolved.provider_model_id],
        )
        session.add(account)
        session.flush()
        worker = BrowserWorker(
            id="creative-openrouter-worker",
            provider=resolved.provider,
            account_id=account.id,
            connection_id="creative-connection",
            capabilities=["image", "poll"],
            max_jobs=8,
        )
        session.add(worker)
        account.worker_id = worker.id
        session.flush()


def _registered_pro(client: TestClient, container, email: str) -> tuple[dict, str, str]:  # type: ignore[no-untyped-def]
    """A real user with a PRO workspace and a project in it: the production path."""

    registered = client.post(
        "/api/auth/register", json={"email": email, "password": "correct horse battery staple"}
    )
    assert registered.status_code in {200, 201}, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    project = client.post("/v1/projects", headers=headers, json={"title": "Director E2E"}).json()
    with container.database.session() as session:
        row = session.get(Project, project["id"])
        workspace = session.get(Workspace, row.workspace_id)
        workspace.plan_tier = "PRO"
        workspace.credit_balance = 500
        user = session.scalar(select(User).where(User.email == email))
        user_id = user.id
    return headers, project["id"], user_id


async def _complete_visuals(
    container, client: TestClient, session_id: str, headers: dict | None = None
) -> dict:  # type: ignore[no-untyped-def]
    """Drive every key-visual job through the real gateway completion, then sync."""

    view = _state(client, session_id, headers)
    job_ids = [anchor["generation_job_id"] for anchor in view["anchors"] if anchor["generation_job_id"]]
    assert job_ids, view["anchors"]
    for job_id in job_ids:
        completed = await container.gateway.process(job_id)
        assert completed.status == "COMPLETED", (job_id, completed.status, completed.error_message)
    synced = client.post(f"/v1/creative/sessions/{session_id}/visuals/sync", headers=headers or {})
    assert synced.status_code == 200, synced.text
    return synced.json()


def _approve_brief(
    client: TestClient, session_id: str, revision: int, headers: dict | None = None, **extra: Any
) -> dict:
    response = client.post(
        f"/v1/creative/sessions/{session_id}/brief/approve",
        headers=headers or {},
        json={"revision": revision, **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _approve_screenplay(
    client: TestClient, session_id: str, headers: dict | None = None, **extra: Any
) -> dict:
    view = _state(client, session_id, headers)
    response = client.post(
        f"/v1/creative/sessions/{session_id}/screenplay/approve",
        headers=headers or {},
        json={"revision": view["screenplay"]["revision"], **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------- dialogue
def test_vague_idea_asks_only_high_value_questions_and_never_repeats_answered_ones(container, project):
    with _client(container) as client:
        started = _start(client, project.id, VAGUE_IDEA)
        assert started["status"] == "CLARIFYING"
        first_questions = started["questions"]
        assert 1 <= len(first_questions) <= 3
        first_codes = {question["code"] for question in first_questions}
        # The format was stated ("短剧"), so the director must not ask for it.
        assert "FORMAT" not in first_codes
        view = _state(client, started["session_id"])
        states = view["brief"]["question_states"]
        assert all(states[code]["status"] == "ASKED" for code in first_codes)

        reply = client.post(
            f"/v1/creative/sessions/{started['session_id']}/messages",
            json={"content": "就叫《雨夜》，主角是雨桐，在天台，大概60秒，悬疑一点"},
        )
        assert reply.status_code == 200, reply.text
        second_codes = {question["code"] for question in reply.json()["questions"]}
        view = _state(client, started["session_id"])
        states = view["brief"]["question_states"]
        answered = {code for code, state in states.items() if state["status"] == "ANSWERED"}
        assert {"PROTAGONIST", "SETTING", "DURATION"} <= answered
        assert not (second_codes & answered), "an answered question was asked again"


def test_rich_idea_needs_no_questions_and_proposes_a_brief(container, project):
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        assert started["questions"] == []
        assert started["proposable"] is True
        assert started["status"] == "BRIEF_PROPOSED"
        # Mock mode has no reachable DIRECTOR model; the degradation is
        # recorded, never silent.
        assert started["reasoner"] == "DETERMINISTIC"
        assert (
            "MODEL_UNAVAILABLE" in started["reason_codes"]
            or "MODEL_RUNTIME_NOT_CONFIGURED" in started["reason_codes"]
        )

        state = _state(client, started["session_id"])
        fields = state["brief"]["fields"]
        assert fields["format"] == "SHORT_DRAMA"
        assert fields["duration_seconds"] == 30
        assert fields["aspect_ratio"] == "9:16"
        assert fields["characters"][0]["name"] == "Mira"
        assert fields["setting"]["location"].lower().startswith("rooftop")
        # Every value came from the user's own words.
        provenance = state["brief"]["provenance"]
        assert provenance["duration_seconds"]["source"] == "USER_STATED"
        assert provenance["characters/mira"]["source"] == "USER_STATED"


def test_model_call_carries_the_director_skill_and_the_whole_conversation(container, project):
    director = ScriptedDirector(
        turn_handler=lambda latest, state: {
            "assistant_message": f"我看到了{len(state.get('question_states') or {})}个问题状态。",
            "brief_operations": [],
            "unresolved_questions": [{"code": "LOGLINE", "question": "这个故事的核心是什么？"}],
        }
    )
    container.creative_director.model_roles = director
    skill = SkillRegistry(SKILLS_ROOT).resolve("director")
    with _client(container) as client:
        started = _start(client, project.id, VAGUE_IDEA)
        assert started["reasoner"] == "MODEL:DIRECTOR"
        session_id = started["session_id"]
        second = client.post(
            f"/v1/creative/sessions/{session_id}/messages", json={"content": "主角叫雨桐，30秒竖屏"}
        )
        assert second.status_code == 200, second.text
        view = _state(client, session_id)

    first_call, second_call = director.calls
    # 1. The Director Skill is the system prompt, verbatim and content-addressed.
    assert first_call["messages"][0]["role"] == "system"
    assert skill.system_prompt in first_call["messages"][0]["content"]
    assert "Approval is not encouragement" in first_call["messages"][0]["content"]
    director_turns = [turn for turn in view["turns"] if turn["speaker"] == "DIRECTOR"]
    assert director_turns[0]["skill_content_hash"] == skill.content_hash
    assert director_turns[0]["skill_version"] == skill.version
    assert director_turns[0]["model_execution_record_id"] == "exec-turn-1"
    assert "SKILL_LOADED" in director_turns[0]["reason_codes"]
    # 2. The second call carries the ordered conversation - the idea, the
    #    director's first reply with its questions, and the new message.
    roles = [message["role"] for message in second_call["messages"]]
    assert roles[0] == "system" and roles[-1] == "user"
    assert "assistant" in roles and roles.count("user") >= 2
    assistant = second_call["messages"][roles.index("assistant")]["content"]
    assert assistant.startswith("我看到了")
    assert "这个故事的核心是什么" in assistant  # the earlier question travels with the reply
    state_block = _latest_state_block(second_call["messages"])
    assert state_block["latest_client_message"] == "主角叫雨桐，30秒竖屏"
    assert state_block["stage"] == "CLARIFYING"
    assert state_block["brief"]["format"] == "SHORT_DRAMA"
    assert state_block["question_states"]["LOGLINE"]["status"] == "ASKED"
    assert "field_provenance" in state_block and "preserved" in state_block
    assert "client_established_facts" in state_block["preserved"]
    # 3. Nothing about providers, models, quotas or retries reaches the creative
    #    context (the Skill's own text is the only place the words may appear).
    joined = json.dumps(second_call["messages"][1:], ensure_ascii=False).lower()
    for forbidden in ("openrouter", "opus", "credit_balance", "retry", "provider_model_id", "plan_tier"):
        assert forbidden not in joined, forbidden


def test_the_user_can_correct_location_duration_and_style(container, project):
    def handler(latest: str, state: dict) -> dict:
        if not state.get("brief"):
            return _rich_turn(latest, state)
        if "换到" in latest:
            return {
                "assistant_message": "好，地点改到地铁站，时长改成45秒，风格改成动画。",
                "brief_operations": [
                    {
                        "op": "REPLACE",
                        "path": "setting.location",
                        "value": "地铁站",
                        "evidence": "换到地铁站",
                        "confidence": "USER_STATED",
                    },
                    {
                        "op": "REPLACE",
                        "path": "duration_seconds",
                        "value": 45,
                        "evidence": "45秒",
                        "confidence": "USER_STATED",
                    },
                    {
                        "op": "REPLACE",
                        "path": "visual_style.medium",
                        "value": "anime",
                        "evidence": "改成动画",
                        "confidence": "USER_STATED",
                    },
                ],
            }
        return {"assistant_message": "Noted.", "brief_operations": []}

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        before = _state(client, session_id)["brief"]["fields"]
        assert before["setting"]["location"] == "rooftop" and before["duration_seconds"] == 30
        reply = client.post(
            f"/v1/creative/sessions/{session_id}/messages",
            json={"content": "换到地铁站，45秒，改成动画"},
        )
        assert reply.status_code == 200, reply.text
        view = _state(client, session_id)
    fields = view["brief"]["fields"]
    assert fields["setting"]["location"] == "地铁站"
    assert fields["duration_seconds"] == 45
    assert fields["visual_style"]["medium"] == "anime"
    provenance = view["brief"]["provenance"]
    assert provenance["setting.location"]["operation"] == "REPLACE"
    assert provenance["setting.location"]["source"] == "USER_STATED"
    assert provenance["setting.location"]["evidence"] == "换到地铁站"
    assert provenance["setting.location"]["turn_sequence"] == 3
    # The revision history is append-only: the correction is a new revision.
    assert view["brief"]["revision"] == 2


def test_an_existing_character_can_gain_a_look_and_a_second_character_can_be_added_changed_and_removed(
    container, project
):
    def handler(latest: str, state: dict) -> dict:
        if not state.get("brief"):
            return _rich_turn(latest, state)
        if "穿" in latest:
            return {
                "assistant_message": "记下了：Mira穿黑色风衣。",
                "brief_operations": [
                    {
                        "op": "UPSERT",
                        "path": "characters",
                        "value": {"name": "Mira", "look": "black trench coat, wet hair"},
                        "evidence": "Mira穿黑色风衣",
                        "confidence": "USER_STATED",
                    }
                ],
            }
        if "加一个" in latest:
            return {
                "assistant_message": "加入第二个角色Ren。",
                "brief_operations": [
                    {
                        "op": "UPSERT",
                        "path": "characters",
                        "value": {"name": "Ren", "role": "the caller"},
                        "evidence": "加一个角色Ren",
                        "confidence": "USER_STATED",
                    }
                ],
            }
        if "Ren是" in latest:
            return {
                "assistant_message": "Ren改成Mira的哥哥。",
                "brief_operations": [
                    {
                        "op": "UPSERT",
                        "path": "characters",
                        "value": {"name": "Ren", "role": "Mira's brother"},
                        "evidence": "Ren是Mira的哥哥",
                        "confidence": "USER_STATED",
                    }
                ],
            }
        if "删掉" in latest:
            return {
                "assistant_message": "删掉Ren。",
                "brief_operations": [
                    {
                        "op": "REMOVE",
                        "path": "characters",
                        "value": {"name": "Ren"},
                        "evidence": "删掉Ren",
                        "confidence": "USER_STATED",
                    }
                ],
            }
        return {"assistant_message": "Noted.", "brief_operations": []}

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]

        def say(text: str) -> dict:
            response = client.post(f"/v1/creative/sessions/{session_id}/messages", json={"content": text})
            assert response.status_code == 200, response.text
            return _state(client, session_id)["brief"]

        brief = say("Mira穿黑色风衣")
        assert brief["fields"]["characters"] == [
            {"name": "Mira", "role": "protagonist", "look": "black trench coat, wet hair"}
        ]
        brief = say("加一个角色Ren")
        assert [member["name"] for member in brief["fields"]["characters"]] == ["Mira", "Ren"]
        assert brief["provenance"]["characters/ren"]["source"] == "USER_STATED"
        brief = say("Ren是Mira的哥哥")
        assert brief["fields"]["characters"][1] == {"name": "Ren", "role": "Mira's brother"}
        brief = say("删掉Ren")
        assert [member["name"] for member in brief["fields"]["characters"]] == ["Mira"]
        assert "characters/ren" not in brief["provenance"]
        assert brief["fields"]["characters"][0]["look"] == "black trench coat, wet hair"


def test_a_model_inference_cannot_overwrite_a_user_fact(container, project):
    def handler(latest: str, state: dict) -> dict:
        if not state.get("brief"):
            return _rich_turn(latest, state)
        return {
            "assistant_message": "I'd move this to a subway platform and make it a minute.",
            "brief_operations": [
                {
                    "op": "REPLACE",
                    "path": "setting.location",
                    "value": "subway platform",
                    "evidence": "",
                    "confidence": "INFERRED",
                },
                {
                    "op": "REPLACE",
                    "path": "duration_seconds",
                    "value": 60,
                    "evidence": "",
                    "confidence": "INFERRED",
                },
                {
                    "op": "REMOVE",
                    "path": "characters",
                    "value": {"name": "Mira"},
                    "evidence": "",
                    "confidence": "INFERRED",
                },
                {
                    "op": "SET",
                    "path": "visual_style.palette",
                    "value": "teal and amber",
                    "evidence": "",
                    "confidence": "INFERRED",
                },
            ],
        }

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        reply = client.post(
            f"/v1/creative/sessions/{session_id}/messages", json={"content": "what would you change?"}
        )
        assert reply.status_code == 200, reply.text
        assert "OPERATIONS_REJECTED" in reply.json()["reason_codes"]
        view = _state(client, session_id)
    fields = view["brief"]["fields"]
    assert fields["setting"]["location"] == "rooftop"
    assert fields["duration_seconds"] == 30
    assert fields["characters"][0]["name"] == "Mira"
    # An inference may still fill an empty field - as an assumption, on record.
    assert fields["visual_style"]["palette"] == "teal and amber"
    assert view["brief"]["provenance"]["visual_style.palette"]["source"] == "MODEL_INFERRED"
    director_turn = [turn for turn in view["turns"] if turn["speaker"] == "DIRECTOR"][-1]
    rejected = {(item["path"], item["reason"]) for item in director_turn["result"]["rejected_operations"]}
    assert ("setting.location", "INFERRED_CANNOT_OVERRIDE_USER_FACT") in rejected
    assert ("duration_seconds", "INFERRED_CANNOT_OVERRIDE_USER_FACT") in rejected
    assert ("characters", "REMOVE_REQUIRES_USER_STATEMENT") in rejected


def test_the_brief_editor_uses_the_same_provenance_path(container, project):
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        edited = client.post(
            f"/v1/creative/sessions/{session_id}/brief/edit",
            json={
                "operations": [
                    {"op": "REPLACE", "path": "duration_seconds", "value": 42},
                    {"op": "UPSERT", "path": "characters", "value": {"name": "Mira", "look": "red scarf"}},
                    {"op": "SET", "path": "duration_seconds", "value": "not a number"},
                ]
            },
        )
        assert edited.status_code == 200, edited.text
        body = edited.json()
        assert body["fields"]["duration_seconds"] == 42
        assert body["fields"]["characters"][0]["look"] == "red scarf"
        assert body["provenance"]["duration_seconds"]["source"] == "USER_EDIT"
        assert body["revision"] == started["brief_revision"] + 1
        assert any(item["reason"] == "INVALID_VALUE" for item in body["rejected"])
        # Edits spend no FREE dialogue round: no new turns were written.
        assert len(_state(client, session_id)["turns"]) == 2


# --------------------------------------------------------------- approval gates
def test_approval_is_refused_while_a_critical_question_is_open_and_allowed_once_the_assumption_is_accepted(
    container, project
):
    def handler(latest: str, state: dict) -> dict:
        if not state.get("brief"):
            return {
                "assistant_message": "一个短剧。我先假设故事是：一个雨夜的天台相遇。主角叫什么？",
                "brief_operations": [
                    {
                        "op": "SET",
                        "path": "format",
                        "value": "SHORT_DRAMA",
                        "evidence": "短剧",
                        "confidence": "USER_STATED",
                    },
                ],
                "assumptions": [
                    {
                        "path": "logline",
                        "value": "一个雨夜的天台相遇，改变两个人的命运",
                        "rationale": "the client gave no story yet",
                    },
                    {"path": "duration_seconds", "value": 60, "rationale": "typical short drama"},
                ],
                "unresolved_questions": [
                    {"code": "PROTAGONIST", "question": "主角叫什么？"},
                    {"code": "LOGLINE", "question": "故事讲什么？"},
                ],
            }
        if "雨桐" in latest:
            return {
                "assistant_message": "主角雨桐，在天台，写实风格。",
                "brief_operations": [
                    {
                        "op": "UPSERT",
                        "path": "characters",
                        "value": {"name": "雨桐", "role": "主角"},
                        "evidence": "主角是雨桐",
                        "confidence": "USER_STATED",
                    },
                    {
                        "op": "SET",
                        "path": "setting.location",
                        "value": "天台",
                        "evidence": "在天台",
                        "confidence": "USER_STATED",
                    },
                    {
                        "op": "SET",
                        "path": "visual_style.medium",
                        "value": "cinematic live-action",
                        "evidence": "写实",
                        "confidence": "USER_STATED",
                    },
                ],
                "answered_question_codes": ["PROTAGONIST", "SETTING", "VISUAL_STYLE"],
            }
        return {"assistant_message": "Noted.", "brief_operations": []}

    container.creative_director.model_roles = ScriptedDirector(handler)
    with _client(container) as client:
        started = _start(client, project.id, VAGUE_IDEA)
        session_id = started["session_id"]
        assert started["status"] == "CLARIFYING"
        assert {item["code"] for item in started["blocking"]} >= {"LOGLINE", "PROTAGONIST"}
        assert any(
            item["path"] == "logline" and item["source"] == "MODEL_INFERRED"
            for item in started["assumptions"]
        )

        # CLARIFYING: the backend refuses, whatever the browser shows.
        refused = client.post(
            f"/v1/creative/sessions/{session_id}/brief/approve",
            json={"revision": started["brief_revision"], "accept_assumptions": True},
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["reason_code"] == "BRIEF_NOT_PROPOSED"
        typed = client.post(f"/v1/creative/sessions/{session_id}/messages", json={"content": "批准"})
        assert typed.status_code == 200 and typed.json()["reasoner"] != "APPROVAL"

        # The user answers the protagonist and setting; the logline is still only assumed.
        reply = client.post(
            f"/v1/creative/sessions/{session_id}/messages", json={"content": "主角是雨桐，在天台，写实风格"}
        )
        assert reply.status_code == 200, reply.text
        assert reply.json()["status"] == "CLARIFYING"
        assert [item["code"] for item in reply.json()["blocking"]] == ["LOGLINE"]
        view = _state(client, session_id)
        assert view["brief"]["question_states"]["PROTAGONIST"]["status"] == "ANSWERED"
        assert view["brief"]["question_states"]["LOGLINE"]["status"] == "ASKED"

        # Accepting the director's assumption explicitly resolves the critical gap.
        accepted = client.post(
            f"/v1/creative/sessions/{session_id}/brief/questions",
            json={"code": "LOGLINE", "action": "ACCEPT_ASSUMPTION"},
        )
        assert accepted.status_code == 200, accepted.text
        body = accepted.json()
        assert body["question_states"]["LOGLINE"]["status"] == "ASSUMPTION_ACCEPTED"
        assert body["provenance"]["logline"]["source"] == "ASSUMPTION_ACCEPTED"
        assert body["session_status"] == "BRIEF_PROPOSED"

        # Remaining assumed values (the duration, the aspect default) must be confirmed.
        unconfirmed = client.post(
            f"/v1/creative/sessions/{session_id}/brief/approve", json={"revision": body["revision"]}
        )
        assert unconfirmed.status_code == 409
        assert unconfirmed.json()["detail"]["reason_code"] == "ASSUMPTIONS_UNCONFIRMED"
        approved = _approve_brief(client, session_id, body["revision"], accept_assumptions=True)
        assert approved["brief"]["status"] == "APPROVED"
        assert approved["brief"]["provenance"]["duration_seconds"]["source"] == "ASSUMPTION_ACCEPTED"
        assert approved["brief"]["provenance"]["duration_seconds"]["accepted_by"]
        assert approved["screenplay"]["reasoner"] == "MODEL:DIRECTOR"
        view = _state(client, session_id)
        assert view["session"]["status"] == "SCREENPLAY_PROPOSED"
        assert view["brief"]["status"] == "APPROVED"


def test_a_stale_brief_revision_cannot_be_approved(container, project):
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        client.post(
            f"/v1/creative/sessions/{session_id}/brief/edit",
            json={"operations": [{"op": "REPLACE", "path": "duration_seconds", "value": 40}]},
        )
        stale = client.post(
            f"/v1/creative/sessions/{session_id}/brief/approve", json={"revision": started["brief_revision"]}
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["reason_code"] == "REVISION_SUPERSEDED"


# ------------------------------------------------------- malformed model output
@pytest.mark.parametrize(
    "payload",
    [
        {
            "assistant_message": "ok",
            "brief_operations": [
                {"op": "SET", "path": "characters", "value": "Mira"},
                {"op": "SET", "path": "setting", "value": ["rooftop"]},
                {"op": "SET", "path": "duration_seconds", "value": "abc"},
                {"op": "SET", "path": "tone", "value": {"mood": 1}},
                {"op": "EXPLODE", "path": "format", "value": 1},
            ],
        },
        {"assistant_message": "ok", "brief_operations": "not a list", "assumptions": {"path": "x"}},
        {
            "assistant_message": "",
            "brief_operations": [
                {"op": "UPSERT", "path": "characters", "value": [{"name": ""}, {"look": "no name"}, 42]}
            ],
        },
        {"unexpected": True},
        "just a string",
        [1, 2, 3],
    ],
)
def test_malformed_nested_model_output_degrades_on_record_and_never_500s(container, project, payload):
    class Weird:
        async def execute_chat(self, project_id, role, *, messages, parameters=None):  # type: ignore[no-untyped-def]
            return (
                _execution(payload)
                if isinstance(payload, dict)
                else type(
                    "Execution",
                    (),
                    {"response": {"choices": [{"message": {"content": json.dumps(payload)}}]}},
                )()
            )

    container.creative_director.model_roles = Weird()
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        assert started["status"] in {"CLARIFYING", "BRIEF_PROPOSED"}
        assert started["reasoner"] in {"MODEL:DIRECTOR", "DETERMINISTIC"}
        if started["reasoner"] == "DETERMINISTIC":
            assert "MODEL_OUTPUT_INVALID" in started["reason_codes"]
        view = _state(client, started["session_id"])
        # The user's own facts still landed, whatever the model sent back.
        assert view["brief"]["fields"]["characters"][0]["name"] == "Mira"
        assert view["brief"]["fields"]["duration_seconds"] == 30
        assert [turn["speaker"] for turn in view["turns"]] == ["USER", "DIRECTOR"]


def test_a_bare_field_object_from_the_model_still_works_without_a_reply(container, project):
    class _TerseReasoner:
        async def execute_chat(self, project_id, role, *, messages, parameters=None):  # type: ignore[no-untyped-def]
            return type(
                "Execution",
                (),
                {"response": {"choices": [{"message": {"content": '{"tone": ["warm"]}'}}]}},
            )()

    container.creative_director.model_roles = _TerseReasoner()
    with _client(container) as client:
        started = _start(client, project.id, VAGUE_IDEA)
        view = _state(client, started["session_id"])
    assert started["reasoner"] == "MODEL:DIRECTOR"
    assert "MODEL_NO_REPLY" in started["reason_codes"]
    assert started["message"] == "还有几点会让方案更清晰："
    # A bare field is an inference, not a client fact.
    assert view["brief"]["provenance"]["tone"]["source"] == "MODEL_INFERRED"


# ------------------------------------------------------------- screenplay
def test_the_model_writes_an_original_treatment_beats_dialogue_and_screenplay(container, project):
    director = ScriptedDirector(_rich_turn)
    container.creative_director.model_roles = director
    skill = SkillRegistry(SKILLS_ROOT).resolve("director")
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        assert started["reasoner"] == "MODEL:DIRECTOR" and started["status"] == "BRIEF_PROPOSED"
        session_id = started["session_id"]
        approved = _approve_brief(client, session_id, started["brief_revision"])
        screenplay = approved["screenplay"]
        assert screenplay["reasoner"] == "MODEL:DIRECTOR"
        assert screenplay["deterministic"] is False
        assert screenplay["revision"] == 1 and screenplay["status"] == "PROPOSED"
        assert screenplay["skill_content_hash"] == skill.content_hash
        assert screenplay["model_execution_record_id"].startswith("exec-screenplay")
        content = screenplay["content"]
        assert content["treatment"]["title"] == "The Wrong Phone"
        assert content["treatment"]["hook"]["opening_question"]
        assert content["invariants"] and content["variables"]
        assert [character["name"] for character in content["characters"]] == ["Mira", "Ren"]
        assert content["characters"][1]["relationships"][0]["with"] == "Mira"
        assert [beat["intent"] for beat in content["beats"]] == ["COLD_OPEN", "TURN", "CLIFFHANGER"]
        dialogue = [
            shot["dialogue"]["text"]
            for beat in content["beats"]
            for shot in beat["shots"]
            if shot.get("dialogue")
        ]
        assert dialogue == ["This isn't mine. Who put my name on it?", "You did, Mira. Three days from now."]
        assert (
            "你终于来了" not in screenplay["script_text"]
            and "You finally came" not in screenplay["script_text"]
        )
        script_lines = screenplay["script_text"].splitlines()
        assert script_lines[0] == "EXT. rooftop - NIGHT"
        assert "Mira picks up the phone" in script_lines
        assert "Ren: You did, Mira. Three days from now." in script_lines
        # Every shot carries exactly one primary action plus start/end state.
        for beat in content["beats"]:
            for shot in beat["shots"]:
                assert (shot["action"] is None) != (shot["dialogue"] is None)
                assert shot["start_state"] and shot["end_state"]
        # The screenplay call was made through the Skill, with the approved brief and the conversation.
        screenplay_call = director.calls[-1]
        assert skill.system_prompt in screenplay_call["messages"][0]["content"]
        request = _latest_state_block(screenplay_call["messages"])
        assert request["task"] == "WRITE_SCREENPLAY"
        assert request["approved_brief"]["characters"][0]["name"] == "Mira"
        assert request["client_established_facts"]["setting.location"] == "rooftop"
        assert screenplay_call["parameters"]["response_format"] == {"type": "json_object"}
        view = _state(client, session_id)
        assert view["session"]["status"] == "SCREENPLAY_PROPOSED"


def test_the_user_can_request_a_rewrite_and_edit_the_screenplay_into_new_revisions(container, project):
    def screenplay(request: dict) -> dict:
        payload = json.loads(json.dumps(SCREENPLAY))
        if request["task"] == "REVISE_SCREENPLAY":
            assert request["previous_screenplay"]["treatment"]["title"] == "The Wrong Phone"
            assert "funnier" in request["client_revision_notes"]
            payload["treatment"]["title"] = "The Wrong Phone (funnier)"
        return payload

    container.creative_director.model_roles = ScriptedDirector(_rich_turn, screenplay)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        rewritten = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/propose", json={"notes": "make it funnier"}
        )
        assert rewritten.status_code == 200, rewritten.text
        assert rewritten.json()["revision"] == 2
        assert rewritten.json()["content"]["treatment"]["title"] == "The Wrong Phone (funnier)"
        assert rewritten.json()["parent_revision"] == 1

        content = json.loads(json.dumps(rewritten.json()["content"]))
        content["beats"][1]["shots"][0]["dialogue"]["text"] = "This isn't mine. Whose is it?"
        edited = client.post(f"/v1/creative/sessions/{session_id}/screenplay/edit", json={"content": content})
        assert edited.status_code == 200, edited.text
        assert edited.json()["revision"] == 3 and edited.json()["reasoner"] == "USER_EDIT"
        assert "Mira: This isn't mine. Whose is it?" in edited.json()["script_text"]

        broken = dict(content)
        broken["beats"] = [
            dict(content["beats"][0], shots=[{"sequence": 1, "action": {"actor": "Nobody", "verb": "fly"}}])
        ]
        rejected = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/edit", json={"content": broken}
        )
        assert rejected.status_code == 400
        view = _state(client, session_id)
        assert [item["status"] for item in view["screenplays"]] == ["SUPERSEDED", "SUPERSEDED", "PROPOSED"]


def test_the_deterministic_scaffold_appears_only_when_the_model_is_unavailable_and_is_labelled(
    container, project
):
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        approved = _approve_brief(client, session_id, started["brief_revision"])
        screenplay = approved["screenplay"]
        assert screenplay["reasoner"] == "DETERMINISTIC"
        assert screenplay["deterministic"] is True
        assert "DETERMINISTIC_FALLBACK" in screenplay["reason_codes"]
        assert any("DETERMINISTIC SCAFFOLD" in item for item in screenplay["content"]["unresolved"])
        refused = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/approve",
            json={"revision": screenplay["revision"]},
        )
        assert refused.status_code == 409
        assert refused.json()["detail"]["reason_code"] == "DETERMINISTIC_SCREENPLAY_UNCONFIRMED"
        accepted = client.post(
            f"/v1/creative/sessions/{session_id}/screenplay/approve",
            json={"revision": screenplay["revision"], "accept_deterministic": True},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["session_status"] == "VISUALS_IN_PROGRESS"

    # Once the model answers, redrafting replaces the scaffold with the director's writing.
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        approved = _approve_brief(client, started["session_id"], started["brief_revision"])
        assert approved["screenplay"]["reasoner"] == "MODEL:DIRECTOR"


def test_a_model_outage_during_the_screenplay_is_recorded_as_a_retryable_fallback(container, project):
    from production_domain.models import RetryCategory
    from provider_sdk import ProviderError

    calls = {"n": 0}

    class Flaky(ScriptedDirector):
        async def execute_chat(self, project_id, role, *, messages, parameters=None):  # type: ignore[no-untyped-def]
            request = _latest_state_block(messages)
            if request.get("task") == "WRITE_SCREENPLAY" and calls["n"] == 0:
                calls["n"] += 1
                raise ProviderError("upstream 503", RetryCategory.PROVIDER_BUSY, code="UPSTREAM")
            return await super().execute_chat(project_id, role, messages=messages, parameters=parameters)

    container.creative_director.model_roles = Flaky(_rich_turn)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        approved = _approve_brief(client, started["session_id"], started["brief_revision"])
        assert approved["screenplay"]["reasoner"] == "DETERMINISTIC"
        assert "MODEL_UNAVAILABLE" in approved["screenplay"]["reason_codes"]
        redrafted = client.post(f"/v1/creative/sessions/{started['session_id']}/screenplay/propose", json={})
        assert redrafted.status_code == 200, redrafted.text
        assert redrafted.json()["reasoner"] == "MODEL:DIRECTOR" and redrafted.json()["revision"] == 2


# --------------------------------------------------- key visuals and the bible
def test_screenplay_approval_derives_versioned_anchors_and_executes_key_visuals(openrouter_container):
    container = openrouter_container
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with container.database.session() as session:
        project = Project(title="Key Visuals")
        session.add(project)
        session.flush()
        project_id = project.id
    with _client(container) as client:
        started = _start(client, project_id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        approved = _approve_screenplay(client, session_id)
        kinds = {action["kind"] for action in approved["actions"]}
        assert kinds == {"GENERATE_KEY_VISUAL"}
        anchors = {anchor["anchor_key"]: anchor for anchor in approved["anchors"]}
        # Characters and the style plate are required; the scene and the prop are optional.
        assert {"character:mira", "character:ren", "style:master", "scene:rooftop", "prop:phone"} <= set(
            anchors
        )
        assert anchors["character:mira"]["required"] and anchors["style:master"]["required"]
        assert not anchors["scene:rooftop"]["required"] and not anchors["prop:phone"]["required"]
        assert all(anchor["version"] == 1 for anchor in anchors.values())
        executed = [entry for entry in approved["executions"] if entry["status"] == "EXECUTED"]
        assert len(executed) == len(approved["executions"]) == len(anchors)

        with container.database.session() as session:
            jobs = list(session.scalars(select(GenerationJob)))
            assert len(jobs) == len(executed)
            assert {job.generation_type for job in jobs} == {"image"}
            rows = list(session.scalars(select(CreativeVisualAnchor)))
            assert {anchor.status for anchor in rows} == {"GENERATING"}
            character_anchor = next(a for a in rows if a.anchor_key == "character:mira")
            assert character_anchor.character_id is not None
            assert session.get(Character, character_anchor.character_id).name == "Mira"
            assert character_anchor.screenplay_id and character_anchor.brief_id

        # Replaying the executor creates nothing new.
        replay = client.post(f"/v1/creative/sessions/{session_id}/visuals/execute")
        assert replay.status_code == 200 and replay.json()["executions"] == []
        with container.database.session() as session:
            assert len(list(session.scalars(select(GenerationJob)))) == len(executed)
            action = session.scalar(select(CreativeAction).order_by(CreativeAction.sequence))
            action.status = "FAILED"
            anchor = session.get(CreativeVisualAnchor, action.payload_json["anchor_id"])
            anchor.status = "FAILED"
            anchor.failure_code = "TRANSIENT_TEST_FAILURE"
        retried = client.post(f"/v1/creative/sessions/{session_id}/visuals/execute")
        assert retried.status_code == 200
        assert [entry["status"] for entry in retried.json()["executions"]] == ["EXECUTED"]
        with container.database.session() as session:
            # The idempotency key reuses the existing job instead of paying twice.
            assert len(list(session.scalars(select(GenerationJob)))) == len(executed)
            anchor = session.get(CreativeVisualAnchor, action.payload_json["anchor_id"])
            assert anchor.status == "GENERATING" and anchor.failure_code is None


def test_an_anchor_whose_content_changed_gets_a_new_version_instead_of_reusing_the_old_image(
    container, project
):
    from creative_director_core import derive_anchor_specs, validate_screenplay

    fields = {
        "format": "SHORT_DRAMA",
        "logline": "x",
        "visual_style": {"medium": "anime"},
        "characters": [{"name": "Mira"}],
    }
    first = derive_anchor_specs(fields, validate_screenplay(SCREENPLAY))
    changed = json.loads(json.dumps(SCREENPLAY))
    changed["characters"][0]["look"] = "white coat, dry hair, umbrella"
    second = derive_anchor_specs(fields, validate_screenplay(changed))
    by_key_first = {spec.anchor_key: spec for spec in first}
    by_key_second = {spec.anchor_key: spec for spec in second}
    assert by_key_first["character:mira"].prompt_hash != by_key_second["character:mira"].prompt_hash
    assert by_key_first["character:ren"].prompt_hash == by_key_second["character:ren"].prompt_hash

    # Through the service: a superseding derivation versions the changed anchor only.
    service = container.creative_director
    with container.database.session() as session:
        row = CreativeSession(project_id=project.id, title="versions")
        session.add(row)
        session.flush()
        from production_domain.models import CreativeBriefRevision

        brief = CreativeBriefRevision(
            session_id=row.id, revision=1, status="APPROVED", fields_json=fields, content_hash="0" * 64
        )
        session.add(brief)
        session.flush()
        content = SCREENPLAY
        screenplay_row = CreativeScreenplayRevision(
            session_id=row.id,
            revision=1,
            status="APPROVED",
            brief_id=brief.id,
            reasoner="MODEL:DIRECTOR",
            content_json=content,
            content_hash="1" * 64,
        )
        session.add(screenplay_row)
        session.flush()
        first_rows = service._derive_anchors(
            session, row, brief, screenplay_row, validate_screenplay(SCREENPLAY)
        )
        mira_v1 = next(a for a in first_rows if a.anchor_key == "character:mira")
        mira_v1.status = "READY"
        mira_v1.media_asset_id = None
        second_row = CreativeScreenplayRevision(
            session_id=row.id,
            revision=2,
            status="APPROVED",
            brief_id=brief.id,
            reasoner="USER_EDIT",
            content_json=changed,
            content_hash="2" * 64,
        )
        session.add(second_row)
        session.flush()
        second_rows = service._derive_anchors(session, row, brief, second_row, validate_screenplay(changed))
        mira_v2 = next(a for a in second_rows if a.anchor_key == "character:mira")
        assert mira_v2.version == 2 and mira_v2.status == "PENDING" and mira_v2.id != mira_v1.id
        assert mira_v1.status == "SUPERSEDED"
        ren = next(a for a in second_rows if a.anchor_key == "character:ren")
        assert ren.version == 1


@pytest.mark.asyncio
async def test_visual_sync_binds_completed_jobs_and_the_bible_waits_for_required_anchors(
    openrouter_container,
):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with container.database.session() as session:
        project = Project(title="Creative Visuals")
        session.add(project)
        session.flush()
        project_id = project.id

    with _client(container) as client:
        started = _start(client, project_id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        approved = _approve_screenplay(client, session_id)
        job_ids = {entry["anchor_id"]: entry["job_id"] for entry in approved["executions"]}
        assert job_ids

        # Nothing is ready yet: the bible is refused with the anchors that block it.
        refused = client.post(f"/v1/creative/sessions/{session_id}/bible/propose")
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["reason_code"] == "REQUIRED_ANCHORS_NOT_READY"
        blocked = {item["anchor_key"] for item in refused.json()["detail"]["anchors"]}
        assert {"character:mira", "character:ren", "style:master"} <= blocked

        # Complete only the required anchors; leave the optional scene pending.
        anchors = {anchor["anchor_key"]: anchor for anchor in _state(client, session_id)["anchors"]}
        for key in ("character:mira", "character:ren", "style:master", "prop:phone"):
            completed = await container.gateway.process(job_ids[anchors[key]["id"]])
            assert completed.status == "COMPLETED"
        synced = client.post(f"/v1/creative/sessions/{session_id}/visuals/sync").json()
        assert synced["ready"] == 4 and synced["all_terminal"] is False
        assert synced["required_not_ready"] == []
        assert [item["anchor_key"] for item in synced["optional_not_terminal"]] == ["scene:rooftop"]
        assert synced["can_propose_bible"] is False
        still = client.post(f"/v1/creative/sessions/{session_id}/bible/propose")
        assert still.status_code == 409
        assert still.json()["detail"]["reason_code"] == "OPTIONAL_ANCHORS_NOT_TERMINAL"

        # An optional anchor that failed may be skipped by the user, on record; a required one may not.
        with container.database.session() as session:
            scene = session.get(CreativeVisualAnchor, anchors["scene:rooftop"]["id"])
            scene.status = "FAILED"
            scene.failure_code = "PROVIDER_REFUSED"
        cannot = client.post(
            f"/v1/creative/sessions/{session_id}/visuals/anchors/{anchors['character:mira']['id']}/skip",
            json={"reason": "meh"},
        )
        assert cannot.status_code == 409 and cannot.json()["detail"]["reason_code"] == "REQUIRED_ANCHOR"
        skipped = client.post(
            f"/v1/creative/sessions/{session_id}/visuals/anchors/{anchors['scene:rooftop']['id']}/skip",
            json={"reason": "we will shoot the plate ourselves"},
        )
        assert skipped.status_code == 200, skipped.text
        assert skipped.json()["status"] == "SKIPPED"
        assert "we will shoot the plate ourselves" in skipped.json()["skip_reason"]
        synced = client.post(f"/v1/creative/sessions/{session_id}/visuals/sync").json()
        assert synced["can_propose_bible"] is True and synced["all_terminal"] is True
        assert all(anchor["media_asset_id"] for anchor in synced["anchors"] if anchor["status"] == "READY")

        proposed = client.post(f"/v1/creative/sessions/{session_id}/bible/propose")
        assert proposed.status_code == 200, proposed.text
        bible = proposed.json()
        assert bible["content"]["screenplay_revision"] == 1
        assert {a["anchor_key"]: a["status"] for a in bible["content"]["anchors"]}[
            "scene:rooftop"
        ] == "SKIPPED"
        assert bible["lineage"]["lock_status"] == "NOT_LOCKED"
        # Under the development bypass there is no user to own a style lock: refused, not faked.
        refused_lock = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve", json={"version": bible["version"]}
        )
        assert refused_lock.status_code == 409
        assert refused_lock.json()["detail"]["reason_code"] == "STYLE_LOCK_REQUIRES_USER"


@pytest.mark.asyncio
async def test_bible_lock_creates_identity_versions_and_a_style_lock_and_compiled_shots_trace_back(
    openrouter_container,
):
    container = openrouter_container
    _wire_openrouter_images(container)
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        headers, project_id, user_id = _registered_pro(client, container, "director-e2e@example.com")
        started = client.post(
            "/v1/creative/sessions", headers=headers, json={"project_id": project_id, "idea": RICH_IDEA}
        )
        assert started.status_code == 201, started.text
        started = started.json()
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"], headers)
        approved = _approve_screenplay(client, session_id, headers)
        assert all(entry["status"] == "EXECUTED" for entry in approved["executions"]), approved["executions"]
        synced = await _complete_visuals(container, client, session_id, headers)
        assert synced["can_propose_bible"] is True
        bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose", headers=headers).json()

        locked = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": bible["version"]},
        )
        assert locked.status_code == 200, locked.text
        lineage = locked.json()["lineage"]
        assert locked.json()["status"] == "LOCKED"
        assert lineage["lock_status"] == "LOCKED"
        assert lineage["style_inherited"] is False
        assert set(lineage["identities"]) == {"character:mira", "character:ren"}

        with container.database.session() as session:
            style_lock = session.scalar(
                select(ProjectStyleLock).where(ProjectStyleLock.project_id == project_id)
            )
            assert style_lock is not None and style_lock.id == lineage["style_lock_id"]
            assert style_lock.locked_by_user_id == user_id
            assert session.get(Project, project_id).canonical_style_version_id == style_lock.style_version_id
            identities = list(session.scalars(select(CharacterIdentityVersion)))
            assert len(identities) == 2
            assert {identity.status for identity in identities} == {"LOCKED"}
            for entry in lineage["identities"].values():
                character = session.get(Character, entry["character_id"])
                assert character.current_identity_version_id == entry["identity_version_id"]
                assert character.status == "CONFIRMED"
                identity = session.get(CharacterIdentityVersion, entry["identity_version_id"])
                assert identity.master_asset_id == entry["media_asset_id"]
            lock_actions = [
                action
                for action in session.scalars(select(CreativeAction))
                if action.kind in {"LOCK_CHARACTER_IDENTITY", "LOCK_PROJECT_STYLE"}
            ]
            assert len(lock_actions) == 3 and {action.status for action in lock_actions} == {"EXECUTED"}
        # Locking again is idempotent: no second identity version, no second lock.
        again = client.post(
            f"/v1/creative/sessions/{session_id}/bible/approve",
            headers=headers,
            json={"version": bible["version"]},
        )
        assert again.status_code == 200
        with container.database.session() as session:
            assert len(list(session.scalars(select(CharacterIdentityVersion)))) == 2

        proposed = client.post(f"/v1/creative/sessions/{session_id}/beats/propose", headers=headers)
        assert proposed.status_code == 200, proposed.text
        beats = proposed.json()["beats"]
        assert [beat["intent"] for beat in beats] == ["COLD_OPEN", "TURN", "CLIFFHANGER"]
        assert beats[1]["shots"][1]["dialogue"] == "You did, Mira. Three days from now."
        assert (
            "character:mira" in beats[0]["shots"][0]["anchors"]
            and "style:master" in beats[0]["shots"][0]["anchors"]
        )

        compiled = client.post(
            f"/v1/creative/sessions/{session_id}/beats/approve", headers=headers, json={"plan_revision": 1}
        )
        assert compiled.status_code == 200, compiled.text
        result = compiled.json()
        assert result["status"] == "COMPILED" and result["shot_ids"]
        assert result["screenplay_revision"] == 1

        with container.database.session() as session:
            episode = session.get(Episode, result["episode_id"])
            assert episode is not None and episode.status == "COMPILED"
            screenplay = session.scalar(
                select(CreativeScreenplayRevision).where(CreativeScreenplayRevision.status == "APPROVED")
            )
            assert episode.script_source == screenplay.script_text
            shots = [session.get(Shot, shot_id) for shot_id in result["shot_ids"]]
            assert shots[0].shot_type == "WIDE"
            assert any(shot.shot_type == "DIALOGUE" for shot in shots)
            assert [round(shot.duration) for shot in shots] == [5, 4, 6, 6, 5]
            lineage_rows = list(session.scalars(select(CreativeShotLineage)))
            assert len(lineage_rows) == len(shots)
            obligations = {o.obligation_key: o for o in session.scalars(select(NarrativeObligation))}
            assert any(key.endswith(":cliffhanger") for key in obligations)
            assert any(key.endswith(":who_is_ren") for key in obligations)
            assert all(o.status == "OPEN" for o in obligations.values())
            # The compiled characters are the very rows the identities were locked on.
            character_names = {
                c.name for c in session.scalars(select(Character).where(Character.project_id == project_id))
            }
            assert character_names == {"Mira", "Ren"}

        for shot_id in result["shot_ids"]:
            traced = client.get(f"/v1/creative/shots/{shot_id}/lineage", headers=headers)
            assert traced.status_code == 200, traced.text
            trace = traced.json()
            assert trace["brief"]["revision"] and trace["screenplay"]["revision"] == 1
            assert trace["bible"]["version"] == bible["version"]
            assert trace["style_lock_id"] == lineage["style_lock_id"]
            assert trace["anchors"]
        mira_shot = client.get(f"/v1/creative/shots/{result['shot_ids'][0]}/lineage", headers=headers).json()
        assert (
            lineage["identities"]["character:mira"]["identity_version_id"]
            in mira_shot["identity_version_ids"]
        )

        # Approving again replays the same compiled episode - no duplicate.
        replay = client.post(
            f"/v1/creative/sessions/{session_id}/beats/approve", headers=headers, json={"plan_revision": 1}
        )
        assert replay.status_code == 200 and replay.json()["episode_id"] == result["episode_id"]
        with container.database.session() as session:
            assert len(list(session.scalars(select(Episode).where(Episode.project_id == project_id)))) == 1
            assert len(list(session.scalars(select(CreativeShotLineage)))) == len(result["shot_ids"])
        closed = client.post(
            f"/v1/creative/sessions/{session_id}/messages",
            headers=headers,
            json={"content": "change everything"},
        )
        assert closed.status_code == 409


def _ready_anchors_without_generation(container, session_id: str, project_id: str) -> None:
    """Bind a registered PNG to every current anchor, bypassing the paid path.

    For tests about the lock and compile semantics rather than the gateway;
    the media carries provider provenance the way a gateway output would.
    """

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 90, 30)).save(buffer, format="PNG")
    payload = buffer.getvalue()
    with container.database.session() as session:
        anchors = list(
            session.scalars(
                select(CreativeVisualAnchor).where(
                    CreativeVisualAnchor.session_id == session_id,
                    CreativeVisualAnchor.status != "SUPERSEDED",
                )
            )
        )
        ids = [anchor.id for anchor in anchors]
    for index, anchor_id in enumerate(ids):
        media, _ = container.media.register(
            project_id,
            "IMAGE",
            io.BytesIO(payload + bytes([index])),
            filename=f"anchor-{index}.png",
            mime_type="image/png",
            provider="openrouter",
            provider_media_id=f"mock-{session_id}-{index}",
        )
        with container.database.session() as session:
            anchor = session.get(CreativeVisualAnchor, anchor_id)
            anchor.status = "READY"
            anchor.media_asset_id = media.id


def _user(container, email: str) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        user = User(email=email, display_name="Locker")
        session.add(user)
        session.flush()
        return user.id


def test_only_the_edited_screenplay_revision_is_compiled_and_a_failed_lock_blocks_compilation(
    container, project
):
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    service = container.creative_director
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        _approve_screenplay(client, session_id)
    _ready_anchors_without_generation(container, session_id, project.id)
    with _client(container) as client:
        bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose").json()
    user_id = _user(container, "locker@example.com")

    # A style lock that fails leaves the bible DRAFT with the failure on record, and blocks compilation.
    real_lock = service.styles.lock

    def broken_lock(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("style embedding service is down")

    service.styles.lock = broken_lock  # type: ignore[method-assign]
    with pytest.raises(Exception) as failure:
        service.approve_bible(session_id, version=bible["version"], actor="locker", actor_user_id=user_id)
    assert getattr(failure.value, "reason_code", None) == "LOCK_FAILED"
    with container.database.session() as session:
        row = session.scalar(select(VisualBibleVersion).where(VisualBibleVersion.session_id == session_id))
        assert row.status == "DRAFT" and row.lineage_json["lock_status"] == "FAILED"
        assert "style embedding service is down" in row.lineage_json["error"]
        assert session.get(CreativeSession, session_id).status == "BIBLE_PROPOSED"
        assert list(session.scalars(select(ProjectStyleLock))) == []
    with pytest.raises(Exception) as blocked:
        service.propose_beats(session_id)
    assert getattr(blocked.value, "reason_code", None) == "INVALID_TRANSITION"

    # The retry, with the service back, locks exactly once.
    service.styles.lock = real_lock  # type: ignore[method-assign]
    locked = service.approve_bible(
        session_id, version=bible["version"], actor="locker", actor_user_id=user_id
    )
    assert locked["status"] == "LOCKED" and locked["lineage"]["lock_status"] == "LOCKED"
    with container.database.session() as session:
        assert len(list(session.scalars(select(ProjectStyleLock)))) == 1
        assert len(list(session.scalars(select(CharacterIdentityVersion)))) == 2

    with _client(container) as client:
        beats = client.post(f"/v1/creative/sessions/{session_id}/beats/propose").json()["beats"]
        # The user rewrites one line and shortens one shot at approval time.
        beats[1]["shots"][0]["dialogue"] = "This isn't mine. Why is it warm?"
        beats[0]["shots"][0]["duration"] = 3
        beats[0]["shots"][0]["description"] = "she hesitates at the door"
        compiled = client.post(
            f"/v1/creative/sessions/{session_id}/beats/approve",
            json={"plan_revision": 1, "beats": beats, "episode_title": "EP01 The Wrong Phone"},
        )
        assert compiled.status_code == 200, compiled.text
        result = compiled.json()
        assert result["screenplay_revision"] == 2
    with container.database.session() as session:
        revisions = {r.revision: r for r in session.scalars(select(CreativeScreenplayRevision))}
        assert revisions[1].status == "SUPERSEDED"
        assert revisions[2].status == "APPROVED" and revisions[2].reasoner == "USER_EDIT"
        assert "BEATS_EDITED_AT_APPROVAL" in revisions[2].reason_codes
        assert (
            revisions[2].content_json["beats"][1]["shots"][0]["dialogue"]["text"]
            == "This isn't mine. Why is it warm?"
        )
        episode = session.get(Episode, result["episode_id"])
        assert episode.title == "EP01 The Wrong Phone"
        assert episode.script_source == revisions[2].script_text
        assert "Mira: This isn't mine. Why is it warm?" in episode.script_source
        assert "Who put my name on it" not in episode.script_source
        shots = [session.get(Shot, shot_id) for shot_id in result["shot_ids"]]
        assert round(shots[0].duration) == 3
        lineage = list(session.scalars(select(CreativeShotLineage)))
        assert {row.screenplay_id for row in lineage} == {revisions[2].id}
        create_action = session.scalar(select(CreativeAction).where(CreativeAction.kind == "CREATE_EPISODE"))
        assert create_action.payload_json["screenplay_id"] == revisions[2].id


def test_a_second_session_in_a_locked_project_inherits_the_style_lock_on_record(container, project):
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    service = container.creative_director
    user_id = _user(container, "inherit@example.com")
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
        locks = list(session.scalars(select(ProjectStyleLock)))
        assert len(locks) == 1
        bibles = {b.session_id: b for b in session.scalars(select(VisualBibleVersion))}
    assert bibles[session_ids[0]].lineage_json["style_inherited"] is False
    assert bibles[session_ids[1]].lineage_json["style_inherited"] is True
    assert bibles[session_ids[1]].lineage_json["style_lock_id"] == locks[0].id
    assert "STYLE_LOCK_INHERITED" in bibles[session_ids[1]].lineage_json["reason_codes"]


def test_bible_lock_is_versioned_and_immutable(container, project):
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    service = container.creative_director
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        _approve_screenplay(client, session_id)
    _ready_anchors_without_generation(container, session_id, project.id)
    user_id = _user(container, "versions@example.com")
    with _client(container) as client:
        proposed = client.post(f"/v1/creative/sessions/{session_id}/bible/propose")
        assert proposed.status_code == 200, proposed.text
        version_one = proposed.json()["version"]
        # A second proposal before locking supersedes the first draft.
        second = client.post(f"/v1/creative/sessions/{session_id}/bible/propose").json()
        assert second["version"] == version_one + 1
    with pytest.raises(Exception) as stale:
        service.approve_bible(session_id, version=version_one, actor="v", actor_user_id=user_id)
    assert getattr(stale.value, "reason_code", None) == "REVISION_SUPERSEDED"
    locked = service.approve_bible(session_id, version=second["version"], actor="v", actor_user_id=user_id)
    assert locked["status"] == "LOCKED" and locked["locked_at"] is not None
    # Locking is idempotent; proposing after the lock is refused.
    again = service.approve_bible(session_id, version=second["version"], actor="v", actor_user_id=user_id)
    assert again["status"] == "LOCKED"
    with _client(container) as client:
        refused = client.post(f"/v1/creative/sessions/{session_id}/bible/propose")
        assert refused.status_code == 409
    with container.database.session() as session:
        statuses = {bible.version: bible.status for bible in session.scalars(select(VisualBibleVersion))}
        assert statuses == {version_one: "SUPERSEDED", second["version"]: "LOCKED"}


# ------------------------------------------------------- transactions and replay
@pytest.mark.asyncio
async def test_an_unexpected_crash_leaves_no_orphan_turn_and_spends_no_free_round(container, monkeypatch):
    with container.database.session() as session:
        user = User(email="free-crash@example.com", display_name="Free")
        session.add(user)
        session.flush()
        workspace = Workspace(owner_user_id=user.id, name="Free", status="ACTIVE", plan_tier="FREE")
        session.add(workspace)
        session.flush()
        project = Project(workspace_id=workspace.id, title="Budget", status="ACTIVE")
        session.add(project)
        session.flush()
        project_id, workspace_id = project.id, workspace.id

    creative = container.creative_director
    creative.free_plan_turn_limit = 2
    creative.model_roles = ScriptedDirector(_rich_turn)
    reply = await creative.start_session(project_id, idea=VAGUE_IDEA, workspace_id=workspace_id)
    session_id = reply.session_id

    # A crash *after* the model answered, inside the write phase: nothing lands.
    original = creative.briefs.analyze

    def explode(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash while writing the turn")

    monkeypatch.setattr(creative.briefs, "analyze", explode)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await creative.post_message(session_id, "主角是雨桐，在天台")
    monkeypatch.setattr(creative.briefs, "analyze", original)
    with container.database.session() as session:
        turns = list(session.scalars(select(CreativeTurn).where(CreativeTurn.session_id == session_id)))
        assert [turn.speaker for turn in turns] == ["USER", "DIRECTOR"]  # only the first round
        assert session.get(CreativeSession, session_id).current_brief_revision == 1

    # The round was not counted: the second FREE round is still available, and lands atomically.
    replied = await creative.post_message(session_id, "主角是雨桐，在天台", client_turn_id="round-2")
    assert replied.turn_sequence == 4
    with pytest.raises(Exception, match="Free plan"):
        await creative.post_message(session_id, "再长一点")

    # A model exception degrades on record - one consistent round, flagged retryable.
    creative.free_plan_turn_limit = 10
    creative.model_roles = ScriptedDirector(raise_with=RuntimeError("model host unreachable"))
    degraded = await creative.post_message(session_id, "再悬疑一点")
    assert degraded.reasoner == "DETERMINISTIC"
    assert "MODEL_CALL_ERROR" in degraded.reason_codes and degraded.retryable is True
    with container.database.session() as session:
        turns = list(
            session.scalars(
                select(CreativeTurn)
                .where(CreativeTurn.session_id == session_id)
                .order_by(CreativeTurn.sequence)
            )
        )
        assert [turn.speaker for turn in turns] == [
            "USER",
            "DIRECTOR",
            "USER",
            "DIRECTOR",
            "USER",
            "DIRECTOR",
        ]
        assert turns[-1].reasoner == "DETERMINISTIC"
        assert turns[-1].brief_revision == 3


def test_a_retried_message_with_the_same_client_turn_id_replays_the_recorded_reply(container, project):
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA, client_turn_id="idea-1")
        session_id = started["session_id"]
        first = client.post(
            f"/v1/creative/sessions/{session_id}/messages",
            json={"content": "再悬疑一点", "client_turn_id": "turn-2"},
        ).json()
        second = client.post(
            f"/v1/creative/sessions/{session_id}/messages",
            json={"content": "再悬疑一点", "client_turn_id": "turn-2"},
        ).json()
        assert second["replayed"] is True and first["replayed"] is False
        assert second["message"] == first["message"]
        assert second["turn_sequence"] == first["turn_sequence"]
        assert "IDEMPOTENT_REPLAY" in second["reason_codes"]
        view = _state(client, session_id)
        assert len(view["turns"]) == 4
        assert len(container.creative_director.model_roles.calls) == 2


def test_beats_compile_retry_reuses_the_persisted_episode(container, project, monkeypatch):
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    service = container.creative_director
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        _approve_screenplay(client, session_id)
    _ready_anchors_without_generation(container, session_id, project.id)
    user_id = _user(container, "retry@example.com")
    with _client(container) as client:
        bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose").json()
    service.approve_bible(session_id, version=bible["version"], actor="r", actor_user_id=user_id)
    service.propose_beats(session_id)

    compiler = service.orchestrator
    original_compile = compiler.compile_episode

    def fail_after_episode_persisted(_episode_id: str):
        raise RuntimeError("transient compiler failure")

    monkeypatch.setattr(compiler, "compile_episode", fail_after_episode_persisted)
    with pytest.raises(RuntimeError, match="transient compiler failure"):
        service.approve_beats(session_id, plan_revision=1, actor="test")
    with container.database.session() as session:
        row = session.get(CreativeSession, session_id)
        persisted_episode_id = row.compiled_episode_id
        assert persisted_episode_id is not None
        assert len(list(session.scalars(select(Episode)))) == 1

    monkeypatch.setattr(compiler, "compile_episode", original_compile)
    result = service.approve_beats(session_id, plan_revision=1, actor="test")
    assert result["episode_id"] == persisted_episode_id
    with container.database.session() as session:
        assert len(list(session.scalars(select(Episode)))) == 1
        actions = [
            a
            for a in session.scalars(select(CreativeAction))
            if a.kind in {"CREATE_EPISODE", "COMPILE_EPISODE"}
        ]
        assert len(actions) == 2
        scenes = list(session.scalars(select(Scene).where(Scene.episode_id == persisted_episode_id)))
        assert scenes


# ---------------------------------------------------------- session lifecycle
def test_typing_an_approval_approves_the_proposed_brief_and_drafts_the_screenplay(container, project):
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        assert started["status"] == "BRIEF_PROPOSED"
        session_id = started["session_id"]
        approved = client.post(f"/v1/creative/sessions/{session_id}/messages", json={"content": "批准。"})
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["reasoner"] == "APPROVAL"
        assert body["approved_revision"] == started["brief_revision"] + 1
        assert body["screenplay"]["reasoner"] == "MODEL:DIRECTOR"
        view = _state(client, session_id)
    assert view["session"]["status"] == "SCREENPLAY_PROPOSED"
    assert view["brief"]["status"] == "APPROVED"


def test_a_conditional_approval_is_a_turn_not_an_approval(container, project):
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        reply = client.post(
            f"/v1/creative/sessions/{session_id}/messages",
            json={"content": "批准，但把主角改成男生"},
        )
        assert reply.status_code == 200
        assert reply.json()["reasoner"] != "APPROVAL"
        view = _state(client, session_id)
    assert view["session"]["status"] in {"BRIEF_PROPOSED", "CLARIFYING"}


def test_a_deleted_session_leaves_the_list_and_stops_taking_turns(container, project):
    with _client(container) as client:
        first = _start(client, project.id, VAGUE_IDEA)["session_id"]
        second = _start(client, project.id, RICH_IDEA)["session_id"]
        listed = client.get("/v1/creative/sessions", params={"project_id": project.id}).json()
        assert {item["id"] for item in listed} == {first, second}
        deleted = client.delete(f"/v1/creative/sessions/{first}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json() == {"id": first, "status": "ABANDONED"}
        listed = client.get("/v1/creative/sessions", params={"project_id": project.id}).json()
        assert [item["id"] for item in listed] == [second]
        assert _state(client, first)["session"]["status"] == "ABANDONED"
        refused = client.post(f"/v1/creative/sessions/{first}/messages", json={"content": "还在吗"})
        assert refused.status_code == 409
        assert client.delete(f"/v1/creative/sessions/{first}").status_code == 200
        assert client.delete("/v1/creative/sessions/nope").status_code == 404


def test_session_rows_are_the_audit_of_everything_that_happened(container, project):
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        _approve_screenplay(client, session_id)
        state = _state(client, session_id)
        assert state["session"]["status"] == "VISUALS_IN_PROGRESS"
        assert [turn["speaker"] for turn in state["turns"]] == ["USER", "DIRECTOR"]
        director_turn = state["turns"][1]
        assert (
            director_turn["context"]["turns_total"] == 0 and director_turn["context"]["compressed"] is False
        )
        assert director_turn["operations"] and director_turn["result"]["creative_notes"] == [
            "one light source, teal night"
        ]
        assert all(
            action["kind"] == "GENERATE_KEY_VISUAL" and action["idempotency_key"]
            for action in state["actions"]
        )
        assert state["screenplay"]["status"] == "APPROVED"
    with container.database.session() as session:
        row = session.get(CreativeSession, session_id)
        assert row is not None and row.workspace_id == project.workspace_id
        actions = list(session.scalars(select(CreativeAction)))
        assert all(action.status in {"EXECUTED", "FAILED"} for action in actions)


def test_a_long_conversation_is_compressed_on_record_but_keeps_facts_and_prohibitions(container, project):
    director = ScriptedDirector(
        turn_handler=lambda latest, state: {"assistant_message": "好的。" * 400, "brief_operations": []}
    )
    container.creative_director.model_roles = director
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA + " 不要出现任何文字水印。")
        session_id = started["session_id"]
        for index in range(14):
            reply = client.post(
                f"/v1/creative/sessions/{session_id}/messages",
                json={"content": f"第{index}轮补充：" + "画面再暗一点。" * 60},
            )
            assert reply.status_code == 200, reply.text
        view = _state(client, session_id)
    last_call = director.calls[-1]["messages"]
    state_block = _latest_state_block(last_call)
    last_turn = [turn for turn in view["turns"] if turn["speaker"] == "DIRECTOR"][-1]
    assert last_turn["context"]["compressed"] is True
    assert last_turn["context"]["turns_condensed"] > 0
    assert "CONTEXT_COMPRESSED" in last_turn["reason_codes"]
    assert any("condensed" in message["content"] for message in last_call if message["role"] == "user")
    assert state_block["preserved"]["client_established_facts"]["duration_seconds"] == 30
    assert any("不要出现任何文字水印" in item for item in state_block["preserved"]["prohibitions"])


# ----------------------------------------------------- changing key visuals
def test_a_key_visual_can_be_regenerated_with_direction_or_replaced_by_the_users_image_before_the_lock(
    container, project
):
    container.creative_director.model_roles = ScriptedDirector(_rich_turn)
    service = container.creative_director
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
        session_id = started["session_id"]
        _approve_brief(client, session_id, started["brief_revision"])
        _approve_screenplay(client, session_id)
    _ready_anchors_without_generation(container, session_id, project.id)
    with _client(container) as client:
        anchors = {anchor["anchor_key"]: anchor for anchor in _state(client, session_id)["anchors"]}
        # Drafting a bible, then changing an image, sends the session back to key visuals.
        bible = client.post(f"/v1/creative/sessions/{session_id}/bible/propose").json()
        assert _state(client, session_id)["session"]["status"] == "BIBLE_PROPOSED"

        regenerated = client.post(
            f"/v1/creative/sessions/{session_id}/visuals/anchors/{anchors['character:mira']['id']}/regenerate",
            json={"direction": "older, grey streak in her hair, no coat"},
        )
        assert regenerated.status_code == 200, regenerated.text
        body = regenerated.json()
        assert body["anchor"]["version"] == 2 and body["anchor"]["status"] in {
            "PENDING",
            "GENERATING",
            "FAILED",
        }
        assert body["anchor"]["prompt"]["user_direction"] == "older, grey streak in her hair, no coat"
        assert body["session_status"] == "VISUALS_IN_PROGRESS"
        assert [action["kind"] for action in body["actions"]] == ["GENERATE_KEY_VISUAL"]
        assert body["actions"][0]["idempotency_key"].endswith(":visual:character:mira:v2")
        view = _state(client, session_id)
        superseded = {a["anchor_key"]: a for a in view["session"]["superseded_anchors"]}
        assert superseded["character:mira"]["version"] == 1 and superseded["character:mira"]["media_asset_id"]
        current = {a["anchor_key"]: a for a in view["anchors"]}
        assert (
            current["character:mira"]["version"] == 2 and current["character:mira"]["media_asset_id"] is None
        )
        assert view["bible"]["status"] == "SUPERSEDED"
        assert bible["version"] == 1

        # The user's own image replaces the scene plate: READY at once, provenance recorded.
        buffer = io.BytesIO()
        Image.new("RGB", (32, 32), (10, 200, 10)).save(buffer, format="PNG")
        media, _ = container.media.register(
            project.id,
            "LOCATION_REFERENCE",
            io.BytesIO(buffer.getvalue()),
            filename="my-roof.png",
            mime_type="image/png",
        )
        replaced = client.post(
            f"/v1/creative/sessions/{session_id}/visuals/anchors/{current['scene:rooftop']['id']}/replace",
            json={"media_asset_id": media.id},
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["anchor"]["status"] == "READY"
        assert replaced.json()["anchor"]["version"] == 2
        assert replaced.json()["anchor"]["media_asset_id"] == media.id
        assert replaced.json()["anchor"]["prompt"]["user_supplied_media_id"] == media.id

        # A foreign or non-image asset is refused; a superseded version cannot be changed.
        other = client.post(
            f"/v1/creative/sessions/{session_id}/visuals/anchors/{current['scene:rooftop']['id']}/replace",
            json={"media_asset_id": media.id},
        )
        assert other.status_code == 409 and other.json()["detail"]["reason_code"] == "ANCHOR_SUPERSEDED"
        missing = client.post(
            f"/v1/creative/sessions/{session_id}/visuals/anchors/{replaced.json()['anchor']['id']}/replace",
            json={"media_asset_id": "nope"},
        )
        assert missing.status_code == 404

    # The regenerated character has no image yet, so the bible waits; once it does, the lock
    # binds the *new* versions - the user-supplied plate included.
    with _client(container) as client:
        refused = client.post(f"/v1/creative/sessions/{session_id}/bible/propose")
        assert (
            refused.status_code == 409
            and refused.json()["detail"]["reason_code"] == "REQUIRED_ANCHORS_NOT_READY"
        )
    _ready_anchors_without_generation(container, session_id, project.id)
    with _client(container) as client:
        proposed = client.post(f"/v1/creative/sessions/{session_id}/bible/propose").json()
        assert proposed["version"] == 2
        versions = {a["anchor_key"]: a["version"] for a in proposed["content"]["anchors"]}
        assert versions["character:mira"] == 2 and versions["scene:rooftop"] == 2
    user_id = _user(container, "images@example.com")
    locked = service.approve_bible(session_id, version=2, actor="images", actor_user_id=user_id)
    assert locked["status"] == "LOCKED"
    with container.database.session() as session:
        identity = session.scalar(select(CharacterIdentityVersion))
        anchor_v2 = session.scalar(
            select(CreativeVisualAnchor).where(
                CreativeVisualAnchor.anchor_key == "character:mira", CreativeVisualAnchor.version == 2
            )
        )
        assert identity.master_asset_id == anchor_v2.media_asset_id
    # After the lock nothing about the visuals can change.
    with _client(container) as client:
        late = client.post(
            f"/v1/creative/sessions/{session_id}/visuals/anchors/{anchor_v2.id}/regenerate",
            json={"direction": "x"},
        )
        assert late.status_code == 409 and late.json()["detail"]["reason_code"] == "INVALID_TRANSITION"


def test_a_model_reply_wrapped_in_prose_still_parses(container, project):
    class Chatty:
        async def execute_chat(self, project_id, role, *, messages, parameters=None):  # type: ignore[no-untyped-def]
            payload = {"assistant_message": "收到，先这样。", "brief_operations": []}
            content = (
                "Sure — here is the JSON you asked for:\n"
                + json.dumps(payload, ensure_ascii=False)
                + "\nLet me know."
            )
            return type(
                "Execution",
                (),
                {"response": {"choices": [{"message": {"content": content}}]}, "execution_record_id": "x"},
            )()

    container.creative_director.model_roles = Chatty()
    with _client(container) as client:
        started = _start(client, project.id, RICH_IDEA)
    assert started["reasoner"] == "MODEL:DIRECTOR"
    assert started["message"] == "收到，先这样。"
